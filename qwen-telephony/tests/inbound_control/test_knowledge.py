from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.store import AccessDeniedError, PlatformStore, ResourceNotFoundError
from server.inbound_control.knowledge import KnowledgeStore
from server.inbound_control.store import InboundAgentStore


def test_knowledge_is_project_scoped_searchable_and_bindable(tmp_path):
    platform = PlatformStore(tmp_path / "platform.sqlite3")
    platform.initialize()
    first = platform.create_project(name="甲公司", slug="kb-a", owner_id="owner-a")
    second = platform.create_project(name="乙公司", slug="kb-b", owner_id="owner-b")
    knowledge = KnowledgeStore(platform)
    knowledge.migrate()
    inbound = InboundAgentStore(
        platform,
        knowledge_validator=lambda project_id, base_ids: knowledge.assert_bases(
            project_id=project_id, base_ids=base_ids
        ),
        knowledge_snapshotter=lambda project_id, base_ids: knowledge.snapshot_document_ids(
            project_id=project_id, base_ids=base_ids
        ),
    )
    inbound.migrate()

    base = knowledge.create_base(
        project_id=first["id"], actor_id="owner-a", name="售后政策", description=""
    )
    document = knowledge.add_text_document(
        project_id=first["id"], actor_id="owner-a", base_id=base["id"],
        filename="退换货.md", media_type="text/markdown",
        text="# 退换货\n\n商品签收后七天内可申请无理由退货，定制商品除外。",
    )
    results = knowledge.search(
        project_id=first["id"], base_ids=[base["id"]], query="几天内能退货", limit=3
    )
    assert results[0]["document_id"] == document["id"]
    assert results[0]["filename"] == "退换货.md"
    assert "七天" in results[0]["content"]

    job = knowledge.queue_document(project_id=first["id"], actor_id="owner-a", base_id=base["id"], filename="安装.txt", media_type="text/plain", data="设备接通电源后长按启动键三秒。".encode())
    assert knowledge.get_job(project_id=first["id"], actor_id="owner-a", job_id=job["id"])["status"] == "queued"
    knowledge.process_job(job["id"])
    completed = knowledge.get_job(project_id=first["id"], actor_id="owner-a", job_id=job["id"])
    assert completed["status"] == "completed" and completed["progress"] == 100 and completed["document_id"]

    with pytest.raises(ResourceNotFoundError):
        knowledge.search(project_id=second["id"], base_ids=[base["id"]], query="退货")
    with pytest.raises(AccessDeniedError):
        knowledge.list_bases(project_id=first["id"], actor_id="owner-b")

    config = {
        "instructions": "你是企业客服，需要依据企业知识准确回答客户提出的问题。",
        "welcome_message": "您好，请问有什么可以帮您？",
        "voice": "longanlingxin", "language": "zh-CN", "max_duration_seconds": 600,
        "recording_mode": "off", "recording_disclosure": "", "tools": [],
        "knowledge_sources": [base["id"]],
    }
    agent = inbound.create_agent(
        project_id=first["id"], actor_id="owner-a", name="知识客服",
        description="", kind="enterprise", config=config,
    )
    assert agent["draft_config"]["knowledge_sources"] == [base["id"]]
    version = inbound.publish_agent(
        project_id=first["id"], actor_id="owner-a", agent_id=agent["id"], expected_revision=1
    )
    pinned_ids = version["config"]["knowledge_document_ids"]
    later = knowledge.add_text_document(
        project_id=first["id"], actor_id="owner-a", base_id=base["id"],
        filename="新版.md", media_type="text/markdown", text="新版本专属口令是星河。",
    )
    assert later["id"] not in pinned_ids
    assert knowledge.search(
        project_id=first["id"], base_ids=[base["id"]], document_ids=pinned_ids, query="星河"
    ) == []
    with pytest.raises(ResourceNotFoundError):
        inbound.create_agent(
            project_id=second["id"], actor_id="owner-b", name="越权客服",
            description="", kind="enterprise", config=config,
        )
    platform.close()
