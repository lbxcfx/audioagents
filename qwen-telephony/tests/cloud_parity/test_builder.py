from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import zipfile

import pytest
from pydantic import ValidationError


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.builder import AgentSpec, BuilderService, RevisionConflictError
from server.cloud_parity.store import PlatformStore, ResourceNotFoundError


def valid_spec() -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "name": "sales-agent",
            "instructions": "向 {{metadata.user_name}} 介绍产品。",
            "welcome_greeting": "您好 {{metadata.user_name}}",
            "models": {
                "stt": "qwen/stt:zh",
                "llm": "qwen/qwen-plus",
                "tts": "qwen/qwen3-tts:Cherry",
            },
            "conversation": {
                "mode": "data_collection",
                "fields": [
                    {"name": "interest", "description": "客户意向", "required": True}
                ],
            },
            "tools": [
                {
                    "type": "http",
                    "name": "save_lead",
                    "description": "保存线索",
                    "method": "POST",
                    "url": "https://example.com/leads",
                    "headers": {"Authorization": "Bearer {{secrets.CRM_TOKEN}}"},
                }
            ],
        }
    )


@pytest.fixture()
def builder_stack(tmp_path: Path):
    store = PlatformStore(tmp_path / "builder.sqlite3")
    store.initialize()
    first = store.create_project(name="Builder", slug="builder", owner_id="owner")
    second = store.create_project(name="Other", slug="other", owner_id="other")
    return BuilderService(store), first, second


def test_spec_round_trip_and_optimistic_revision(builder_stack) -> None:
    builder, project, _ = builder_stack
    created = builder.save(project_id=project["id"], actor_id="owner", spec=valid_spec())
    assert created["revision"] == 1
    assert AgentSpec.model_validate(created["spec"]) == valid_spec()

    updated_spec = valid_spec().model_copy(update={"welcome_greeting": "新的欢迎语"})
    updated = builder.save(
        project_id=project["id"],
        actor_id="owner",
        spec=updated_spec,
        spec_id=created["id"],
        expected_revision=1,
    )
    assert updated["revision"] == 2
    with pytest.raises(RevisionConflictError):
        builder.save(
            project_id=project["id"],
            actor_id="owner",
            spec=valid_spec(),
            spec_id=created["id"],
            expected_revision=1,
        )


def test_invalid_url_and_template_are_rejected() -> None:
    payload = valid_spec().model_dump(mode="json")
    payload["tools"][0]["url"] = "file:///etc/passwd"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(payload)

    payload = valid_spec().model_dump(mode="json")
    payload["instructions"] = "Hello {{environment.PATH}}"
    with pytest.raises(ValidationError, match="unsupported template"):
        AgentSpec.model_validate(payload)


def test_export_contains_compilable_project_without_secret_values(builder_stack) -> None:
    builder, project, _ = builder_stack
    record = builder.save(project_id=project["id"], actor_id="owner", spec=valid_spec())
    content = builder.export_zip(
        project_id=project["id"], user_id="owner", spec_id=record["id"]
    )

    with zipfile.ZipFile(BytesIO(content)) as archive:
        assert set(archive.namelist()) == {
            "agent.py", "agent-spec.json", "requirements.txt", "Dockerfile", "README.md"
        }
        source = archive.read("agent.py").decode("utf-8")
        compile(source, "agent.py", "exec")
        assert "CRM_TOKEN" in source
        assert "very-secret-value" not in source


def test_project_cannot_read_another_projects_spec(builder_stack) -> None:
    builder, first, second = builder_stack
    record = builder.save(project_id=first["id"], actor_id="owner", spec=valid_spec())
    with pytest.raises(ResourceNotFoundError):
        builder.get(project_id=second["id"], user_id="other", spec_id=record["id"])
