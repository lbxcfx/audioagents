from __future__ import annotations

from io import BytesIO
import json
import re
from typing import Annotated, Any, Literal, Union
import uuid
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


TEMPLATE_RE = re.compile(r"{{\s*(metadata\.[a-zA-Z_][\w.]*|secrets\.[A-Z][A-Z0-9_]*)\s*}}")
ANY_TEMPLATE_RE = re.compile(r"{{(.*?)}}")


def _validate_templates(value: str) -> str:
    for raw in ANY_TEMPLATE_RE.findall(value):
        candidate = "{{" + raw + "}}"
        if TEMPLATE_RE.fullmatch(candidate) is None:
            raise ValueError(f"unsupported template expression: {candidate}")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    stt: str = Field(min_length=1, max_length=200)
    llm: str = Field(min_length=1, max_length=200)
    tts: str = Field(min_length=1, max_length=200)


class DataField(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=80)
    description: str = Field(min_length=1, max_length=500)
    value_type: Literal["string", "number", "boolean", "object", "list"] = "string"
    multiple: bool = False
    required: bool = False


class ConversationConfig(StrictModel):
    mode: Literal["open", "data_collection"] = "open"
    fields: list[DataField] = Field(default_factory=list, max_length=50)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: list[DataField]) -> list[DataField]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("data collection field names must be unique")
        return value


class HttpTool(StrictModel):
    type: Literal["http"]
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = Field(min_length=8, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    silent: bool = False

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        _validate_templates(value)
        sanitized = TEMPLATE_RE.sub("value", value)
        if not sanitized.startswith(("http://", "https://")):
            raise ValueError("HTTP tool URL must use http or https")
        return value

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for item in value.values():
            _validate_templates(item)
        return value


class RpcTool(StrictModel):
    type: Literal["rpc"]
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    preview_response: Any = None
    silent: bool = False


class McpTool(StrictModel):
    type: Literal["mcp"]
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        _validate_templates(value)
        if not TEMPLATE_RE.sub("value", value).startswith(("http://", "https://")):
            raise ValueError("MCP URL must use http or https")
        return value


ToolConfig = Annotated[Union[HttpTool, RpcTool, McpTool], Field(discriminator="type")]


class EndCallConfig(StrictModel):
    final_response: str = Field(default="", max_length=4000)
    delete_room: bool = False
    summary_enabled: bool = True
    summary_instructions: str = Field(default="", max_length=4000)
    result_endpoint: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("final_response", "summary_instructions")
    @classmethod
    def valid_text_template(cls, value: str) -> str:
        return _validate_templates(value)

    @field_validator("result_endpoint")
    @classmethod
    def valid_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        _validate_templates(value)
        if not TEMPLATE_RE.sub("value", value).startswith(("http://", "https://")):
            raise ValueError("result endpoint must use http or https")
        return value


class AgentSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$", max_length=120)
    instructions: str = Field(min_length=1, max_length=50_000)
    welcome_greeting: str = Field(default="", max_length=4000)
    models: ModelConfig
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    tools: list[ToolConfig] = Field(default_factory=list, max_length=50)
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    end_call: EndCallConfig = Field(default_factory=EndCallConfig)

    @field_validator("instructions", "welcome_greeting")
    @classmethod
    def valid_templates(cls, value: str) -> str:
        return _validate_templates(value)

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: list[ToolConfig]) -> list[ToolConfig]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return value


class RevisionConflictError(RuntimeError):
    pass


