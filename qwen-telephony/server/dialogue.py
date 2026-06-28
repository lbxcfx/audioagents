from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .db import connect, row_to_dict, rows_to_dicts


POSITIVE_WORDS = ("是", "对", "可以", "方便", "需要", "想", "好", "嗯", "行", "了解", "继续")
NEGATIVE_WORDS = ("不是", "不用", "不需要", "没兴趣", "不方便", "算了", "不要")
REJECT_WORDS = ("别打", "挂了", "投诉", "拉黑", "拒绝", "不打扰", "再见")
NEUTRAL_WORDS = ("什么", "怎么", "多少", "哪里", "哪个", "介绍", "说一下", "讲讲")


@dataclass
class NluResult:
    intent: str
    confidence: float
    matched_keyword: str = ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def split_keywords(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、\n;；]", value or "") if item.strip()]


def classify_intent(text: str, node: dict[str, Any] | None = None) -> NluResult:
    normalized = normalize_text(text)
    if not normalized:
        return NluResult("unknown", 0.0)

    node_keywords = (node or {}).get("intent_keywords") or {}
    if isinstance(node_keywords, dict):
        standard_confidence = {
            "reject": 0.94,
            "negative": 0.86,
            "positive": 0.84,
            "neutral": 0.74,
            "unknown": 0.56,
        }
        ordered_intents = ["reject", "negative", "positive", "neutral", "unknown"]
        custom_intents = [intent for intent in node_keywords.keys() if intent not in ordered_intents]
        for intent in [*ordered_intents, *custom_intents]:
            raw_keywords = node_keywords.get(intent, [])
            if isinstance(raw_keywords, list):
                keywords: list[str] = []
                for item in raw_keywords:
                    keywords.extend(split_keywords(str(item or "")))
            else:
                keywords = split_keywords(str(raw_keywords or ""))
            for word in keywords:
                if normalize_text(word) in normalized:
                    return NluResult(intent, standard_confidence.get(intent, 0.8), word)

    for intent, words, confidence in (
        ("reject", REJECT_WORDS, 0.92),
        ("negative", NEGATIVE_WORDS, 0.82),
        ("positive", POSITIVE_WORDS, 0.78),
        ("neutral", NEUTRAL_WORDS, 0.68),
    ):
        for word in words:
            if word in normalized:
                return NluResult(intent, confidence, word)
    return NluResult("unknown", 0.35)


def keyword_score(text: str, keyword: str) -> float:
    normalized_text = normalize_text(text)
    normalized_keyword = normalize_text(keyword)
    if not normalized_text or not normalized_keyword:
        return 0.0
    if normalized_keyword in normalized_text:
        return min(1.0, 0.78 + len(normalized_keyword) / max(len(normalized_text), 1) * 0.2)
    return SequenceMatcher(None, normalized_text, normalized_keyword).ratio() * 0.72


def get_config() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM dialogue_config WHERE id = 1").fetchone()
        if not row:
            return {"nlu_enabled": True, "default_scene_id": None}
        data = row_to_dict(row)
        return {
            "nlu_enabled": bool(data["nlu_enabled"]),
            "default_scene_id": data["default_scene_id"],
            "updated_at": data["updated_at"],
        }


def resolve_scene_id(scene_id: int | None) -> int | None:
    if scene_id:
        return scene_id
    return get_config().get("default_scene_id")


def load_flow(scene_id: int) -> tuple[dict[str, Any], int | None]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT dialogue_versions.*
            FROM dialogue_versions
            JOIN dialogue_scenes ON dialogue_scenes.active_version_id = dialogue_versions.id
            WHERE dialogue_scenes.id = ?
            """,
            (scene_id,),
        ).fetchone()
    if not row:
        return {}, None
    data = row_to_dict(row)
    return json.loads(data["flow_json"]), data["id"]


def flow_nodes(flow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in flow.get("nodes", [])}


def match_knowledge(scene_id: int, text: str) -> dict[str, Any] | None:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM knowledge_items
            WHERE scene_id = ? AND enabled = 1
            ORDER BY sort_order ASC, id ASC
            """,
            (scene_id,),
        ).fetchall()

    best: tuple[float, dict[str, Any], str] | None = None
    for row in rows:
        item = row_to_dict(row)
        keywords = split_keywords(item["keywords"]) or [item["title"]]
        for keyword in keywords:
            score = keyword_score(text, keyword)
            if best is None or score > best[0]:
                best = (score, item, keyword)

    if not best or best[0] < 0.68:
        return None

    score, item, keyword = best
    with connect() as conn:
        conn.execute(
            "UPDATE knowledge_items SET hit_count = hit_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item["id"],),
        )
    return {
        "id": item["id"],
        "title": item["title"],
        "answer": item["answer"],
        "score": round(score, 3),
        "keyword": keyword,
    }


