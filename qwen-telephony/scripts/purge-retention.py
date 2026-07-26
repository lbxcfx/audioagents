from __future__ import annotations

import json
from pathlib import Path
import sys

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[1]
AGENT_ROOT = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

load_dotenv(AGENT_ROOT / ".env")
load_dotenv(PROJECT_DIR / "config" / "local.env", override=False)

from server.cloud_parity.config import PlatformSettings
from server.cloud_parity.store import PlatformStore


def main() -> None:
    settings = PlatformSettings.from_env(AGENT_ROOT)
    store = PlatformStore(
        settings.database_path,
        default_retention_days=settings.default_retention_days,
        database_url=settings.database_url,
        min_pool_size=settings.database_pool_min_size,
        max_pool_size=settings.database_pool_max_size,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        store.initialize()
        print(json.dumps({"projects": store.run_retention_maintenance()}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
