from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Any, ContextManager, Iterator, Protocol, Sequence


class CursorLike(Protocol):
    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def fetchmany(self, size: int = 0) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


class ConnectionLike(Protocol):
    def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
    ) -> CursorLike: ...

    def executescript(self, script: str) -> None: ...


class DatabaseAdapter(Protocol):
    backend: str

    def connection(self) -> ContextManager[ConnectionLike]: ...

    def transaction(self) -> ContextManager[ConnectionLike]: ...

    def acquire_migration_lock(self, conn: ConnectionLike) -> None: ...

    def close(self) -> None: ...


class DatabaseConfigurationError(RuntimeError):
    pass


def is_integrity_error(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        from psycopg import IntegrityError as PostgresIntegrityError
    except ImportError:
        return False
    return isinstance(exc, PostgresIntegrityError)


def _split_sql_statements(script: str) -> list[str]:
    """Split the project's migration DDL without corrupting quoted strings.

    The migrations intentionally avoid procedural PostgreSQL blocks. Keeping the
    splitter here lets SQLite and psycopg execute identical, transactional DDL.
    """

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    current.append(script[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1

    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


class _SQLiteConnection:
    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
    ) -> sqlite3.Cursor:
        return self._raw.execute(query, params or ())

    def executescript(self, script: str) -> None:
        # sqlite3.Connection.executescript() implicitly commits first, which can
        # leave a partially applied schema. Execute statements individually so
        # the surrounding BEGIN IMMEDIATE remains atomic.
        for statement in _split_sql_statements(script):
            self._raw.execute(statement)


class SQLiteAdapter:
    backend = "sqlite"

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser().resolve()

    def _open(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.database_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[_SQLiteConnection]:
        conn = self._open()
        try:
            yield _SQLiteConnection(conn)
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[_SQLiteConnection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield _SQLiteConnection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def acquire_migration_lock(self, conn: ConnectionLike) -> None:
        # BEGIN IMMEDIATE already serializes SQLite writers.
        return None

    def close(self) -> None:
        return None


class _PostgresConnection:
    def __init__(self, raw: Any):
        self._raw = raw

    def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
    ) -> CursorLike:
        # Cloud-parity SQL uses the DB-API qmark style. PostgreSQL's JSON
        # operators are not used by this schema, so translating placeholders is
        # deterministic and keeps all services backend-neutral.
        statement = query.replace("?", "%s")
        return self._raw.execute(statement, params)

    def executescript(self, script: str) -> None:
        for statement in _split_sql_statements(script):
            self._raw.execute(statement)


class PostgresAdapter:
    backend = "postgresql"

    def __init__(
        self,
        database_url: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        pool_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 10.0,
        application_name: str = "ai-login-replica-cloud-parity",
    ):
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError(
                "CLOUD_PARITY_DATABASE_URL must use postgresql:// or postgres://"
            )
        if min_pool_size < 0 or max_pool_size < 1 or min_pool_size > max_pool_size:
            raise DatabaseConfigurationError("invalid PostgreSQL pool size")
        if pool_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise DatabaseConfigurationError("database timeouts must be positive")

        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise DatabaseConfigurationError(
                "PostgreSQL mode requires psycopg and psycopg-pool; install requirements.lock"
            ) from exc

        self._pool_timeout_seconds = pool_timeout_seconds
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            timeout=pool_timeout_seconds,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": int(max(1, connect_timeout_seconds)),
                "application_name": application_name,
            },
            open=False,
            name="cloud-parity",
        )
        self._opened = False
        self._open_lock = threading.Lock()

    def _ensure_open(self) -> None:
        if self._opened:
            return
        with self._open_lock:
            if self._opened:
                return
            self._pool.open(wait=True, timeout=self._pool_timeout_seconds)
            self._opened = True

    @contextmanager
    def connection(self) -> Iterator[_PostgresConnection]:
        self._ensure_open()
        with self._pool.connection(timeout=self._pool_timeout_seconds) as conn:
            yield _PostgresConnection(conn)

    @contextmanager
    def transaction(self) -> Iterator[_PostgresConnection]:
        self._ensure_open()
        with self._pool.connection(timeout=self._pool_timeout_seconds) as conn:
            with conn.transaction():
                yield _PostgresConnection(conn)

    def acquire_migration_lock(self, conn: ConnectionLike) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            ("ai-login-replica:cloud-parity:migrations",),
        )

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False


def create_database_adapter(
    *,
    database_path: str | Path,
    database_url: str | None = None,
    min_pool_size: int = 1,
    max_pool_size: int = 10,
    pool_timeout_seconds: float = 10.0,
    connect_timeout_seconds: float = 10.0,
) -> DatabaseAdapter:
    if database_url:
        return PostgresAdapter(
            database_url,
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
            pool_timeout_seconds=pool_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    return SQLiteAdapter(database_path)
