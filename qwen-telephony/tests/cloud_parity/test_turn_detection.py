from __future__ import annotations

import asyncio
import io
import json
from importlib.metadata import version
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

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

    async def realtime_fixed_cache() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(phone_agent, "_build_session_vad", build_vad)
    monkeypatch.setattr(phone_agent, "ensure_greeting_audio_cache", greeting_cache)
    monkeypatch.setattr(
        phone_agent,
        "ensure_realtime_fixed_audio_cache",
        realtime_fixed_cache,
    )
    process = SimpleNamespace(userdata={})

    phone_agent.prewarm_process(process)

    assert process.userdata["vad"] is vad
    assert calls == 1


def test_realtime_opening_selection_uses_default_variants_and_scene_entry(
    monkeypatch,
) -> None:
    monkeypatch.delenv("QWEN_REALTIME_OPENING_TEXT", raising=False)
    selected = phone_agent.DEFAULT_REALTIME_OPENINGS[2]
    monkeypatch.setattr(phone_agent.secrets, "choice", lambda _items: selected)
    assert phone_agent._select_realtime_opening(None) == selected
    assert phone_agent._select_realtime_opening(
        {
            "flow": {
                "entry_node": "rider_opening",
                "nodes": [{"id": "rider_opening", "text": "前端默认骑手开场"}],
            }
        }
    ) == selected

    scene = {
        "flow": {
            "entry_node": "welcome",
            "nodes": [
                {"id": "other", "text": "不是入口"},
                {"id": "welcome", "text": "您好，这是动态开场。"},
            ],
        }
    }
    assert phone_agent._select_realtime_opening(scene) == "您好，这是动态开场。"


