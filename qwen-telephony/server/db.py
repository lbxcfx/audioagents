from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "qwen-telephony" / "data" / "ops.sqlite3"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows if row is not None]


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                prompt TEXT NOT NULL DEFAULT '',
                max_concurrency INTEGER NOT NULL DEFAULT 2,
                retry_limit INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                contact_id INTEGER,
                scene_id INTEGER,
                caller_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                room_name TEXT NOT NULL DEFAULT '',
                sip_participant_id TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                ended_at TEXT,
                duration_sec INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                intent_level TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL,
                FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
                FOREIGN KEY(scene_id) REFERENCES dialogue_scenes(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS call_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(call_id) REFERENCES calls(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dialogue_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                nlu_enabled INTEGER NOT NULL DEFAULT 1,
                default_scene_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dialogue_scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                business_type TEXT NOT NULL DEFAULT '',
                script_type TEXT NOT NULL DEFAULT 'common',
                auto_break TEXT NOT NULL DEFAULT '否',
                audit_status TEXT NOT NULL DEFAULT '待审核',
                ui_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                active_version_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dialogue_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_id INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                flow_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                published_at TEXT,
                FOREIGN KEY(scene_id) REFERENCES dialogue_scenes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                answer TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                hit_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scene_id) REFERENCES dialogue_scenes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS intent_label_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 10,
                condition_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(scene_id) REFERENCES dialogue_scenes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS unresolved_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_id INTEGER,
                user_text TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dialogue_sessions (
                id TEXT PRIMARY KEY,
                scene_id INTEGER NOT NULL,
                current_node_id TEXT NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0,
                fallback_count INTEGER NOT NULL DEFAULT 0,
                positive_count INTEGER NOT NULL DEFAULT 0,
                negative_count INTEGER NOT NULL DEFAULT 0,
                reject_count INTEGER NOT NULL DEFAULT 0,
                neutral_count INTEGER NOT NULL DEFAULT 0,
                knowledge_hit_count INTEGER NOT NULL DEFAULT 0,
                label TEXT NOT NULL DEFAULT '',
                slots_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scene_id) REFERENCES dialogue_scenes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dialogue_turn_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                scene_id INTEGER,
                user_text TEXT NOT NULL,
                route_type TEXT NOT NULL,
                response_text TEXT NOT NULL DEFAULT '',
                current_node_id TEXT NOT NULL DEFAULT '',
                next_node_id TEXT NOT NULL DEFAULT '',
                nlu_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS task_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                default_prompt TEXT NOT NULL DEFAULT '',
                max_concurrency INTEGER NOT NULL DEFAULT 2,
                retry_limit INTEGER NOT NULL DEFAULT 1,
                default_scene_id INTEGER,
                status TEXT NOT NULL DEFAULT 'enabled',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(default_scene_id) REFERENCES dialogue_scenes(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS dispatch_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                call_id INTEGER,
                phone TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                dispatch_type TEXT NOT NULL DEFAULT 'LiveKit队列',
                status TEXT NOT NULL DEFAULT 'pending',
                room_name TEXT NOT NULL DEFAULT '',
                failure_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL,
                FOREIGN KEY(call_id) REFERENCES calls(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS push_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                target TEXT NOT NULL DEFAULT '',
                push_type TEXT NOT NULL DEFAULT 'Webhook',
                content TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                failure_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
            );
            """
        )
        call_columns = {row["name"] for row in conn.execute("PRAGMA table_info(calls)").fetchall()}
        if "scene_id" not in call_columns:
            conn.execute("ALTER TABLE calls ADD COLUMN scene_id INTEGER")
        if "caller_name" not in call_columns:
            conn.execute("ALTER TABLE calls ADD COLUMN caller_name TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE calls SET caller_name = '测试号' WHERE phone = '1000@127.0.0.1:5066' AND caller_name = ''")
        scene_columns = {row["name"] for row in conn.execute("PRAGMA table_info(dialogue_scenes)").fetchall()}
        if "script_type" not in scene_columns:
            conn.execute("ALTER TABLE dialogue_scenes ADD COLUMN script_type TEXT NOT NULL DEFAULT 'common'")
        if "auto_break" not in scene_columns:
            conn.execute("ALTER TABLE dialogue_scenes ADD COLUMN auto_break TEXT NOT NULL DEFAULT '否'")
        if "audit_status" not in scene_columns:
            conn.execute("ALTER TABLE dialogue_scenes ADD COLUMN audit_status TEXT NOT NULL DEFAULT '待审核'")
        if "ui_json" not in scene_columns:
            conn.execute("ALTER TABLE dialogue_scenes ADD COLUMN ui_json TEXT NOT NULL DEFAULT '{}'")
        template_count = conn.execute("SELECT COUNT(*) AS total FROM task_templates").fetchone()["total"]
        if not template_count:
            conn.execute(
                """
                INSERT INTO task_templates (name, default_prompt, max_concurrency, retry_limit, status, notes)
                VALUES (?, ?, 2, 1, 'enabled', ?)
                """,
                ("默认外呼模板", "你是电话回访助手，确认客户是否需要产品演示，并记录意向等级。", "系统默认任务模板"),
            )
        conn.execute(
            """
            INSERT INTO dispatch_records (
                campaign_id, call_id, phone, contact_name,
                dispatch_type, status, room_name, failure_reason, created_at, updated_at
            )
            SELECT calls.campaign_id,
                   calls.id,
                   calls.phone,
                   COALESCE(contacts.name, calls.caller_name, ''),
                   'LiveKit队列',
                   CASE
                       WHEN calls.status = 'pending' THEN 'pending'
                       WHEN calls.status IN ('failed', 'no_answer', 'busy') THEN 'failed'
                       WHEN calls.status IN ('dialing', 'ringing', 'active') THEN 'active'
                       ELSE 'completed'
                   END,
                   calls.room_name,
                   calls.failure_reason,
                   calls.created_at,
                   CURRENT_TIMESTAMP
            FROM calls
            LEFT JOIN contacts ON contacts.id = calls.contact_id
            WHERE calls.campaign_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM dispatch_records WHERE dispatch_records.call_id = calls.id
              )
            """
        )


def seed_db() -> None:
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS total FROM campaigns").fetchone()["total"]
        if count:
            return

        cursor = conn.execute(
            """
            INSERT INTO campaigns (name, status, prompt, max_concurrency, retry_limit)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "6月客户回访",
                "running",
                "你是电话回访助手，确认客户是否需要产品演示，并记录意向等级。",
                3,
                1,
            ),
        )
        campaign_id = cursor.lastrowid

        contacts = [
            ("张女士", "13800000001", "重点客户,华东", "上周提交过试用申请"),
            ("李先生", "13800000002", "新线索", "来自官网表单"),
            ("王经理", "13800000003", "企业客户", "需要下午联系"),
            ("赵总", "13800000004", "高价值", "关注部署方案"),
        ]
        contact_ids: list[int] = []
        for contact in contacts:
            cur = conn.execute(
                "INSERT INTO contacts (name, phone, tags, notes) VALUES (?, ?, ?, ?)",
                contact,
            )
            contact_ids.append(cur.lastrowid)

        calls = [
            (campaign_id, contact_ids[0], "13800000001", "completed", "qwen-call-1", "", 186, "", "客户希望明天下午安排产品演示。", "high"),
            (campaign_id, contact_ids[1], "13800000002", "no_answer", "qwen-call-2", "", 0, "无人接听", "", "unknown"),
            (campaign_id, contact_ids[2], "13800000003", "active", "qwen-call-3", "", 72, "", "正在确认需求。", "medium"),
            (campaign_id, contact_ids[3], "13800000004", "pending", "", "", 0, "", "", "unknown"),
        ]
        for call in calls:
            cur = conn.execute(
                """
                INSERT INTO calls (
                    campaign_id, contact_id, phone, status, room_name, sip_participant_id,
                    duration_sec, failure_reason, summary, intent_level
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                call,
            )
            if call[3] == "completed":
                conn.executemany(
                    "INSERT INTO call_messages (call_id, role, text) VALUES (?, ?, ?)",
                    [
                        (cur.lastrowid, "assistant", "您好，我是智能回访助手，想了解您是否需要产品演示。"),
                        (cur.lastrowid, "user", "可以，明天下午联系我。"),
                        (cur.lastrowid, "assistant", "好的，我已经记录明天下午回访。"),
                    ],
                )

        flow_json = """
        {
          "entry_node": "start",
          "max_turns": 10,
          "unknown_route": "fallback",
          "nodes": [
            {
              "id": "start",
              "type": "scene",
              "name": "主流程开场白",
              "text": "您好，我是智能客服助手。请问您现在方便了解一下我们的服务吗？",
              "routes": {
                "positive": "intro",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "clarify",
                "unknown": "fallback"
              }
            },
            {
              "id": "intro",
              "type": "scene",
              "name": "企业介绍",
              "text": "我们主要提供企业客户的语音 AI 服务，可以接入外呼、客服问答和业务系统。您更关心功能还是价格？",
              "routes": {
                "positive": "feature",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "feature",
                "unknown": "fallback"
              }
            },
            {
              "id": "feature",
              "type": "scene",
              "name": "功能说明",
              "text": "系统支持 ASR、固定话术状态机、知识库命中、LLM 兜底和 TTS 播报。固定问题会优先走话术链路，响应更快。",
              "routes": {
                "positive": "end",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "end",
                "unknown": "fallback"
              }
            },
            {
              "id": "clarify",
              "type": "scene",
              "name": "澄清节点",
              "text": "我理解了。为了更准确地帮您处理，您可以简单说一下想咨询的是产品、价格还是接入方式。",
              "routes": {
                "positive": "feature",
                "negative": "hangup",
                "reject": "hangup",
                "neutral": "fallback",
                "unknown": "fallback"
              }
            },
            {
              "id": "fallback",
              "type": "llm_fallback",
              "name": "LLM 兜底",
              "text": ""
            },
            {
              "id": "hangup",
              "type": "end",
              "name": "挂机",
              "text": "好的，那就不打扰您了，祝您生活愉快。"
            },
            {
              "id": "end",
              "type": "end",
              "name": "结束",
              "text": "好的，信息我已经记录。后续如需继续了解，可以随时联系我们。"
            }
          ]
        }
        """
        scene = conn.execute(
            """
            INSERT INTO dialogue_scenes (name, industry, business_type, status)
            VALUES (?, ?, ?, 'published')
            """,
            ("AI语音客服标准话术", "企业服务", "售前咨询"),
        )
        scene_id = scene.lastrowid
        version = conn.execute(
            """
            INSERT INTO dialogue_versions (scene_id, version, status, flow_json, published_at)
            VALUES (?, 1, 'published', ?, CURRENT_TIMESTAMP)
            """,
            (scene_id, flow_json),
        )
        conn.execute(
            "UPDATE dialogue_scenes SET active_version_id = ? WHERE id = ?",
            (version.lastrowid, scene_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dialogue_config (id, nlu_enabled, default_scene_id) VALUES (1, 1, ?)",
            (scene_id,),
        )
        conn.executemany(
            """
            INSERT INTO knowledge_items (scene_id, title, answer, keywords, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    scene_id,
                    "怎么接入",
                    "接入方式通常是 LiveKit 负责实时音频，ASR 后先进入话术状态机，未命中时再走 LLM，最后用 TTS 播报。",
                    "接入,怎么接,如何接入,livekit,链路",
                    10,
                ),
                (
                    scene_id,
                    "速度优势",
                    "固定话术命中时不需要等待大模型生成，通常能把响应控制在几十毫秒到一百毫秒级，再直接进入 TTS。",
                    "速度,延迟,快不快,响应,性能",
                    20,
                ),
                (
                    scene_id,
                    "可关闭 NLU",
                    "可以关闭 NLU 开关。关闭后链路会保持原来的 ASR、LLM、TTS，不走状态机和知识库。",
                    "关闭,nlu,开关,原链路,LLM",
                    30,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO intent_label_rules (scene_id, label, priority, condition_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                (scene_id, "A类", 1, '{"knowledge_hit_count": 2, "positive_count": 1}'),
                (scene_id, "B类", 2, '{"positive_count": 1}'),
                (scene_id, "C类", 3, '{"reject_count": 1}'),
            ],
        )


def seed_dialogue_db() -> None:
    with connect() as conn:
        dialogue_count = conn.execute("SELECT COUNT(*) AS total FROM dialogue_scenes").fetchone()["total"]
        if dialogue_count:
            return

        flow_json = """
        {
          "entry_node": "start",
          "max_turns": 10,
          "unknown_route": "fallback",
          "nodes": [
            {
              "id": "start",
              "type": "scene",
              "name": "主流程开场白",
              "text": "您好，我是智能客服助手。请问您现在方便了解一下我们的服务吗？",
              "routes": {
                "positive": "intro",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "clarify",
                "unknown": "fallback"
              }
            },
            {
              "id": "intro",
              "type": "scene",
              "name": "企业介绍",
              "text": "我们主要提供企业客户的语音 AI 服务，可以接入外呼、客服问答和业务系统。您更关心功能还是价格？",
              "routes": {
                "positive": "feature",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "feature",
                "unknown": "fallback"
              }
            },
            {
              "id": "feature",
              "type": "scene",
              "name": "功能说明",
              "text": "系统支持 ASR、固定话术状态机、知识库命中、LLM 兜底和 TTS 播报。固定问题会优先走话术链路，响应更快。",
              "routes": {
                "positive": "end",
                "negative": "clarify",
                "reject": "hangup",
                "neutral": "end",
                "unknown": "fallback"
              }
            },
            {
              "id": "clarify",
              "type": "scene",
              "name": "澄清节点",
              "text": "我理解了。为了更准确地帮您处理，您可以简单说一下想咨询的是产品、价格还是接入方式。",
              "routes": {
                "positive": "feature",
                "negative": "hangup",
                "reject": "hangup",
                "neutral": "fallback",
                "unknown": "fallback"
              }
            },
            {"id": "fallback", "type": "llm_fallback", "name": "LLM 兜底", "text": ""},
            {"id": "hangup", "type": "end", "name": "挂机", "text": "好的，那就不打扰您了，祝您生活愉快。"},
            {"id": "end", "type": "end", "name": "结束", "text": "好的，信息我已经记录。后续如需继续了解，可以随时联系我们。"}
          ]
        }
        """
        scene = conn.execute(
            """
            INSERT INTO dialogue_scenes (name, industry, business_type, status)
            VALUES (?, ?, ?, 'published')
            """,
            ("AI语音客服标准话术", "企业服务", "售前咨询"),
        )
        scene_id = scene.lastrowid
        version = conn.execute(
            """
            INSERT INTO dialogue_versions (scene_id, version, status, flow_json, published_at)
            VALUES (?, 1, 'published', ?, CURRENT_TIMESTAMP)
            """,
            (scene_id, flow_json),
        )
        conn.execute(
            "UPDATE dialogue_scenes SET active_version_id = ? WHERE id = ?",
            (version.lastrowid, scene_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dialogue_config (id, nlu_enabled, default_scene_id) VALUES (1, 1, ?)",
            (scene_id,),
        )
        conn.executemany(
            """
            INSERT INTO knowledge_items (scene_id, title, answer, keywords, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    scene_id,
                    "怎么接入",
                    "接入方式通常是 LiveKit 负责实时音频，ASR 后先进入话术状态机，未命中时再走 LLM，最后用 TTS 播报。",
                    "接入,怎么接,如何接入,livekit,链路",
                    10,
                ),
                (
                    scene_id,
                    "速度优势",
                    "固定话术命中时不需要等待大模型生成，通常能把响应控制在几十毫秒到一百毫秒级，再直接进入 TTS。",
                    "速度,延迟,快不快,响应,性能",
                    20,
                ),
                (
                    scene_id,
                    "可关闭 NLU",
                    "可以关闭 NLU 开关。关闭后链路会保持原来的 ASR、LLM、TTS，不走状态机和知识库。",
                    "关闭,nlu,开关,原链路,LLM",
                    30,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO intent_label_rules (scene_id, label, priority, condition_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                (scene_id, "A类", 1, '{"knowledge_hit_count": 2, "positive_count": 1}'),
                (scene_id, "B类", 2, '{"positive_count": 1}'),
                (scene_id, "C类", 3, '{"reject_count": 1}'),
            ],
        )
