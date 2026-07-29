from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from qwen_audio_realtime import (  # noqa: E402
    QwenAudioRealtimeModel,
    load_realtime_instructions,
    voice_pipeline,
)


def test_voice_pipeline_defaults_to_classic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_VOICE_PIPELINE", raising=False)
    assert voice_pipeline() == "classic"


def test_voice_pipeline_accepts_both_modes() -> None:
    assert voice_pipeline("classic") == "classic"
    assert voice_pipeline(" REALTIME ") == "realtime"


def test_voice_pipeline_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="QWEN_VOICE_PIPELINE"):
        voice_pipeline("hybrid")


def test_prompt_loader_replaces_call_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_AUDIO_REALTIME_INSTRUCTIONS", raising=False)
    monkeypatch.delenv("QWEN_AUDIO_REALTIME_PROMPT_FILE", raising=False)
    prompt = load_realtime_instructions(
        root=ROOT.parent,
        session_id="room-123",
        scene_id=7,
        customer_name="林晓",
        customer_phone="13800000000",
        customer_profile="建筑设计师",
    )
    assert "room-123" in prompt
    assert "场景编号：7" in prompt
    assert "客户姓名：林晓" in prompt
    assert "建筑设计师" in prompt
    assert "陌陌公司负责骑手招聘的招聘专员" in prompt
    assert "rider_opening" in prompt
    assert "骑手配送岗位" in prompt
    assert "{{" not in prompt


def test_prompt_loader_compiles_frontend_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_AUDIO_REALTIME_INSTRUCTIONS", raising=False)
    monkeypatch.delenv("QWEN_AUDIO_REALTIME_PROMPT_FILE", raising=False)
    scene = {
        "name": "续费提醒",
        "industry": "企业服务",
        "business_type": "客户续费",
        "ui": {"agent_identity": "某软件公司的客户成功顾问"},
        "flow": {
            "entry_node": "welcome",
            "max_turns": 6,
            "unknown_route": "clarify",
            "nodes": [
                {
                    "id": "welcome",
                    "type": "scene",
                    "name": "确认联系人",
                    "text": "您好，请问是林经理吗？",
                    "routes": {"本人": "renew", "非本人": "end"},
                    "intent_keywords": {"本人": ["是我", "本人"]},
                },
                {"id": "renew", "type": "scene", "name": "续费沟通", "text": "想和您确认续费安排。"},
                {"id": "end", "type": "end", "name": "结束", "text": "感谢接听，再见。"},
            ],
        },
        "knowledge": [{"title": "到期日", "answer": "服务将于月底到期。", "keywords": "到期", "enabled": 1}],
    }
    prompt = load_realtime_instructions(
        root=ROOT.parent,
        session_id="room-flex",
        scene_id=9,
        scene=scene,
    )
    assert "某软件公司的客户成功顾问" in prompt
    assert "您好，请问是林经理吗？" in prompt
    assert "本人 -> renew" in prompt
    assert "本人=[是我, 本人]" in prompt
    assert "服务将于月底到期" in prompt


def test_realtime_model_uses_dashscope_beta_compatibility() -> None:
    model = QwenAudioRealtimeModel(api_key="test-key")
    try:
        assert model.provider == "dashscope-qwen-audio-realtime"
        assert model.model == "qwen-audio-3.0-realtime-flash"
        assert model._opts.is_azure is True
        assert model._opts.entra_token == "test-key"
        assert model._opts.api_key is None
        assert model._opts.turn_detection.type == "server_vad"
        assert model._opts.turn_detection.threshold == 0.65
        assert model._opts.turn_detection.prefix_padding_ms == 200
        assert model._opts.turn_detection.silence_duration_ms == 650
    finally:
        # No session was opened, so there is no async resource to close.
        pass