def test_realtime_opening_selection_accepts_runtime_override(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_REALTIME_OPENING_TEXT", "您好，请问是晓旭老师吗？")
    assert phone_agent._select_realtime_opening(None) == "您好，请问是晓旭老师吗？"
    assert phone_agent._select_realtime_opening({"flow": {}}) == "您好，请问是晓旭老师吗？"


def test_local_realtime_transcript_is_saved(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QWEN_REALTIME_TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(phone_agent, "time_ns", lambda: 123)
    phone_agent._append_local_transcript(
        room_name="recorded/room",
        role="user",
        text=" 是的。 ",
        item_id="item-1",
    )
    record = json.loads((tmp_path / "recorded_room.jsonl").read_text(encoding="utf-8"))
    assert record == {
        "timestamp_ns": 123,
        "room": "recorded/room",
        "role": "user",
        "text": "是的。",
        "item_id": "item-1",
        "source": "realtime",
    }


def test_blind_ab_mapping_records_identical_pcm_and_hidden_paths(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.wav"
    pcm = (b"\x01\x00\xff\xff" * 240)
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(pcm)
    prepared = phone_agent._prepare_wav_for_room_playback(source.read_bytes())
    monkeypatch.setenv("QWEN_REALTIME_AB_RESULT_DIR", str(tmp_path / "results"))

    target = phone_agent._write_audio_ab_mapping(
        room_name="strict/ab",
        source=source,
        prepared_audio=prepared,
        pcm_sha256=phone_agent._wav_pcm_sha256(prepared),
        mapping={"A": "agent_session_roomio", "B": "direct_local_audio_track"},
    )

    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["pcm_sha256"] == phone_agent.hashlib.sha256(pcm).hexdigest()
    assert record["sample_rate"] == 24_000
    assert record["channels"] == 1
    assert record["mapping"] == {
        "A": "agent_session_roomio",
        "B": "direct_local_audio_track",
    }
    assert record["blinded"] is True


def test_agent_output_ab_arm_receives_the_exact_pcm() -> None:
    async def run() -> None:
        pcm = b"".join(value.to_bytes(2, "little", signed=True) for value in range(1000))
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(pcm)

        class Output:
            def __init__(self) -> None:
                self.captured = bytearray()
                self.flushed = False

            async def capture_frame(self, frame) -> None:
                self.captured.extend(bytes(frame.data))

            def flush(self) -> None:
                self.flushed = True

            async def wait_for_playout(self):
                return SimpleNamespace(interrupted=False)

        sink = Output()
        duration = await phone_agent._play_wav_bytes_via_agent_output(
            sink,
            wav_buffer.getvalue(),
            label="test A/B RoomIO arm",
        )
        assert bytes(sink.captured) == pcm
        assert sink.flushed is True
        assert duration == pytest.approx(1000 / 24_000)

    asyncio.run(run())


def test_realtime_scene_fetch_is_started_without_blocking_opening(monkeypatch) -> None:
    async def run() -> None:
        release = asyncio.Event()

        async def slow_fetch(_scene_id: int):
            await release.wait()
            return {"name": "late scene"}

        monkeypatch.setattr(phone_agent, "fetch_realtime_scene", slow_fetch)
        phone_agent._REALTIME_SCENE_CACHE.pop(987654, None)

        scene, task = phone_agent._start_realtime_scene_fetch(987654)

        assert scene is None
        assert task is not None
        assert not task.done()
        release.set()
        assert await task == {"name": "late scene"}

    asyncio.run(run())


def test_wait_for_active_sip_participant_ignores_ringing_until_active() -> None:
    async def run() -> None:
        participant = SimpleNamespace(
            kind=phone_agent.rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            attributes={"sip.callStatus": "ringing"},
        )
        room = SimpleNamespace(remote_participants={"callee": participant})

        async def activate() -> None:
            await asyncio.sleep(0.03)
            participant.attributes["sip.callStatus"] = "active"

        activation = asyncio.create_task(activate())
        selected = await phone_agent._wait_for_active_sip_participant(
            room,
            timeout=0.2,
        )
        await activation
        assert selected is participant

    asyncio.run(run())


def test_complete_wechat_followup_returns_before_persistence_finishes(monkeypatch) -> None:
    async def run() -> None:
        agent = phone_agent.PhoneAgent()
        persist_started = asyncio.Event()
        release_persist = asyncio.Event()

        async def slow_record(_event_type, _payload) -> bool:
            persist_started.set()
            await release_persist.wait()
            return True

        monkeypatch.setattr(agent, "_record_realtime_business_event", slow_record)
        result = await phone_agent.PhoneAgent.complete_wechat_followup.__wrapped__(
            agent,
            summary="微信已确认",
        )

        assert result == "微信跟进结果已提交。"
        await asyncio.wait_for(persist_started.wait(), timeout=0.1)
        assert any(not task.done() for task in agent._pending_business_event_tasks)
        release_persist.set()
        await agent.wait_for_pending_business_events()

    asyncio.run(run())


def test_realtime_end_call_defers_goodbye_to_programmatic_handler(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(phone_agent, "voice_pipeline", lambda: phone_agent.REALTIME_PIPELINE)
        agent = phone_agent.PhoneAgent()
        result = await phone_agent.PhoneAgent.end_call.__wrapped__(
            agent,
            SimpleNamespace(),
            reason="normal completion",
        )
        assert result == "结束请求已接收，程序将播放统一结束语。"

    asyncio.run(run())


def test_phone_agent_accepts_preseeded_opening_context() -> None:
    opening = "您好，这是已经播放的开场。"
    chat_ctx = phone_agent.llm.ChatContext.empty()
    chat_ctx.add_message(role="assistant", content=opening)

    agent = phone_agent.PhoneAgent(chat_ctx=chat_ctx)

    assert len(agent.chat_ctx.items) == 1
    assert opening in str(agent.chat_ctx.items[0].content)


def test_task_realtime_context_does_not_seed_synthetic_customer_event() -> None:
    chat_ctx = phone_agent._initial_realtime_chat_context(
        selected_pipeline=phone_agent.REALTIME_PIPELINE,
        realtime_opening="",
        task_prompt_override="邀请客户吃饭",
    )

    assert chat_ctx is None


def test_outbound_identity_opening_uses_customer_name_exactly() -> None:
    assert phone_agent._outbound_identity_opening(" 任 总 ") == (
        "您好，我是李宝祥的智能助理，请问您是任总吗？"
    )
    assert phone_agent._outbound_identity_opening("") == (
        "您好，我是李宝祥的智能助理，请问怎么称呼您？"
    )


def test_realtime_audio_io_gate_controls_input_and_output() -> None:
    class AudioIO:
        def __init__(self) -> None:
            self.audio = object()
            self.states: list[bool] = []

        def set_audio_enabled(self, enabled: bool) -> None:
            self.states.append(enabled)

    input_io = AudioIO()
    output_io = AudioIO()
    session = SimpleNamespace(input=input_io, output=output_io)

    phone_agent._set_session_audio_io_enabled(session, False)
    phone_agent._set_session_audio_io_enabled(session, True)

    assert input_io.states == [False, True]
    assert output_io.states == [False, True]


def test_fallback_summary_reports_agreed_business_outcome_and_reminder() -> None:
    summary = phone_agent._fallback_business_summary(
        customer_name="任总",
        reason="客户连续5秒未回应，系统主动挂机",
        turns=[
            ("assistant", "您好，我是李宝祥的智能助理，请问您是任总吗？"),
            ("user", "是的。"),
            ("assistant", "李总想约您今晚六点半吃饭。"),
            ("user", "可以。"),
            ("assistant", "好的，地点稍后李总会跟您确认。"),
        ],
    )

    assert summary == (
        "任总已同意：李总想约您今晚六点半吃饭；"
        "提示：地点稍后李总会跟您确认。"
    )
    assert "5秒" not in summary
    assert "系统主动挂机" not in summary


def test_fallback_summary_removes_spoken_salutation_and_closing_pleasantry() -> None:
    summary = phone_agent._fallback_business_summary(
        customer_name="李艳美",
        reason="parent process shutdown",
        turns=[
            ("assistant", "您好，我是李宝祥的智能助理，请问您是李艳美吗？"),
            ("user", "你有什么事？"),
            ("assistant", "李姐您好呀！宝祥想邀请您周末带果果来北京玩。"),
            ("user", "好啊。"),
            ("assistant", "太好啦！那我跟宝祥说一声，周末等你们来呀！"),
        ],
    )

    assert summary == "李艳美已同意：宝祥想邀请您周末带果果来北京玩。"
    assert "李姐您好" not in summary
    assert "太好啦" not in summary


def test_fallback_summary_keeps_no_response_runtime_reason() -> None:
    assert phone_agent._fallback_business_summary(
        customer_name="任总",
        reason="客户连续5秒未回应，系统主动挂机",
        turns=[
            ("assistant", "您好，我是李宝祥的智能助理，请问您是任总吗？"),
        ],
    ) == "客户连续5秒未回应，系统主动挂机"


def test_saved_business_summary_strips_runtime_hangup_mechanics() -> None:
    summary = phone_agent._sanitize_business_summary(
        "客户连续5秒未回应，系统主动挂机",
        customer_name="任总",
        turns=[
            ("assistant", "李总想约您今晚六点半吃饭。"),
            ("user", "可以。"),
        ],
    )

    assert summary == "任总已同意：李总想约您今晚六点半吃饭。"


def test_identity_confirmation_is_not_a_business_result() -> None:
    turns = [
        ("assistant", "您好，我是李宝祥的智能助理，请问您是李家魁吗？"),
        ("user", "是。"),
    ]

    assert phone_agent._evidence_based_call_result(
        customer_name="李家魁",
        turns=turns,
    ) is None
    assert phone_agent._fallback_business_summary(
        customer_name="李家魁",
        reason="通话结束",
        turns=turns,
    ) == "通话结束"


def test_business_result_is_derived_from_transcript_not_model_claim() -> None:
    result = phone_agent._evidence_based_call_result(
        customer_name="李家魁",
        turns=[
            ("assistant", "您好，我是李宝祥的智能助理，请问您是李家魁吗？"),
            ("user", "是。"),
            ("assistant", "明天下午三点参加3D动捕会议，您方便吗？"),
            ("user", "明天上午三点。"),
        ],
    )

    assert result == "已完成与李家魁的电话沟通；对方最后回复：“明天上午三点”。"


def test_save_call_result_rejects_unsupported_model_conclusion() -> None:
    async def run() -> None:
        agent = phone_agent.PhoneAgent(
            managed_job={
                "call_id": "call-1",
                "customer_name": "李家魁",
                "direction": "outbound",
                "realtime_prompt": "邀请参加会议",
            }
        )
        agent._conversation_turns = [
            ("assistant", "您好，我是李宝祥的智能助理，请问您是李家魁吗？"),
            ("user", "是。"),
        ]
        persisted: list[tuple[str, dict]] = []

        async def persist(event_type: str, payload: dict) -> bool:
            persisted.append((event_type, payload))
            return True

        agent._record_realtime_business_event = persist
        result = await phone_agent.PhoneAgent.save_call_result.__wrapped__(
            agent,
            summary="李家魁已确认参加明天下午三点会议。",
            intent_label="confirmed",
        )

        assert "尚无可验证的业务答复" in result
        assert persisted == []
        assert agent._business_result_saved is False

        end_result = await phone_agent.PhoneAgent.end_call.__wrapped__(
            agent,
            SimpleNamespace(),
            reason="客户已同意",
        )
        assert "不能结束通话" in end_result

    asyncio.run(run())


def test_save_call_result_persists_only_transcript_derived_text() -> None:
    async def run() -> None:
        agent = phone_agent.PhoneAgent(
            managed_job={"call_id": "call-2", "customer_name": "李家魁"}
        )
        agent._conversation_turns = [
            ("assistant", "请问您是李家魁吗？"),
            ("user", "是。"),
            ("assistant", "明天下午三点参加会议，您方便吗？"),
            ("user", "不方便。"),
        ]
        persisted: list[tuple[str, dict]] = []

        async def persist(event_type: str, payload: dict) -> bool:
            persisted.append((event_type, payload))
            return True

        agent._record_realtime_business_event = persist
        result = await phone_agent.PhoneAgent.save_call_result.__wrapped__(
            agent,
            summary="李家魁已确认参加。",
            intent_label="confirmed",
        )

        assert result == "通话结果已保存。"
        assert persisted == [
            (
                "call.result",
                {
                    "summary": "李家魁未同意：明天下午三点参加会议，您方便吗。",
                    "intent_label": "",
                },
            )
        ]

    asyncio.run(run())


def test_outbound_agent_hangup_is_default(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_OUTBOUND_CUSTOMER_HANGUP_ONLY", raising=False)
    assert not phone_agent._customer_hangup_only({"direction": "outbound"})
    monkeypatch.setenv("QWEN_OUTBOUND_CUSTOMER_HANGUP_ONLY", "true")
    assert phone_agent._customer_hangup_only({"direction": "outbound"})


def test_wechat_added_notice_matches_spoken_punctuation_variants() -> None:
    assert phone_agent._is_wechat_added_notice(
        "好的，已经加您了，请您通过一下。"
    )
    assert phone_agent._is_wechat_added_notice("已经加您了 请您通过")
    assert not phone_agent._is_wechat_added_notice("好的，已记下您的微信。")


@pytest.mark.parametrize("text", ["好的。", "嗯，好的", "行", "知道了", "我会通过"])
def test_short_wechat_acknowledgement_is_detected(text: str) -> None:
    assert phone_agent._is_short_wechat_acknowledgement(text)


@pytest.mark.parametrize("text", ["好的，我还有个问题", "不用了", "微信怎么加"])
def test_wechat_questions_are_not_treated_as_closing_acknowledgements(
    text: str,
) -> None:
    assert not phone_agent._is_short_wechat_acknowledgement(text)


def test_only_dedicated_tool_requests_programmatic_wechat_notice() -> None:
    assert phone_agent._function_call_requests_wechat_notice(
        SimpleNamespace(
            name="complete_wechat_followup",
            arguments='{"summary":"confirmed"}',
        )
    )
    assert not phone_agent._function_call_requests_wechat_notice(
        SimpleNamespace(
            name="save_call_result",
            arguments='{"summary":"ordinary result"}',
        )
    )


def test_programmatic_audio_tools_cancel_model_tool_reply() -> None:
    cancelled = False

    def cancel_tool_reply() -> None:
        nonlocal cancelled
        cancelled = True

    event = SimpleNamespace(
        function_calls=[
            SimpleNamespace(
                name="complete_wechat_followup",
                arguments='{"summary":"confirmed"}',
            ),
            SimpleNamespace(name="end_call", arguments='{"reason":"done"}'),
        ],
        cancel_tool_reply=cancel_tool_reply,
    )

    assert phone_agent._cancel_tool_reply_for_programmatic_audio(event) == {
        "complete_wechat_followup",
        "end_call",
    }
    assert cancelled


def test_wechat_notice_precedes_batched_end_call() -> None:
    assert phone_agent._programmatic_audio_action(
        {"complete_wechat_followup", "end_call"}
    ) == "wechat_notice"
    assert phone_agent._programmatic_audio_action({"end_call"}) == "final_goodbye"


def test_wechat_silence_timer_starts_when_playout_finishes() -> None:
    events: list[str] = []

    phone_agent._finish_wechat_notice_playout(
        awaiting_acknowledgement=True,
        start_close_timer=lambda: events.append("timer-started"),
    )

    assert events == ["timer-started"]


def test_completed_single_flight_task_is_not_restarted() -> None:
    async def run() -> None:
        calls = 0

        async def action() -> None:
            nonlocal calls
            calls += 1

        task = phone_agent._start_single_flight_task(
            None, action, name="single-flight-test"
        )
        await task
        same_task = phone_agent._start_single_flight_task(
            task, action, name="single-flight-test"
        )
        await same_task

        assert same_task is task
        assert calls == 1

    asyncio.run(run())


def test_non_programmatic_tool_keeps_model_tool_reply() -> None:
    event = SimpleNamespace(
        function_calls=[SimpleNamespace(name="save_call_result", arguments="{}")],
        cancel_tool_reply=lambda: pytest.fail("reply must not be cancelled"),
    )
    assert phone_agent._cancel_tool_reply_for_programmatic_audio(event) == set()


def test_direct_fixed_audio_waits_for_playout_before_unpublishing(monkeypatch) -> None:
    events: list[str] = []
    captured_samples: list[int] = []

    class FakeAudioSource:
        def __init__(self, *_args, **_kwargs) -> None:
            self.frames = 0

        async def capture_frame(self, _frame) -> None:
            self.frames += 1
            captured_samples.append(_frame.samples_per_channel)
            if self.frames == 1:
                events.append("first-frame-captured")

        async def wait_for_playout(self) -> None:
            events.append("playout-complete")

        async def aclose(self) -> None:
            events.append("source-closed")

    class FakeParticipant:
        async def publish_track(self, _track):
            events.append("published")
            return SimpleNamespace(sid="track-1")

        async def unpublish_track(self, sid: str) -> None:
            events.append(f"unpublished:{sid}")

    monkeypatch.setattr(phone_agent.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(
        phone_agent.rtc,
        "LocalAudioTrack",
        SimpleNamespace(create_audio_track=lambda _name, source: source),
    )

    sample_rate = phone_agent.ROOM_AUDIO_SAMPLE_RATE
    samples = sample_rate // 25
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * samples)

    duration = asyncio.run(
        phone_agent._play_wav_bytes_direct(
            SimpleNamespace(local_participant=FakeParticipant()),
            output.getvalue(),
            label="test goodbye",
            track_name="test-track",
            tail_silence_ms=400,
            on_first_frame_queued=lambda: events.append("first-frame-callback"),
        )
    )

    assert duration == pytest.approx(0.04)
    assert sum(captured_samples) == samples + sample_rate * 400 // 1000
    assert events == [
        "published",
        "first-frame-captured",
        "first-frame-callback",
        "playout-complete",
        "unpublished:track-1",
        "source-closed",
    ]


def test_realtime_scene_cache_expires(monkeypatch) -> None:
    async def run() -> None:
        scene_id = 234567
        old_scene = {"name": "old"}
        new_scene = {"name": "new"}
        phone_agent._REALTIME_SCENE_CACHE[scene_id] = old_scene
        phone_agent._REALTIME_SCENE_CACHE_UPDATED_AT[scene_id] = 100.0
        monkeypatch.setattr(phone_agent, "perf_counter", lambda: 105.0)
        monkeypatch.setenv("QWEN_REALTIME_SCENE_CACHE_TTL_SECONDS", "30")

        cached, task = phone_agent._start_realtime_scene_fetch(scene_id)
        assert cached is old_scene
        assert task is None

        async def refresh(_scene_id: int):
            assert _scene_id == scene_id
            return new_scene

        monkeypatch.setattr(phone_agent, "fetch_realtime_scene", refresh)
        monkeypatch.setattr(phone_agent, "perf_counter", lambda: 131.0)
        cached, task = phone_agent._start_realtime_scene_fetch(scene_id)
        assert cached is old_scene
        assert task is not None
        assert await task is new_scene

        phone_agent._REALTIME_SCENE_CACHE.pop(scene_id, None)
        phone_agent._REALTIME_SCENE_CACHE_UPDATED_AT.pop(scene_id, None)

    asyncio.run(run())


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
