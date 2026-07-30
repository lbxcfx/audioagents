from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
import uuid

from server.cloud_parity.store import PlatformStore, ResourceNotFoundError
from .store import row_dict, utc_now

CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_content_assets (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
 source_url TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'draft', created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE, UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_inbound_content_project ON inbound_content_assets(project_id, status, updated_at DESC);
"""

VIDEO_SERVICE_PRESETS = {
    "avatars": [
        {"id": "fanyu-broadcast_standing", "name": "梵宇", "style": "专业站姿", "provider": "aliyun_ims", "mode": "rendered", "accent": "#30445b"},
        {"id": "baihan-broadcast_standing", "name": "白涵", "style": "亲和讲解", "provider": "aliyun_ims", "mode": "rendered", "accent": "#7b5b4b"},
        {"id": "realtime-service-host", "name": "实时服务讲解员", "style": "可打断互动", "provider": "avatar_provider", "mode": "realtime", "accent": "#315b52"},
    ],
    "content": [
        {"id": "preset-welcome", "name": "30 秒服务介绍", "kind": "video", "description": "数字人介绍视频客服的使用方法", "duration": "00:30", "accent": "#27384d"},
        {"id": "preset-install", "name": "产品安装演示", "kind": "video", "description": "从开箱到通电的标准安装流程", "duration": "02:40", "accent": "#604c3d"},
        {"id": "preset-checklist", "name": "安装完成检查", "kind": "steps", "description": "逐项确认电源、网络和指示灯", "duration": "4 步", "accent": "#3f5b53"},
    ],
    "tutorial": {"title": "用一分钟完成第一场视频服务", "script": [
        "欢迎来到视频客服工作台。这里可以让数字人一边讲解，一边向客户播放产品视频。",
        "第一步，选择一位数字人主持。实时模式支持客户随时提问，口播模式适合固定产品介绍。",
        "第二步，从内容库选择审核过的安装视频、图片或步骤卡片。",
        "最后进入预演，像客户一样检查声音、画面和知识问答，再发布给客户。",
    ]},
}

class ContentStore:
    def __init__(self, platform: PlatformStore): self.platform = platform
    def migrate(self) -> None:
        with self.platform.transaction() as conn:
            self.platform._database.acquire_migration_lock(conn); conn.executescript(CONTENT_SCHEMA)
    def create(self, *, project_id: str, actor_id: str, name: str, kind: str, source_url: str, description: str, metadata: dict[str, Any]) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.write")
        parsed = urlparse(source_url)
        if kind not in {"image", "video", "pdf", "steps"} or parsed.scheme != "https" or not parsed.hostname: raise ValueError("asset must use an HTTPS URL and a supported kind")
        asset_id, now = str(uuid.uuid4()), utc_now()
        with self.platform.transaction() as conn:
            conn.execute("INSERT INTO inbound_content_assets (id, project_id, name, kind, source_url, description, metadata_json, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (asset_id, project_id, name.strip(), kind, source_url, description.strip(), json.dumps(metadata), actor_id, now, now))
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="content_asset.create", resource_type="content_asset", resource_id=asset_id, payload={"name": name, "kind": kind})
        return self.get(project_id=project_id, asset_id=asset_id)
    def get(self, *, project_id: str, asset_id: str, published_only: bool = False) -> dict[str, Any]:
        suffix = " AND status = 'published'" if published_only else ""
        with self.platform.connect() as conn: row = conn.execute(f"SELECT * FROM inbound_content_assets WHERE id = ? AND project_id = ?{suffix}", (asset_id, project_id)).fetchone()
        if row is None: raise ResourceNotFoundError("content asset not found")
        item = row_dict(row) or {}; item["metadata"] = json.loads(item.pop("metadata_json")); return item
    def list(self, *, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        with self.platform.connect() as conn: rows = conn.execute("SELECT id FROM inbound_content_assets WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [self.get(project_id=project_id, asset_id=str(row["id"])) for row in rows]
    def publish(self, *, project_id: str, actor_id: str, asset_id: str) -> dict[str, Any]:
        self.platform.require_role(project_id, actor_id, {"owner", "admin"}); now = utc_now()
        with self.platform.transaction() as conn:
            result = conn.execute("UPDATE inbound_content_assets SET status = 'published', updated_at = ? WHERE id = ? AND project_id = ?", (now, asset_id, project_id))
            if getattr(result, "rowcount", 0) == 0: raise ResourceNotFoundError("content asset not found")
            self.platform._append_audit(conn, project_id=project_id, actor_id=actor_id, action="content_asset.publish", resource_type="content_asset", resource_id=asset_id)
        return self.get(project_id=project_id, asset_id=asset_id)
    def assert_assets(self, *, project_id: str, asset_ids: list[str]) -> None:
        for asset_id in asset_ids: self.get(project_id=project_id, asset_id=asset_id, published_only=True)
    def video_service_presets(self, *, project_id: str, actor_id: str) -> dict[str, Any]:
        self.platform.require_permission(project_id, actor_id, "agent.read")
        return VIDEO_SERVICE_PRESETS
