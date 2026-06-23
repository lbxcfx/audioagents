from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dialogue import get_config, get_scene, handle_turn, list_scenes, start_session
from .db import connect, init_db, row_to_dict, rows_to_dicts, seed_db, seed_dialogue_db


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "qwen-telephony" / "server" / "static"

app = FastAPI(title="Audio Agents Operations API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)
    prompt: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=100)
    retry_limit: int = Field(default=1, ge=0, le=10)


class ContactCreate(BaseModel):
    name: str = ""
    phone: str = Field(min_length=3)
    tags: str = ""
    notes: str = ""


class CallCreate(BaseModel):
    campaign_id: int | None = None
    contact_id: int | None = None
    phone: str = Field(min_length=3)


class CallUpdate(BaseModel):
    status: Literal["pending", "dialing", "ringing", "active", "completed", "failed", "no_answer", "busy"] | None = None
    duration_sec: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None
    summary: str | None = None
    intent_level: Literal["unknown", "low", "medium", "high"] | None = None


class CallSimulation(BaseModel):
    event: Literal["ringing", "answer", "hangup", "no_answer", "busy", "failed"]
    duration_sec: int = Field(default=18, ge=0)
    summary: str = ""
    intent_level: Literal["unknown", "low", "medium", "high"] = "unknown"


class DialogueConfigUpdate(BaseModel):
    nlu_enabled: bool | None = None
    default_scene_id: int | None = None


class DialogueNluUpdate(BaseModel):
    enabled: bool


class DialogueSceneCreate(BaseModel):
    name: str = Field(min_length=1)
    industry: str = ""
    business_type: str = ""


class DialogueSceneUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    business_type: str | None = None
    status: Literal["draft", "published", "disabled"] | None = None
    flow: dict[str, Any] | None = None


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    keywords: str = ""
    sort_order: int = 10
    enabled: bool = True


class LabelRuleCreate(BaseModel):
    label: str = Field(min_length=1)
    priority: int = 10
    condition: dict[str, int] = Field(default_factory=dict)
    enabled: bool = True


class DialogueTurnRequest(BaseModel):
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    scene_id: int | None = None
    channel: str = "api"
    nlu_enabled: bool | None = None


class DialogueStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    scene_id: int | None = None


class DialogueMicroSipTestRequest(BaseModel):
    phone: str = "1000@127.0.0.1:5066"
    visible: bool = True


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_db()
    seed_dialogue_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dialogue/config")
def dialogue_config() -> dict:
    return get_config()