def upsert_unresolved(scene_id: int | None, text: str) -> None:
    normalized = normalize_text(text)
    if not normalized:
        return
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM unresolved_questions
            WHERE scene_id IS ? AND user_text = ? AND status = 'pending'
            """,
            (scene_id, text),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE unresolved_questions
                SET hit_count = hit_count + 1, last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO unresolved_questions (scene_id, user_text) VALUES (?, ?)",
                (scene_id, text),
            )


def load_or_create_session(session_id: str, scene_id: int, entry_node: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM dialogue_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO dialogue_sessions (id, scene_id, current_node_id)
                VALUES (?, ?, ?)
                """,
                (session_id, scene_id, entry_node),
            )
            row = conn.execute("SELECT * FROM dialogue_sessions WHERE id = ?", (session_id,)).fetchone()
    return row_to_dict(row)


def evaluate_label(scene_id: int, session: dict[str, Any]) -> str:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM intent_label_rules
            WHERE scene_id = ? AND enabled = 1
            ORDER BY priority ASC, id ASC
            """,
            (scene_id,),
        ).fetchall()

    for row in rows:
        rule = row_to_dict(row)
        conditions = json.loads(rule["condition_json"] or "{}")
        matched = True
        for key, value in conditions.items():
            if int(session.get(key) or 0) < int(value):
                matched = False
                break
        if matched:
            return rule["label"]
    return session.get("label") or ""


def end_node_hangup_meta(node: dict[str, Any] | None) -> dict[str, Any]:
    if not node or node.get("type") != "end":
        return {}

    ui = node.get("ui") or {}
    try:
        delay_ms = int(ui.get("pauseMs") or 3000)
    except (TypeError, ValueError):
        delay_ms = 3000
    return {
        "should_hangup": True,
        "hangup_delay_ms": max(0, delay_ms),
    }


def handle_turn(
    *,
    session_id: str,
    text: str,
    scene_id: int | None = None,
    channel: str = "api",
    nlu_enabled_override: bool | None = None,
) -> dict[str, Any]:
    config = get_config()
    if nlu_enabled_override is False or not config["nlu_enabled"]:
        return {
            "handled": False,
            "route_type": "disabled",
            "reason": "nlu_disabled",
        }

    resolved_scene_id = resolve_scene_id(scene_id)
    if not resolved_scene_id:
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "no_scene_configured",
        }

    flow, version_id = load_flow(resolved_scene_id)
    if not flow:
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "no_published_flow",
        }

    nodes = flow_nodes(flow)
    entry_node = flow.get("entry_node", "start")
    session = load_or_create_session(session_id, resolved_scene_id, entry_node)
    current_node_id = session.get("current_node_id") or entry_node
    current_node = nodes.get(current_node_id) or nodes.get(entry_node)
    if current_node and current_node.get("type") == "end":
        response_text = current_node.get("text") or ""
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO dialogue_turn_logs (
                    session_id, scene_id, user_text, route_type, response_text,
                    current_node_id, next_node_id, nlu_json
                )
                VALUES (?, ?, ?, 'end', ?, ?, ?, '{}')
                """,
                (
                    session_id,
                    resolved_scene_id,
                    text,
                    response_text,
                    current_node_id,
                    current_node_id,
                ),
            )
        return {
            "handled": True,
            "route_type": "end",
            "text": response_text,
            "scene_id": resolved_scene_id,
            "flow_version_id": version_id,
            "current_node_id": current_node_id,
            "next_node_id": current_node_id,
            "label": session.get("label") or "",
            "nlu": {},
            **end_node_hangup_meta(current_node),
        }
    nlu = classify_intent(text, current_node)
    knowledge = match_knowledge(resolved_scene_id, text)

    if knowledge:
        session["knowledge_hit_count"] = int(session["knowledge_hit_count"]) + 1
        session["turn_count"] = int(session["turn_count"]) + 1
        session["label"] = evaluate_label(resolved_scene_id, session)
        with connect() as conn:
            conn.execute(
                """
                UPDATE dialogue_sessions
                SET turn_count = ?, knowledge_hit_count = ?, label = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (session["turn_count"], session["knowledge_hit_count"], session["label"], session_id),
            )
            conn.execute(
                """
                INSERT INTO dialogue_turn_logs (
                    session_id, scene_id, user_text, route_type, response_text,
                    current_node_id, next_node_id, nlu_json
                )
                VALUES (?, ?, ?, 'knowledge', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    resolved_scene_id,
                    text,
                    knowledge["answer"],
                    current_node_id,
                    current_node_id,
                    json.dumps({"intent": nlu.__dict__, "knowledge": knowledge}, ensure_ascii=False),
                ),
            )
        return {
            "handled": True,
            "route_type": "knowledge",
            "text": knowledge["answer"],
            "scene_id": resolved_scene_id,
            "flow_version_id": version_id,
            "current_node_id": current_node_id,
            "next_node_id": current_node_id,
            "label": session["label"],
            "nlu": {
                "intent": nlu.intent,
                "confidence": nlu.confidence,
                "matched_keyword": nlu.matched_keyword,
                "knowledge": knowledge,
            },
        }

    route = nlu.intent if nlu.confidence >= 0.55 else "unknown"
    next_node_id = (current_node or {}).get("routes", {}).get(route)
    if not next_node_id:
        next_node_id = (current_node or {}).get("routes", {}).get("unknown") or flow.get("unknown_route")

    next_node = nodes.get(next_node_id or "")
    if not next_node or next_node.get("type") == "llm_fallback":
        upsert_unresolved(resolved_scene_id, text)
        with connect() as conn:
            conn.execute(
                """
                UPDATE dialogue_sessions
                SET fallback_count = fallback_count + 1, turn_count = turn_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (session_id,),
            )
            conn.execute(
                """
                INSERT INTO dialogue_turn_logs (
                    session_id, scene_id, user_text, route_type, current_node_id,
                    next_node_id, nlu_json
                )
                VALUES (?, ?, ?, 'llm_fallback', ?, ?, ?)
                """,
                (
                    session_id,
                    resolved_scene_id,
                    text,
                    current_node_id,
                    next_node_id or "",
                    json.dumps({"intent": nlu.__dict__}, ensure_ascii=False),
                ),
            )
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "no_state_or_knowledge_match",
            "scene_id": resolved_scene_id,
            "current_node_id": current_node_id,
            "next_node_id": next_node_id,
            "nlu": {
                "intent": nlu.intent,
                "confidence": nlu.confidence,
                "matched_keyword": nlu.matched_keyword,
            },
        }

    counter_field = {
        "positive": "positive_count",
        "negative": "negative_count",
        "reject": "reject_count",
        "neutral": "neutral_count",
    }.get(route)
    session["turn_count"] = int(session["turn_count"]) + 1
    if counter_field:
        session[counter_field] = int(session[counter_field]) + 1
    session["label"] = evaluate_label(resolved_scene_id, session)

    response_text = next_node.get("text") or ""
    with connect() as conn:
        if counter_field:
            conn.execute(
                f"""
                UPDATE dialogue_sessions
                SET current_node_id = ?, turn_count = ?, {counter_field} = ?,
                    label = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_node["id"], session["turn_count"], session[counter_field], session["label"], session_id),
            )
        else:
            conn.execute(
                """
                UPDATE dialogue_sessions
                SET current_node_id = ?, turn_count = ?, label = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_node["id"], session["turn_count"], session["label"], session_id),
            )
        conn.execute(
            """
            INSERT INTO dialogue_turn_logs (
                session_id, scene_id, user_text, route_type, response_text,
                current_node_id, next_node_id, nlu_json
            )
            VALUES (?, ?, ?, 'flow', ?, ?, ?, ?)
            """,
            (
                session_id,
                resolved_scene_id,
                text,
                response_text,
                current_node_id,
                next_node["id"],
                json.dumps({"intent": nlu.__dict__, "route": route}, ensure_ascii=False),
            ),
        )

    return {
        "handled": True,
        "route_type": "flow",
        "text": response_text,
        "scene_id": resolved_scene_id,
        "flow_version_id": version_id,
        "current_node_id": current_node_id,
        "next_node_id": next_node["id"],
        "label": session["label"],
        "nlu": {
            "intent": nlu.intent,
            "confidence": nlu.confidence,
            "matched_keyword": nlu.matched_keyword,
            "route": route,
        },
        **end_node_hangup_meta(next_node),
    }


def start_session(*, session_id: str, scene_id: int | None = None) -> dict[str, Any]:
    config = get_config()
    resolved_scene_id = resolve_scene_id(scene_id)
    if not config["nlu_enabled"]:
        return {
            "handled": False,
            "route_type": "disabled",
            "reason": "nlu_disabled",
        }
    if not resolved_scene_id:
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "no_scene_configured",
        }
    flow, version_id = load_flow(resolved_scene_id)
    if not flow:
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "no_published_flow",
        }
    entry_node = flow.get("entry_node", "start")
    node = flow_nodes(flow).get(entry_node)
    if not node:
        return {
            "handled": False,
            "route_type": "llm_fallback",
            "reason": "entry_node_missing",
        }
    with connect() as conn:
        conn.execute("DELETE FROM dialogue_sessions WHERE id = ?", (session_id,))
        conn.execute(
            """
            INSERT INTO dialogue_sessions (id, scene_id, current_node_id, turn_count)
            VALUES (?, ?, ?, 0)
            """,
            (session_id, resolved_scene_id, entry_node),
        )
        conn.execute(
            """
            INSERT INTO dialogue_turn_logs (
                session_id, scene_id, user_text, route_type, response_text,
                current_node_id, next_node_id, nlu_json
            )
            VALUES (?, ?, '', 'start', ?, '', ?, '{}')
            """,
            (session_id, resolved_scene_id, node.get("text") or "", entry_node),
        )
    return {
        "handled": True,
        "route_type": "start",
        "text": node.get("text") or "",
        "scene_id": resolved_scene_id,
        "flow_version_id": version_id,
        "current_node_id": "",
        "next_node_id": entry_node,
        "label": "",
        "nlu": {},
    }


def list_scenes() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT dialogue_scenes.*,
                   dialogue_versions.version AS active_version,
                   COUNT(knowledge_items.id) AS knowledge_count
            FROM dialogue_scenes
            LEFT JOIN dialogue_versions ON dialogue_versions.id = dialogue_scenes.active_version_id
            LEFT JOIN knowledge_items ON knowledge_items.scene_id = dialogue_scenes.id
            GROUP BY dialogue_scenes.id
            ORDER BY dialogue_scenes.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


def get_scene(scene_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        scene = conn.execute("SELECT * FROM dialogue_scenes WHERE id = ?", (scene_id,)).fetchone()
        if not scene:
            return None
        version = conn.execute(
            "SELECT * FROM dialogue_versions WHERE id = ?",
            (scene["active_version_id"],),
        ).fetchone()
        knowledge = conn.execute(
            "SELECT * FROM knowledge_items WHERE scene_id = ? ORDER BY sort_order ASC, id ASC",
            (scene_id,),
        ).fetchall()
        labels = conn.execute(
            "SELECT * FROM intent_label_rules WHERE scene_id = ? ORDER BY priority ASC, id ASC",
            (scene_id,),
        ).fetchall()
    data = row_to_dict(scene)
    data["flow"] = json.loads(version["flow_json"]) if version else {}
    data["ui"] = json.loads(data.get("ui_json") or "{}")
    data["knowledge"] = rows_to_dicts(knowledge)
    data["label_rules"] = rows_to_dicts(labels)
    return data
