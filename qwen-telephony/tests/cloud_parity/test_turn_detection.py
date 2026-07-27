from __future__ import annotations

import asyncio
from importlib.metadata import version
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from livekit.agents.inference_runner import _InferenceRunner


PROJECT_DIR = Path(__file__).resolve().parents[2]
AGENT_DIR = PROJECT_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import phone_agent


def test_turn_detector_plugin_matches_the_livekit_agents_version() -> None:
    assert version("livekit-agents") == "1.6.6"
    assert version("livekit-plugins-turn-detector") == "1.6.6"


def test_multilingual_runner_is_registered_before_worker_startup() -> None:
    assert "lk_end_of_utterance_multilingual" in _InferenceRunner.registered_runners


def test_multilingual_turn_detector_uses_local_language_thresholds(monkeypatch) -> None:
    calls: list[dict[str, float | None]] = []

    def factory(**options):
        calls.append(options)
        return SimpleNamespace(provider="livekit", model="multilingual")

    monkeypatch.delenv("QWEN_TURN_DETECTION_MODE", raising=False)
    monkeypatch.delenv("QWEN_TURN_DETECTOR_THRESHOLD", raising=False)
    monkeypatch.delenv("LIVEKIT_REMOTE_EOT_URL", raising=False)

    detector = phone_agent._build_turn_detector(model_factory=factory)

    assert detector.model == "multilingual"
    assert calls == [{"unlikely_threshold": None}]


def test_turn_detector_supports_validated_threshold_and_text_alias(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_TURN_DETECTION_MODE", "text")
    monkeypatch.setenv("QWEN_TURN_DETECTOR_THRESHOLD", "0.72")
    monkeypatch.delenv("LIVEKIT_REMOTE_EOT_URL", raising=False)

    detector = phone_agent._build_turn_detector(
        model_factory=lambda **options: options
    )

    assert phone_agent._turn_detection_mode() == "multilingual"
    assert detector == {"unlikely_threshold": 0.72}


def test_turn_detector_never_sends_transcripts_to_remote_eot(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_TURN_DETECTION_MODE", "multilingual")
    monkeypatch.setenv("LIVEKIT_REMOTE_EOT_URL", "https://unexpected.example")

    with pytest.raises(ValueError, match="must be unset"):
        phone_agent._build_turn_detector(model_factory=lambda **_options: object())


def test_vad_mode_is_an_explicit_model_free_fallback(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_TURN_DETECTION_MODE", "vad")
    monkeypatch.setenv("LIVEKIT_REMOTE_EOT_URL", "https://ignored-in-vad-mode.example")

    detector = phone_agent._build_turn_detector(
        model_factory=lambda **_options: pytest.fail("model must not be constructed")
    )

    assert detector == "vad"


def test_silero_vad_is_configured_for_telephone_audio(monkeypatch) -> None:
    captured: dict[str, float | int] = {}
    for name in (
        "QWEN_VAD_SAMPLE_RATE",
        "QWEN_VAD_MIN_SPEECH_SECONDS",
        "QWEN_VAD_MIN_SILENCE_SECONDS",
        "QWEN_VAD_PREFIX_PADDING_SECONDS",
        "QWEN_VAD_ACTIVATION_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)

    vad = object()

    def loader(**options):
        captured.update(options)
        return vad

    assert phone_agent._build_session_vad(loader=loader) is vad
    assert captured == {
        "min_speech_duration": 0.05,
        "min_silence_duration": 0.55,
        "prefix_padding_duration": 0.2,
        "activation_threshold": 0.45,
        "sample_rate": 8000,
    }


def test_silero_vad_is_prewarmed_once_per_job_process(monkeypatch) -> None:
    vad = object()
    calls = 0

    def build_vad():
        nonlocal calls
        calls += 1
        return vad

    async def greeting_cache() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(phone_agent, "_build_session_vad", build_vad)
    monkeypatch.setattr(phone_agent, "ensure_greeting_audio_cache", greeting_cache)
    process = SimpleNamespace(userdata={})

    phone_agent.prewarm_process(process)

    assert process.userdata["vad"] is vad
    assert calls == 1


def test_dynamic_endpointing_defaults_are_bounded(monkeypatch) -> None:
    for name in (
        "QWEN_ENDPOINTING_MODE",
        "QWEN_ENDPOINTING_MIN_DELAY",
        "QWEN_ENDPOINTING_MAX_DELAY",
        "QWEN_ENDPOINTING_ALPHA",
    ):
        monkeypatch.delenv(name, raising=False)

    assert phone_agent._turn_endpointing_options() == {
        "mode": "dynamic",
        "min_delay": 0.5,
        "max_delay": 3.0,
        "alpha": 0.9,
    }


@pytest.mark.parametrize(
    ("name", "value", "call", "message"),
    [
        (
            "QWEN_TURN_DETECTION_MODE",
            "automatic",
            phone_agent._turn_detection_mode,
            "QWEN_TURN_DETECTION_MODE",
        ),
        (
            "QWEN_VAD_SAMPLE_RATE",
            "44100",
            lambda: phone_agent._build_session_vad(loader=lambda **_options: object()),
            "QWEN_VAD_SAMPLE_RATE",
        ),
        (
            "QWEN_VAD_ACTIVATION_THRESHOLD",
            "NaN",
            lambda: phone_agent._build_session_vad(loader=lambda **_options: object()),
            "QWEN_VAD_ACTIVATION_THRESHOLD",
        ),
        (
            "QWEN_TURN_DETECTOR_THRESHOLD",
            "1.1",
            lambda: phone_agent._build_turn_detector(
                model_factory=lambda **_options: object()
            ),
            "QWEN_TURN_DETECTOR_THRESHOLD",
        ),
    ],
)
def test_invalid_turn_configuration_fails_fast(
    monkeypatch, name: str, value: str, call, message: str
) -> None:
    monkeypatch.setenv(name, value)
    if name == "QWEN_TURN_DETECTOR_THRESHOLD":
        monkeypatch.setenv("QWEN_TURN_DETECTION_MODE", "multilingual")
        monkeypatch.delenv("LIVEKIT_REMOTE_EOT_URL", raising=False)

    with pytest.raises(ValueError, match=message):
        call()


def test_endpointing_rejects_an_inverted_delay_range(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_ENDPOINTING_MIN_DELAY", "4")
    monkeypatch.setenv("QWEN_ENDPOINTING_MAX_DELAY", "1")

    with pytest.raises(ValueError, match="must not exceed"):
        phone_agent._turn_endpointing_options()


def test_agent_image_bakes_models_outside_the_runtime_cache_volume() -> None:
    dockerfile = (PROJECT_DIR / "Dockerfile.agent").read_text(encoding="utf-8")

    assert "HF_HOME=/app/models/huggingface" in dockerfile
    assert "ARG HF_ENDPOINT=https://huggingface.co" in dockerfile
    assert "RUN python -m livekit.agents download-files" in dockerfile
    assert "HF_HOME=/app/qwen-telephony/cache" not in dockerfile