def _upsert_dialogue_config(*, nlu_enabled: bool | None = None, default_scene_id: int | None = None) -> dict:
    with connect() as conn:
        current = conn.execute("SELECT * FROM dialogue_config WHERE id = 1").fetchone()
        next_nlu_enabled = bool(current["nlu_enabled"]) if current else True
        next_default_scene_id = current["default_scene_id"] if current else None
        if nlu_enabled is not None:
            next_nlu_enabled = nlu_enabled
        if default_scene_id is not None:
            scene = conn.execute("SELECT id FROM dialogue_scenes WHERE id = ?", (default_scene_id,)).fetchone()
            if not scene:
                raise HTTPException(status_code=404, detail="Default dialogue scene not found")
            next_default_scene_id = default_scene_id
        conn.execute(
            """
            INSERT INTO dialogue_config (id, nlu_enabled, default_scene_id, updated_at)
            VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                nlu_enabled = excluded.nlu_enabled,
                default_scene_id = excluded.default_scene_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (1 if next_nlu_enabled else 0, next_default_scene_id),
        )
    return get_config()


@app.put("/api/dialogue/config")
def update_dialogue_config(payload: DialogueConfigUpdate) -> dict:
    payload_fields = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    if not payload_fields:
        raise HTTPException(status_code=400, detail="No config fields provided")
    if "nlu_enabled" in payload_fields and "default_scene_id" in payload_fields:
        raise HTTPException(
            status_code=400,
            detail="Use /api/dialogue/config/nlu or /api/dialogue/scenes/{scene_id}/default for decoupled updates",
        )
    if "nlu_enabled" in payload_fields:
        if payload.nlu_enabled is None:
            raise HTTPException(status_code=400, detail="nlu_enabled cannot be null")
        return _upsert_dialogue_config(nlu_enabled=payload.nlu_enabled)
    return _upsert_dialogue_config(default_scene_id=payload.default_scene_id)


@app.post("/api/dialogue/config/nlu")
def update_dialogue_nlu(payload: DialogueNluUpdate) -> dict:
    return _upsert_dialogue_config(nlu_enabled=payload.enabled)


@app.get("/api/dialogue/scenes")
def dialogue_scenes() -> list[dict]:
    return list_scenes()


@app.post("/api/dialogue/scenes")
def create_dialogue_scene(payload: DialogueSceneCreate) -> dict:
    default_flow = {
        "entry_node": "start",
        "max_turns": 10,
        "unknown_route": "fallback",
        "nodes": [
            {
                "id": "start",
                "type": "scene",
                "name": "主流程开场白",
                "text": "您好，我是智能客服助手。请问您现在方便沟通吗？",
                "routes": {
                    "positive": "intro",
                    "negative": "fallback",
                    "reject": "hangup",
                    "neutral": "intro",
                    "unknown": "fallback",
                },
            },
            {
                "id": "intro",
                "type": "scene",
                "name": "业务介绍",
                "text": "好的，我简单介绍一下我们的服务。您想先了解功能、价格还是接入方式？",
                "routes": {
                    "positive": "end",
                    "negative": "fallback",
                    "reject": "hangup",
                    "neutral": "end",
                    "unknown": "fallback",
                },
            },
            {"id": "fallback", "type": "llm_fallback", "name": "LLM 兜底", "text": ""},
            {"id": "hangup", "type": "end", "name": "挂机", "text": "好的，那就不打扰您了。"},
            {"id": "end", "type": "end", "name": "结束", "text": "好的，感谢您的时间。"},
        ],
    }
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO dialogue_scenes (name, industry, business_type, status)
            VALUES (?, ?, ?, 'draft')
            """,
            (payload.name, payload.industry, payload.business_type),
        )
        scene_id = cur.lastrowid
        version = conn.execute(
            """
            INSERT INTO dialogue_versions (scene_id, version, status, flow_json)
            VALUES (?, 1, 'draft', ?)
            """,
            (scene_id, json.dumps(default_flow, ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE dialogue_scenes SET active_version_id = ? WHERE id = ?",
            (version.lastrowid, scene_id),
        )
    scene = get_scene(scene_id)
    return scene or {}


@app.get("/api/dialogue/scenes/{scene_id}")
def read_dialogue_scene(scene_id: int) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    return scene


def validate_flow(flow: dict[str, Any]) -> None:
    nodes = flow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=400, detail="Flow must contain nodes")
    node_ids = {node.get("id") for node in nodes}
    if not flow.get("entry_node") or flow["entry_node"] not in node_ids:
        raise HTTPException(status_code=400, detail="entry_node is missing or invalid")
    for node in nodes:
        if not node.get("id") or not node.get("type") or not node.get("name"):
            raise HTTPException(status_code=400, detail="Every node needs id, type, and name")
        if node.get("type") == "scene":
            routes = node.get("routes") or {}
            if "unknown" not in routes:
                raise HTTPException(status_code=400, detail=f"Node {node['id']} must define unknown route")
            for target in routes.values():
                if target and target not in node_ids:
                    raise HTTPException(status_code=400, detail=f"Route target {target} does not exist")


def _prewarm_dialogue_audio(scene_id: int) -> bool:
    script = ROOT / "qwen-telephony" / "scripts" / "prewarm-dialogue-audio.py"
    if not script.exists():
        return False
    python = ROOT / "qwen-telephony" / ".venv-win" / "Scripts" / "python.exe"
    python_cmd = str(python) if python.exists() else sys.executable

    log_dir = ROOT / "qwen-telephony" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"dialogue-audio-prewarm-{scene_id}.out.log"
    err_path = log_dir / f"dialogue-audio-prewarm-{scene_id}.err.log"
    env = os.environ.copy()
    env.setdefault("QWEN_TTS_USE_SSE", "false")
    env.setdefault("QWEN_TTS_CACHE_ENABLED", "true")
    with out_path.open("ab") as out_file, err_path.open("ab") as err_file:
        subprocess.Popen(
            [python_cmd, str(script), "--scene-id", str(scene_id)],
            cwd=str(ROOT),
            env=env,
            stdout=out_file,
            stderr=err_file,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
        )
    return True


@app.put("/api/dialogue/scenes/{scene_id}")
def update_dialogue_scene(scene_id: int, payload: DialogueSceneUpdate) -> dict:
    if payload.flow is not None:
        validate_flow(payload.flow)
    with connect() as conn:
        scene = conn.execute("SELECT * FROM dialogue_scenes WHERE id = ?", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="Dialogue scene not found")
        if payload.name is not None or payload.industry is not None or payload.business_type is not None or payload.status is not None:
            conn.execute(
                """
                UPDATE dialogue_scenes
                SET name = COALESCE(?, name),
                    industry = COALESCE(?, industry),
                    business_type = COALESCE(?, business_type),
                    status = COALESCE(?, status),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payload.name, payload.industry, payload.business_type, payload.status, scene_id),
            )
        if payload.flow is not None:
            active_version_id = scene["active_version_id"]
            conn.execute(
                "UPDATE dialogue_versions SET flow_json = ? WHERE id = ?",
                (json.dumps(payload.flow, ensure_ascii=False), active_version_id),
            )
    updated = get_scene(scene_id)
    return updated or {}


@app.post("/api/dialogue/scenes/{scene_id}/publish")
def publish_dialogue_scene(scene_id: int) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    validate_flow(scene["flow"])
    with connect() as conn:
        conn.execute("UPDATE dialogue_scenes SET status = 'published', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (scene_id,))
        conn.execute("UPDATE dialogue_versions SET status = 'published', published_at = CURRENT_TIMESTAMP WHERE id = ?", (scene["active_version_id"],))
    updated = get_scene(scene_id) or {}
    updated["audio_prewarm_started"] = _prewarm_dialogue_audio(scene_id)
    return updated


@app.post("/api/dialogue/scenes/{scene_id}/default")
def set_default_dialogue_scene(scene_id: int) -> dict:
    return _upsert_dialogue_config(default_scene_id=scene_id)


def _microsip_exe() -> Path:
    return ROOT / "tools" / "microsip" / "MicroSIP.exe"


def _agent_worker_reachable() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:18081/worker", timeout=2) as response:
            return response.status < 500
    except Exception:
        return False


def _livekit_http_url() -> str:
    value = os.getenv("LIVEKIT_HTTP_URL") or os.getenv("LIVEKIT_URL") or "http://127.0.0.1:7880"
    if value.startswith("ws://"):
        return "http://" + value.removeprefix("ws://")
    if value.startswith("wss://"):
        return "https://" + value.removeprefix("wss://")
    return value


async def _create_agent_dispatch_async(scene_id: int, room_name: str) -> dict[str, str]:
    from livekit import api

    agent_name = os.getenv("QWEN_AGENT_EXPLICIT_NAME") or os.getenv("LIVEKIT_AGENT_NAME") or ""
    if not agent_name:
        return {}
    lkapi = api.LiveKitAPI(
        url=_livekit_http_url(),
        api_key=os.getenv("LIVEKIT_API_KEY", "devkey"),
        api_secret=os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    try:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room_name,
                metadata=json.dumps({"scene_id": scene_id, "source": "microsip-test"}, ensure_ascii=False),
                attributes={
                    "qwen.dialogue_scene_id": str(scene_id),
                    "qwen.test_source": "microsip",
                },
            )
        )
        return {"id": dispatch.id, "agent_name": dispatch.agent_name, "room": dispatch.room}
    finally:
        await lkapi.aclose()


def _create_agent_dispatch(scene_id: int, room_name: str) -> dict[str, str] | None:
    try:
        return asyncio.run(_create_agent_dispatch_async(scene_id, room_name))
    except Exception:
        return None


@app.post("/api/dialogue/scenes/{scene_id}/microsip-test")
def microsip_test(scene_id: int, payload: DialogueMicroSipTestRequest) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO calls (phone, status, room_name, summary, intent_level)
            VALUES (?, 'dialing', ?, ?, 'unknown')
            """,
            (
                payload.phone,
                f"microsip-script-test-{scene_id}",
                f"话术发布后 MicroSIP 测试：{scene['name']}",
            ),
        )
        call_id = cur.lastrowid
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    exe = _microsip_exe()
    prerequisites = {
        "microsip_exists": exe.exists(),
        "agent_worker_reachable": _agent_worker_reachable(),
        "windows": platform.system().lower() == "windows",
    }
    manual = {
        "start_system": "qwen-telephony/scripts/start-system.ps1",
        "start_microsip": "qwen-telephony/scripts/start-microsip.ps1",
        "dial": f"sip:{payload.phone}",
        "agent_scene": f"QWEN_DIALOGUE_SCENE_ID={scene_id} 或使用默认场景 {scene_id}",
    }

    dispatch = None
    if not all(prerequisites.values()):
        return {
            "call": row_to_dict(row),
            "started": False,
            "message": "MicroSIP 测试任务已创建，但当前环境不能自动启动 MicroSIP。请按 manual 指令手动测试。",
            "prerequisites": prerequisites,
            "manual": manual,
        }

    dispatch = _create_agent_dispatch(scene_id, "qwen-phone-room")

    try:
        startupinfo = None
        creationflags = 0
        if not payload.visible:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [str(exe), payload.phone],
            cwd=str(exe.parent),
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {
            "call": row_to_dict(row),
            "started": False,
            "message": f"MicroSIP 测试任务已创建，但 MicroSIP 启动失败：{exc}",
            "prerequisites": prerequisites,
            "manual": manual,
            "dispatch": dispatch,
        }

    return {
        "call": row_to_dict(row),
        "started": True,
        "message": f"已使用 MicroSIP 呼叫 sip:{payload.phone}。接通后请按话术进行语音测试。",
        "prerequisites": prerequisites,
        "manual": manual,
        "dispatch": dispatch,
    }


@app.post("/api/dialogue/scenes/{scene_id}/knowledge")
def create_knowledge(scene_id: int, payload: KnowledgeCreate) -> dict:
    if not get_scene(scene_id):
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO knowledge_items (scene_id, title, answer, keywords, sort_order, enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scene_id, payload.title, payload.answer, payload.keywords, payload.sort_order, 1 if payload.enabled else 0),
        )
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.put("/api/dialogue/knowledge/{item_id}")
def update_knowledge(item_id: int, payload: KnowledgeCreate) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Knowledge item not found")
        conn.execute(
            """
            UPDATE knowledge_items
            SET title = ?, answer = ?, keywords = ?, sort_order = ?, enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.title, payload.answer, payload.keywords, payload.sort_order, 1 if payload.enabled else 0, item_id),
        )
        updated = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    return row_to_dict(updated)


@app.delete("/api/dialogue/knowledge/{item_id}")
def delete_knowledge(item_id: int) -> dict:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_items WHERE id = ?", (item_id,))
    return {"deleted": item_id}


@app.post("/api/dialogue/scenes/{scene_id}/label-rules")
def create_label_rule(scene_id: int, payload: LabelRuleCreate) -> dict:
    if not get_scene(scene_id):
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO intent_label_rules (scene_id, label, priority, condition_json, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scene_id, payload.label, payload.priority, json.dumps(payload.condition, ensure_ascii=False), 1 if payload.enabled else 0),
        )
        row = conn.execute("SELECT * FROM intent_label_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.delete("/api/dialogue/label-rules/{rule_id}")
def delete_label_rule(rule_id: int) -> dict:
    with connect() as conn:
        conn.execute("DELETE FROM intent_label_rules WHERE id = ?", (rule_id,))
    return {"deleted": rule_id}


@app.get("/api/dialogue/unresolved")
def list_unresolved(scene_id: int | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if scene_id:
        clauses.append("scene_id = ?")
        params.append(scene_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM unresolved_questions {where} ORDER BY hit_count DESC, last_seen_at DESC LIMIT 100",
            params,
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/dialogue/turn")
def dialogue_turn(payload: DialogueTurnRequest) -> dict:
    return handle_turn(
        session_id=payload.session_id,
        text=payload.text,
        scene_id=payload.scene_id,
        channel=payload.channel,
        nlu_enabled_override=payload.nlu_enabled,
    )


@app.post("/api/dialogue/start")
def dialogue_start(payload: DialogueStartRequest) -> dict:
    return start_session(session_id=payload.session_id, scene_id=payload.scene_id)


@app.get("/api/dashboard")
def dashboard() -> dict:
    with connect() as conn:
        total_calls = conn.execute("SELECT COUNT(*) AS value FROM calls").fetchone()["value"]
        active_calls = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE status = 'active'").fetchone()["value"]
        completed = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE status = 'completed'").fetchone()["value"]
        failed = conn.execute(
            "SELECT COUNT(*) AS value FROM calls WHERE status IN ('failed', 'no_answer', 'busy')"
        ).fetchone()["value"]
        high_intent = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE intent_level = 'high'").fetchone()["value"]
        avg_duration = conn.execute("SELECT COALESCE(AVG(duration_sec), 0) AS value FROM calls WHERE duration_sec > 0").fetchone()["value"]
        status_rows = conn.execute("SELECT status, COUNT(*) AS total FROM calls GROUP BY status ORDER BY total DESC").fetchall()
        intent_rows = conn.execute("SELECT intent_level, COUNT(*) AS total FROM calls GROUP BY intent_level ORDER BY total DESC").fetchall()
        recent_calls = conn.execute(
            """
            SELECT calls.*, contacts.name AS contact_name, campaigns.name AS campaign_name
            FROM calls
            LEFT JOIN contacts ON contacts.id = calls.contact_id
            LEFT JOIN campaigns ON campaigns.id = calls.campaign_id
            ORDER BY calls.id DESC
            LIMIT 8
            """
        ).fetchall()

    answer_rate = round((completed / total_calls) * 100, 1) if total_calls else 0
    return {
        "kpis": {
            "total_calls": total_calls,
            "answer_rate": answer_rate,
            "avg_duration": round(avg_duration or 0),
            "high_intent": high_intent,
            "failed": failed,
            "active_calls": active_calls,
        },
        "status_distribution": rows_to_dicts(status_rows),
        "intent_distribution": rows_to_dicts(intent_rows),
        "recent_calls": rows_to_dicts(recent_calls),
    }


@app.get("/api/campaigns")
def list_campaigns() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT campaigns.*,
                   COUNT(calls.id) AS call_count,
                   SUM(CASE WHEN calls.status = 'completed' THEN 1 ELSE 0 END) AS completed_count
            FROM campaigns
            LEFT JOIN calls ON calls.campaign_id = campaigns.id
            GROUP BY campaigns.id
            ORDER BY campaigns.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/campaigns")
def create_campaign(payload: CampaignCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO campaigns (name, status, prompt, max_concurrency, retry_limit)
            VALUES (?, 'draft', ?, ?, ?)
            """,
            (payload.name, payload.prompt, payload.max_concurrency, payload.retry_limit),
        )
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/contacts")
def list_contacts() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    return rows_to_dicts(rows)


