from __future__ import annotations

import hashlib
import base64
import io
import json
import math
import os
import re
from typing import Any
import uuid
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader
import httpx
from minio import Minio
from urllib.parse import urlparse

from server.cloud_parity.store import AccessDeniedError, PlatformStore, ResourceNotFoundError

from .store import row_dict, utc_now


KNOWLEDGE_SCHEMA_VERSION = 4
KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_knowledge_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_knowledge_bases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS inbound_knowledge_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(knowledge_base_id) REFERENCES inbound_knowledge_bases(id) ON DELETE CASCADE,
    UNIQUE(knowledge_base_id, content_sha256)
);
CREATE TABLE IF NOT EXISTS inbound_knowledge_chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    search_terms_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(knowledge_base_id) REFERENCES inbound_knowledge_bases(id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES inbound_knowledge_documents(id) ON DELETE CASCADE,
    UNIQUE(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_inbound_kb_project ON inbound_knowledge_bases(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_kb_documents ON inbound_knowledge_documents(project_id, knowledge_base_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_kb_chunks ON inbound_knowledge_chunks(project_id, knowledge_base_id, document_id);
"""

KNOWLEDGE_SCHEMA_V2 = """
ALTER TABLE inbound_knowledge_chunks ADD COLUMN embedding_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE inbound_knowledge_documents ADD COLUMN index_version INTEGER NOT NULL DEFAULT 1;
"""
KNOWLEDGE_SCHEMA_V3 = """
ALTER TABLE inbound_knowledge_documents ADD COLUMN raw_object_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE inbound_knowledge_documents ADD COLUMN parsed_object_ref TEXT NOT NULL DEFAULT '';
"""
KNOWLEDGE_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS inbound_knowledge_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
 filename TEXT NOT NULL, media_type TEXT NOT NULL, payload_base64 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, error_message TEXT NOT NULL DEFAULT '', document_id TEXT,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
 FOREIGN KEY(knowledge_base_id) REFERENCES inbound_knowledge_bases(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_inbound_knowledge_jobs ON inbound_knowledge_jobs(project_id, status, updated_at);
"""


def _terms(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9][a-z0-9_.-]{1,63}", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    bigrams = ["".join(chinese[index:index + 2]) for index in range(len(chinese) - 1)]
    return latin + bigrams


def _chunks(text: str, *, target: int = 900, overlap: int = 120) -> list[tuple[str, str]]:
    clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", clean) if part.strip()]
    output: list[tuple[str, str]] = []
    buffer = ""
    heading = ""
    for paragraph in paragraphs:
        if paragraph.startswith("#"):
            heading = paragraph.lstrip("#").strip()[:240]
        if buffer and len(buffer) + len(paragraph) + 2 > target:
            output.append((heading, buffer))
            buffer = buffer[-overlap:] + "\n\n" + paragraph
        else:
            buffer = f"{buffer}\n\n{paragraph}".strip()
    if buffer:
        output.append((heading, buffer))
    return output


SUPPORTED_MEDIA_TYPES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def extract_document_text(data: bytes, *, filename: str, media_type: str) -> str:
    expected_suffix = SUPPORTED_MEDIA_TYPES.get(media_type)
    if expected_suffix is None or not filename.lower().endswith(expected_suffix):
        raise ValueError("document type and filename extension do not match")
    if not data or len(data) > 20_000_000:
        raise ValueError("document must contain 1 to 20000000 bytes")
    if media_type in {"text/plain", "text/markdown"}:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("text documents must use UTF-8 encoding") from exc
    if media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("invalid PDF signature")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ValueError("encrypted PDF documents are not supported")
            if len(reader.pages) > 500:
                raise ValueError("PDF documents may contain at most 500 pages")
            return "\n\n".join(
                f"# 第 {index + 1} 页\n\n{page.extract_text() or ''}"
                for index, page in enumerate(reader.pages)
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF parsing failed") from exc
    if not data.startswith(b"PK"):
        raise ValueError("invalid DOCX signature")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2_000 or sum(info.file_size for info in infos) > 100_000_000:
                raise ValueError("DOCX archive exceeds the safe expansion limit")
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(f"{namespace}p"):
            value = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
            if value:
                paragraphs.append(value)
        return "\n\n".join(paragraphs)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("DOCX parsing failed") from exc


class KnowledgeStore:
    def __init__(self, platform: PlatformStore):
        self.platform = platform

    def migrate(self) -> None:
        with self.platform.transaction() as conn:
            self.platform._database.acquire_migration_lock(conn)
            conn.executescript(KNOWLEDGE_SCHEMA)
            conn.execute(
                "INSERT INTO inbound_knowledge_schema_migrations (version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
                (1, utc_now()),
            )
            applied = conn.execute("SELECT 1 FROM inbound_knowledge_schema_migrations WHERE version = 2").fetchone()
            if applied is None:
                conn.executescript(KNOWLEDGE_SCHEMA_V2)
                conn.execute("INSERT INTO inbound_knowledge_schema_migrations (version, applied_at) VALUES (2, ?)", (utc_now(),))
            applied_v3 = conn.execute("SELECT 1 FROM inbound_knowledge_schema_migrations WHERE version = 3").fetchone()
            if applied_v3 is None:
                conn.executescript(KNOWLEDGE_SCHEMA_V3)
                conn.execute("INSERT INTO inbound_knowledge_schema_migrations (version, applied_at) VALUES (3, ?)", (utc_now(),))
            applied_v4 = conn.execute("SELECT 1 FROM inbound_knowledge_schema_migrations WHERE version = 4").fetchone()
            if applied_v4 is None:
                conn.executescript(KNOWLEDGE_SCHEMA_V4)
                conn.execute("INSERT INTO inbound_knowledge_schema_migrations (version, applied_at) VALUES (4, ?)", (utc_now(),))

    def healthcheck(self) -> dict[str, Any]:
        with self.platform.connect() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM inbound_knowledge_schema_migrations").fetchone()
        return {"status": "ok", "schema_version": int(row["version"] or 0)}

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if os.getenv("INBOUND_EMBEDDING_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return [[] for _ in texts]
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key: raise ValueError("DASHSCOPE_API_KEY is required when embedding is enabled")
        endpoint = os.getenv("QWEN_OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        with httpx.Client(timeout=30) as client:
            response = client.post(f"{endpoint}/embeddings", headers={"Authorization": f"Bearer {api_key}"}, json={"model": os.getenv("INBOUND_EMBEDDING_MODEL", "text-embedding-v4"), "input": texts, "encoding_format": "float"})
            response.raise_for_status(); data = response.json().get("data", [])
        vectors = [item.get("embedding", []) for item in sorted(data, key=lambda item: item.get("index", 0))]
        if len(vectors) != len(texts): raise ValueError("embedding provider returned an invalid vector count")
        return vectors

    def _store_objects(self, *, project_id: str, base_id: str, digest: str, media_type: str, data: bytes, parsed_text: str) -> tuple[str, str]:
        if os.getenv("INBOUND_KNOWLEDGE_OBJECT_STORE_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return "", ""
        endpoint = urlparse(os.getenv("INBOUND_KNOWLEDGE_S3_ENDPOINT", "")); bucket = os.getenv("INBOUND_KNOWLEDGE_S3_BUCKET", "audioagents-knowledge").strip()
        access, secret = os.getenv("INBOUND_KNOWLEDGE_S3_ACCESS_KEY", "").strip(), os.getenv("INBOUND_KNOWLEDGE_S3_SECRET", "").strip()
        if not endpoint.hostname or not access or not secret or not bucket: raise ValueError("knowledge object storage is not fully configured")
        address = f"{endpoint.hostname}:{endpoint.port}" if endpoint.port else endpoint.hostname
        client = Minio(address, access_key=access, secret_key=secret, secure=endpoint.scheme == "https")
        prefix = f"projects/{project_id}/knowledge/{base_id}/{digest}"
        raw_name, parsed_name = f"{prefix}/source{SUPPORTED_MEDIA_TYPES[media_type]}", f"{prefix}/parsed.md"
        client.put_object(bucket, raw_name, io.BytesIO(data), len(data), content_type=media_type)
        parsed_bytes = parsed_text.encode("utf-8"); client.put_object(bucket, parsed_name, io.BytesIO(parsed_bytes), len(parsed_bytes), content_type="text/markdown")
        return f"s3://{bucket}/{raw_name}", f"s3://{bucket}/{parsed_name}"

    def _base(self, conn: Any, project_id: str, base_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM inbound_knowledge_bases WHERE id = ? AND project_id = ?",
            (base_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("knowledge base not found")
        return row_dict(row) or {}

    def assert_bases(self, *, project_id: str, base_ids: list[str]) -> None:
        if not base_ids:
            return
        with self.platform.connect() as conn:
            for base_id in base_ids:
                base = self._base(conn, project_id, base_id)
                if base["status"] != "active":
                    raise ValueError("knowledge base is not active")

    def create_base(self, *, project_id: str, actor_id: str, name: str, description: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        base_id, now = str(uuid.uuid4()), utc_now()
        with self.platform.transaction() as conn:
            conn.execute(
                "INSERT INTO inbound_knowledge_bases (id, project_id, name, description, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (base_id, project_id, name.strip(), description.strip(), actor_id, now, now),
            )
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="knowledge_base.create", resource_type="knowledge_base", resource_id=base_id, payload={"name": name.strip()})
        return self.get_base(project_id=project_id, actor_id=actor_id, base_id=base_id)

    def get_base(self, *, project_id: str, actor_id: str, base_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            item = self._base(conn, project_id, base_id)
            count = conn.execute("SELECT COUNT(*) AS count FROM inbound_knowledge_documents WHERE project_id = ? AND knowledge_base_id = ?", (project_id, base_id)).fetchone()
        item["document_count"] = int(count["count"] or 0)
        return item

    def list_bases(self, *, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            rows = conn.execute(
                "SELECT b.*, (SELECT COUNT(*) FROM inbound_knowledge_documents d WHERE d.knowledge_base_id = b.id) AS document_count FROM inbound_knowledge_bases b WHERE b.project_id = ? ORDER BY b.updated_at DESC",
                (project_id,),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def add_text_document(self, *, project_id: str, actor_id: str, base_id: str, filename: str, media_type: str, text: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        encoded = text.encode("utf-8")
        if not encoded or len(encoded) > 5_000_000:
            raise ValueError("document text must contain 1 to 5000000 UTF-8 bytes")
        digest, document_id, now = hashlib.sha256(encoded).hexdigest(), str(uuid.uuid4()), utc_now()
        pieces = _chunks(text)
        if not pieces:
            raise ValueError("document contains no indexable text")
        embeddings = self._embed([content for _, content in pieces])
        with self.platform.transaction() as conn:
            self._base(conn, project_id, base_id)
            existing = conn.execute("SELECT id FROM inbound_knowledge_documents WHERE knowledge_base_id = ? AND content_sha256 = ?", (base_id, digest)).fetchone()
            if existing is not None:
                raise ValueError("the same document content already exists in this knowledge base")
            conn.execute(
                "INSERT INTO inbound_knowledge_documents (id, project_id, knowledge_base_id, filename, media_type, content_sha256, source_text, status, chunk_count, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)",
                (document_id, project_id, base_id, filename.strip(), media_type.strip(), digest, text, len(pieces), actor_id, now, now),
            )
            for ordinal, (heading, content) in enumerate(pieces):
                conn.execute(
                    "INSERT INTO inbound_knowledge_chunks (id, project_id, knowledge_base_id, document_id, ordinal, heading, content, search_terms_json, embedding_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), project_id, base_id, document_id, ordinal, heading, content, json.dumps(_terms(content), ensure_ascii=False), json.dumps(embeddings[ordinal]), now),
                )
            conn.execute("UPDATE inbound_knowledge_bases SET updated_at = ? WHERE id = ? AND project_id = ?", (now, base_id, project_id))
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="knowledge_document.index", resource_type="knowledge_document", resource_id=document_id, payload={"knowledge_base_id": base_id, "filename": filename.strip(), "sha256": digest, "chunk_count": len(pieces)})
        return {"id": document_id, "project_id": project_id, "knowledge_base_id": base_id, "filename": filename.strip(), "media_type": media_type.strip(), "content_sha256": digest, "status": "ready", "chunk_count": len(pieces), "created_at": now}

    def add_document(self, *, project_id: str, actor_id: str, base_id: str, filename: str, media_type: str, data: bytes) -> dict[str, Any]:
        text = extract_document_text(data, filename=filename, media_type=media_type)
        raw_ref, parsed_ref = self._store_objects(project_id=project_id, base_id=base_id, digest=hashlib.sha256(data).hexdigest(), media_type=media_type, data=data, parsed_text=text)
        result = self.add_text_document(project_id=project_id, actor_id=actor_id, base_id=base_id, filename=filename, media_type=media_type, text=text)
        with self.platform.transaction() as conn: conn.execute("UPDATE inbound_knowledge_documents SET raw_object_ref = ?, parsed_object_ref = ? WHERE id = ? AND project_id = ?", (raw_ref, parsed_ref, result["id"], project_id))
        result["raw_object_ref"], result["parsed_object_ref"] = raw_ref, parsed_ref
        return result

    def queue_document(self, *, project_id: str, actor_id: str, base_id: str, filename: str, media_type: str, data: bytes) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        if not data or len(data) > 20_000_000: raise ValueError("document must contain 1 to 20000000 bytes")
        job_id, now = str(uuid.uuid4()), utc_now()
        with self.platform.transaction() as conn:
            self._base(conn, project_id, base_id)
            conn.execute("INSERT INTO inbound_knowledge_jobs (id, project_id, knowledge_base_id, filename, media_type, payload_base64, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (job_id, project_id, base_id, filename, media_type, base64.b64encode(data).decode(), actor_id, now, now))
        return {"id": job_id, "status": "queued", "progress": 0, "created_at": now}

    def process_job(self, job_id: str) -> None:
        with self.platform.transaction() as conn:
            row = conn.execute("SELECT * FROM inbound_knowledge_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "failed"}: return
            claimed = conn.execute("UPDATE inbound_knowledge_jobs SET status = 'processing', progress = 10, attempts = attempts + 1, error_message = '', updated_at = ? WHERE id = ? AND status IN ('queued', 'failed')", (utc_now(), job_id))
            if getattr(claimed, "rowcount", 0) == 0: return
            item = row_dict(row) or {}
        try:
            result = self.add_document(project_id=str(item["project_id"]), actor_id=str(item["created_by"]), base_id=str(item["knowledge_base_id"]), filename=str(item["filename"]), media_type=str(item["media_type"]), data=base64.b64decode(item["payload_base64"]))
            with self.platform.transaction() as conn: conn.execute("UPDATE inbound_knowledge_jobs SET status = 'completed', progress = 100, document_id = ?, payload_base64 = '', updated_at = ? WHERE id = ?", (result["id"], utc_now(), job_id))
        except Exception as exc:
            with self.platform.transaction() as conn:
                current = conn.execute("SELECT attempts FROM inbound_knowledge_jobs WHERE id = ?", (job_id,)).fetchone(); attempts = int(current["attempts"] or 1)
                final = attempts >= 3
                conn.execute("UPDATE inbound_knowledge_jobs SET status = ?, progress = 0, error_message = ?, payload_base64 = CASE WHEN ? THEN '' ELSE payload_base64 END, updated_at = ? WHERE id = ?", ("dead" if final else "failed", str(exc)[:1000], final, utc_now(), job_id))
            if not final: self.process_job(job_id)

    def pending_job_ids(self, limit: int = 10) -> list[str]:
        with self.platform.connect() as conn: rows = conn.execute("SELECT id FROM inbound_knowledge_jobs WHERE status IN ('queued', 'failed') ORDER BY created_at LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        return [str(row["id"]) for row in rows]

    def get_job(self, *, project_id: str, actor_id: str, job_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn: row = conn.execute("SELECT id, project_id, knowledge_base_id, filename, media_type, status, progress, attempts, error_message, document_id, created_at, updated_at FROM inbound_knowledge_jobs WHERE id = ? AND project_id = ?", (job_id, project_id)).fetchone()
        if row is None: raise ResourceNotFoundError("knowledge job not found")
        return row_dict(row) or {}

    def list_documents(self, *, project_id: str, actor_id: str, base_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn:
            self._base(conn, project_id, base_id)
            rows = conn.execute(
                "SELECT id, project_id, knowledge_base_id, filename, media_type, content_sha256, status, error_message, chunk_count, created_by, created_at, updated_at FROM inbound_knowledge_documents WHERE project_id = ? AND knowledge_base_id = ? ORDER BY updated_at DESC",
                (project_id, base_id),
            ).fetchall()
        return [row_dict(row) or {} for row in rows]

    def delete_document(self, *, project_id: str, actor_id: str, base_id: str, document_id: str) -> dict[str, str]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        with self.platform.transaction() as conn:
            self._base(conn, project_id, base_id)
            row = conn.execute("SELECT filename FROM inbound_knowledge_documents WHERE id = ? AND project_id = ? AND knowledge_base_id = ?", (document_id, project_id, base_id)).fetchone()
            if row is None:
                raise ResourceNotFoundError("knowledge document not found")
            conn.execute("DELETE FROM inbound_knowledge_documents WHERE id = ? AND project_id = ?", (document_id, project_id))
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="knowledge_document.delete", resource_type="knowledge_document", resource_id=document_id, payload={"knowledge_base_id": base_id, "filename": row["filename"]})
        return {"id": document_id, "status": "deleted"}

    def snapshot_document_ids(self, *, project_id: str, base_ids: list[str]) -> list[str]:
        self.assert_bases(project_id=project_id, base_ids=base_ids)
        placeholders = ",".join("?" for _ in base_ids)
        with self.platform.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM inbound_knowledge_documents WHERE project_id = ? AND knowledge_base_id IN ({placeholders}) AND status = 'ready' ORDER BY id",
                (project_id, *base_ids),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def search(self, *, project_id: str, base_ids: list[str], query: str, limit: int = 5, document_ids: list[str] | None = None) -> list[dict[str, Any]]:
        query_terms = _terms(query)
        if not query_terms or not base_ids:
            return []
        self.assert_bases(project_id=project_id, base_ids=base_ids)
        placeholders = ",".join("?" for _ in base_ids)
        document_filter, document_parameters = "", []
        if document_ids is not None:
            if not document_ids:
                return []
            document_filter = f" AND d.id IN ({','.join('?' for _ in document_ids)})"
            document_parameters = document_ids
        with self.platform.connect() as conn:
            rows = conn.execute(
                f"SELECT c.*, d.filename FROM inbound_knowledge_chunks c JOIN inbound_knowledge_documents d ON d.id = c.document_id AND d.project_id = c.project_id WHERE c.project_id = ? AND c.knowledge_base_id IN ({placeholders}) AND d.status = 'ready'{document_filter}",
                (project_id, *base_ids, *document_parameters),
            ).fetchall()
        query_set = set(query_terms)
        query_vector = self._embed([query])[0]
        ranked = []
        for row in rows:
            item = row_dict(row) or {}
            terms = json.loads(item.pop("search_terms_json"))
            vector = json.loads(item.pop("embedding_json", "[]"))
            counts = {term: terms.count(term) for term in query_set}
            matched = sum(1 for value in counts.values() if value)
            semantic = 0.0
            if query_vector and vector and len(query_vector) == len(vector):
                denominator = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(sum(value * value for value in vector))
                semantic = sum(left * right for left, right in zip(query_vector, vector)) / denominator if denominator else 0.0
            if not matched and semantic <= 0:
                continue
            lexical = matched / math.sqrt(max(1, len(query_set))) + sum(math.log1p(value) for value in counts.values()) / 10
            score = lexical * 0.55 + max(0.0, semantic) * 0.45 if query_vector else lexical
            ranked.append({"chunk_id": item["id"], "knowledge_base_id": item["knowledge_base_id"], "document_id": item["document_id"], "filename": item["filename"], "ordinal": item["ordinal"], "heading": item["heading"], "content": item["content"], "score": round(score, 6)})
        ranked.sort(key=lambda item: (-item["score"], item["document_id"], item["ordinal"]))
        return ranked[:max(1, min(limit, 20))]