class BuilderService:
    def __init__(self, store: PlatformStore):
        self.store = store

    def save(
        self,
        *,
        project_id: str,
        actor_id: str,
        spec: AgentSpec,
        spec_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "agent.write")
        now = _utc_now()
        spec_json = spec.model_dump_json()
        with self.store.transaction() as conn:
            if spec_id:
                current = conn.execute(
                    "SELECT * FROM agent_specs WHERE id = ? AND project_id = ?",
                    (spec_id, project_id),
                ).fetchone()
                if current is None:
                    raise ResourceNotFoundError("agent spec not found")
                if expected_revision is None or int(current["revision"]) != expected_revision:
                    raise RevisionConflictError("agent spec revision conflict")
                revision = int(current["revision"]) + 1
                conn.execute(
                    """
                    UPDATE agent_specs
                    SET name = ?, revision = ?, status = 'draft', spec_json = ?,
                        updated_at = ?, published_at = NULL
                    WHERE id = ? AND project_id = ?
                    """,
                    (spec.name, revision, spec_json, now, spec_id, project_id),
                )
            else:
                spec_id = str(uuid.uuid4())
                revision = 1
                conn.execute(
                    """
                    INSERT INTO agent_specs (
                        id, project_id, name, revision, status, spec_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 'draft', ?, ?, ?)
                    """,
                    (spec_id, project_id, spec.name, spec_json, now, now),
                )
            conn.execute(
                """
                INSERT INTO agent_spec_revisions (
                    id, project_id, agent_spec_id, revision, spec_json, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), project_id, spec_id, revision, spec_json, now, actor_id),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="agent_spec.save",
                resource_type="agent_spec",
                resource_id=spec_id,
                payload={"revision": revision},
            )
            row = conn.execute("SELECT * FROM agent_specs WHERE id = ?", (spec_id,)).fetchone()
        return self._record(row)

    def publish(
        self, *, project_id: str, actor_id: str, spec_id: str, expected_revision: int
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "agent.write")
        now = _utc_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM agent_specs WHERE id = ? AND project_id = ?",
                (spec_id, project_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("agent spec not found")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError("agent spec revision conflict")
            conn.execute(
                """
                UPDATE agent_specs SET status = 'published', published_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, spec_id),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="agent_spec.publish",
                resource_type="agent_spec",
                resource_id=spec_id,
                payload={"revision": expected_revision},
            )
            updated = conn.execute("SELECT * FROM agent_specs WHERE id = ?", (spec_id,)).fetchone()
        return self._record(updated)

    def get(self, *, project_id: str, user_id: str, spec_id: str) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "agent.read")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_specs WHERE id = ? AND project_id = ?",
                (spec_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("agent spec not found")
        return self._record(row)

    def list(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "agent.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_specs WHERE project_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._record(item) for item in rows]

    def export_zip(self, *, project_id: str, user_id: str, spec_id: str) -> bytes:
        record = self.get(project_id=project_id, user_id=user_id, spec_id=spec_id)
        spec = AgentSpec.model_validate(record["spec"])
        spec_json = spec.model_dump_json(indent=2)
        agent_py = self._generate_agent_py(spec)
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("agent.py", agent_py)
            archive.writestr("agent-spec.json", spec_json)
            archive.writestr(
                "requirements.txt",
                "livekit-agents>=1.6.6\naiohttp>=3.10.0\n",
            )
            archive.writestr(
                "Dockerfile",
                "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nCMD [\"python\", \"agent.py\", \"start\"]\n",
            )
            archive.writestr(
                "README.md",
                f"# {spec.name}\n\nGenerated by the self-hosted Cloud-Parity Agent Builder.\n",
            )
        return output.getvalue()

    @staticmethod
    def _generate_agent_py(spec: AgentSpec) -> str:
        encoded = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)
        return f'''from __future__ import annotations

import json
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli

SPEC = json.loads({encoded!r})


class ConfiguredAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=SPEC["instructions"])


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    session = AgentSession(
        stt=SPEC["models"]["stt"],
        llm=SPEC["models"]["llm"],
        tts=SPEC["models"]["tts"],
    )
    await session.start(room=ctx.room, agent=ConfiguredAgent())
    if SPEC.get("welcome_greeting"):
        await session.generate_reply(instructions=SPEC["welcome_greeting"])


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=SPEC["name"]))
'''

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        if row is None:
            raise ResourceNotFoundError("agent spec not found")
        record = _row(row) or {}
        record["spec"] = json.loads(record.pop("spec_json"))
        return record