@app.post("/api/contacts")
def create_contact(payload: ContactCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO contacts (name, phone, tags, notes) VALUES (?, ?, ?, ?)",
            (payload.name, payload.phone, payload.tags, payload.notes),
        )
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/calls")
def list_calls(status: str | None = None, campaign_id: int | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("calls.status = ?")
        params.append(status)
    if campaign_id:
        clauses.append("calls.campaign_id = ?")
        params.append(campaign_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT calls.*, contacts.name AS contact_name, campaigns.name AS campaign_name
            FROM calls
            LEFT JOIN contacts ON contacts.id = calls.contact_id
            LEFT JOIN campaigns ON campaigns.id = calls.campaign_id
            {where}
            ORDER BY calls.id DESC
            """,
            params,
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/calls")
def create_call(payload: CallCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (campaign_id, contact_id, phone, status) VALUES (?, ?, ?, 'pending')",
            (payload.campaign_id, payload.contact_id, payload.phone),
        )
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.patch("/api/calls/{call_id}")
def update_call(call_id: int, payload: CallUpdate) -> dict:
    updates = {key: value for key, value in payload.dict().items() if value is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    assignments = ", ".join(f"{key} = ?" for key in updates)
    params = list(updates.values()) + [call_id]
    with connect() as conn:
        conn.execute(f"UPDATE calls SET {assignments} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    return row_to_dict(row)


@app.post("/api/campaigns/{campaign_id}/enqueue")
def enqueue_campaign(campaign_id: int) -> dict:
    """Create pending call rows for campaign contacts that do not already have calls."""
    with connect() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        contacts = conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
        created = 0
        for contact in contacts:
            exists = conn.execute(
                "SELECT 1 FROM calls WHERE campaign_id = ? AND contact_id = ?",
                (campaign_id, contact["id"]),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO calls (campaign_id, contact_id, phone, status) VALUES (?, ?, ?, 'pending')",
                (campaign_id, contact["id"], contact["phone"]),
            )
            created += 1
        conn.execute("UPDATE campaigns SET status = 'queued', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (campaign_id,))
    return {"campaign_id": campaign_id, "created_calls": created}


@app.post("/api/calls/{call_id}/dial")
def dial_call(call_id: int) -> dict:
    """MVP placeholder for LiveKit SIP outbound dialing."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        room_name = row["room_name"] or f"qwen-outbound-{call_id}"
        conn.execute(
            "UPDATE calls SET status = 'dialing', room_name = ? WHERE id = ?",
            (room_name, call_id),
        )
        updated = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return {
        "call": row_to_dict(updated),
        "message": "Dial request queued. Configure an outbound SIP trunk to place real phone calls.",
    }


@app.post("/api/calls/{call_id}/simulate")
def simulate_call(call_id: int, payload: CallSimulation) -> dict:
    """Simulate MicroSIP-side call events when no real outbound SIP trunk is available."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")

        room_name = row["room_name"] or f"qwen-outbound-{call_id}"
        if payload.event == "ringing":
            conn.execute(
                "UPDATE calls SET status = 'ringing', room_name = ? WHERE id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP ringing."
        elif payload.event == "answer":
            conn.execute(
                "UPDATE calls SET status = 'active', room_name = ?, started_at = COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP answered."
        elif payload.event == "hangup":
            conn.execute(
                """
                UPDATE calls
                SET status = 'completed',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    duration_sec = ?,
                    summary = ?,
                    intent_level = ?
                WHERE id = ?
                """,
                (
                    room_name,
                    payload.duration_sec,
                    payload.summary or "MicroSIP 模拟通话已完成",
                    payload.intent_level,
                    call_id,
                ),
            )
            message = "Simulated MicroSIP hangup."
        elif payload.event == "no_answer":
            conn.execute(
                """
                UPDATE calls
                SET status = 'no_answer',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟无人接听'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            message = "Simulated MicroSIP no answer."
        elif payload.event == "busy":
            conn.execute(
                """
                UPDATE calls
                SET status = 'busy',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟忙线'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            message = "Simulated MicroSIP busy."
        else:
            conn.execute(
                """
                UPDATE calls
                SET status = 'failed',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟失败'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            message = "Simulated MicroSIP failure."

        updated = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    return {"call": row_to_dict(updated), "message": message}
