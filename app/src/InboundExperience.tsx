import { useEffect, useRef, useState } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { inboundRequest } from "./inboundApi";

type ExperienceState = "idle" | "requesting" | "connecting" | "active" | "reconnecting" | "ending" | "completed" | "error";

type DemoInfo = {
  available: boolean;
  reason_code?: string;
  name?: string;
  description?: string;
  max_duration_seconds?: number;
  notice?: string;
  public_number?: string;
  tel_uri?: string;
};

type WebSession = {
  session_id: string;
  token: string;
  url: string;
  max_duration_seconds: number;
  remaining_calls: number;
};

type VoiceRoom = Pick<Room, "on" | "connect" | "disconnect" | "localParticipant">;

declare global {
  interface Window {
    __inboundVoiceRoomFactory?: () => VoiceRoom;
  }
}

export default function InboundExperience() {
  const [info, setInfo] = useState<DemoInfo | null>(null);
  const [state, setState] = useState<ExperienceState>("idle");
  const [message, setMessage] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [session, setSession] = useState<WebSession | null>(null);
  const [muted, setMuted] = useState(false);
  const [serverReason, setServerReason] = useState("");
  const roomRef = useRef<VoiceRoom | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    inboundRequest<DemoInfo>("/inbound-api/public/demo")
      .then(setInfo)
      .catch((error) => {
        setInfo({ available: false, reason_code: "service_unavailable" });
        setMessage(error instanceof Error ? error.message : "体验服务暂时不可用");
      });
  }, []);

  useEffect(() => {
    if (state !== "active") return;
    const timer = window.setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [state]);

  useEffect(() => {
    if (!session || !["connecting", "active", "reconnecting", "ending"].includes(state)) return;
    const poll = window.setInterval(() => {
      inboundRequest<{ status: string; termination_reason: string }>(`/inbound-api/public/demo/sessions/${session.session_id}`)
        .then((status) => {
          if (status.status === "completed") {
            setServerReason(status.termination_reason || "completed");
            roomRef.current?.disconnect();
            roomRef.current = null;
            setState("completed");
          }
        })
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(poll);
  }, [session?.session_id, state]);

  useEffect(() => {
    if (!session || state !== "active" || seconds < session.max_duration_seconds + 5) return;
    setServerReason("time_limit");
    void endExperience();
  }, [seconds, session?.max_duration_seconds, state]);

  useEffect(() => () => {
    roomRef.current?.disconnect();
  }, []);

  async function startExperience() {
    setMessage("");
    setState("requesting");
    try {
      const created = await inboundRequest<WebSession>("/inbound-api/public/demo/web-sessions", {
        method: "POST",
        body: JSON.stringify({ participant_name: "访客" }),
      });
      setSession(created);
      setState("connecting");
      const room: VoiceRoom = window.__inboundVoiceRoomFactory?.()
        || new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;
      room.on(RoomEvent.Reconnecting, () => setState("reconnecting"));
      room.on(RoomEvent.Reconnected, () => setState("active"));
      room.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === Track.Kind.Audio && audioRef.current) track.attach(audioRef.current);
      });
      room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach());
      room.on(RoomEvent.Disconnected, (reason) => {
        roomRef.current = null;
        setState((current) => {
          if (current === "ending" || serverReason === "time_limit") return "completed";
          if (reason) setMessage("语音连接已中断，您可以重新开始体验。");
          return reason ? "error" : "completed";
        });
      });
      await room.connect(created.url, created.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setSeconds(0);
      setMuted(false);
      setServerReason("");
      setState("active");
    } catch (error) {
      roomRef.current?.disconnect();
      roomRef.current = null;
      setState("error");
      setMessage(error instanceof Error ? error.message : "无法开始语音体验");
    }
  }

  async function endExperience() {
    setState("ending");
    try {
      await roomRef.current?.disconnect();
    } finally {
      roomRef.current = null;
      setState("completed");
    }
  }

  async function toggleMute() {
    const next = !muted;
    await roomRef.current?.localParticipant.setMicrophoneEnabled(!next);
    setMuted(next);
  }

  const time = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  const live = ["connecting", "active", "reconnecting", "ending"].includes(state);

  return (
    <main className="inbound-experience">
      <header className="inbound-public-nav">
        <a href="/" aria-label="返回云声通首页"><img src="/assets/brand/call-logo.svg" alt="云声通" /></a>
        <nav><a href="#how">如何体验</a><a href="#boundaries">隐私与边界</a></nav>
        <a className="inbound-console-link" href="/app/inbound/agents">企业控制台</a>
      </header>

      <section className="experience-hero">
        <div className="experience-copy">
          <span className="section-kicker">公开语音体验</span>
          <h1>打一通自然的电话，<br />感受认真倾听的服务。</h1>
          <p>无需创建复杂配置，允许麦克风后即可与公开体验助手交谈。每次会话独立，不会接触任何企业数据。</p>
          <div className="experience-facts"><span>最长 {Math.round((info?.max_duration_seconds || 180) / 60)} 分钟</span><span>无需注册</span><span>不开放业务工具</span></div>
          {info?.public_number ? <a className="experience-phone" href={info.tel_uri}>用手机拨打 {info.public_number}</a> : <small className="experience-phone-pending">当前开放浏览器体验，公开电话号码配置完成后将在这里显示。</small>}
        </div>

        <section className={`voice-stage ${live ? "is-live" : ""}`}>
          <audio ref={audioRef} autoPlay playsInline />
          <div className="voice-stage-top"><span role="status" aria-live="polite">{state === "active" ? "正在通话" : state === "reconnecting" ? "正在恢复连接" : state === "completed" ? "通话已结束" : state === "error" ? "连接未完成" : "智能接听体验"}</span><time aria-hidden="true">{time}</time></div>
          <div className="voice-orbit" aria-hidden="true"><i /><i /><div>声</div></div>
          <h2>{info?.name || "温暖的服务助手"}</h2>
          <p>{state === "active" ? (muted ? "麦克风已静音。" : "请自然说话，我正在认真听。") : state === "connecting" ? "正在建立安全的语音连接…" : state === "completed" ? (serverReason === "time_limit" ? "本次体验时间已到，感谢您的交流。" : "感谢您的体验，希望这次交流让您感到自然、清晰。") : "准备好后，开始一段真实的语音交流。"}</p>
          {message ? <div className="voice-error" role="alert">{message}</div> : null}
          <div className="voice-actions">
            {state === "active" || state === "reconnecting" ? <><button className="mute-call" onClick={toggleMute} type="button">{muted ? "打开麦克风" : "静音"}</button><button className="end-call" onClick={endExperience} type="button">结束通话</button></> : null}
            {!live && state !== "completed" ? <button className="start-call" disabled={!info?.available || state === "requesting"} onClick={startExperience} type="button">{state === "requesting" ? "正在准备…" : "立即体验"}</button> : null}
            {state === "completed" || state === "error" ? <button className="start-call" onClick={startExperience} type="button">再次体验</button> : null}
          </div>
          {session ? <small>今日还可体验 {session.remaining_calls} 次</small> : <small>{info?.notice || "请勿在体验中提供密码、验证码或敏感个人信息。"}</small>}
        </section>
      </section>

      <section className="experience-steps" id="how">
        <div><span>01</span><h2>允许麦克风</h2><p>浏览器只在本次会话中使用您的声音。</p></div>
        <div><span>02</span><h2>自然表达</h2><p>像平常打电话一样说话，可以随时打断或补充。</p></div>
        <div><span>03</span><h2>随时结束</h2><p>您可以主动挂断，达到体验时长后系统也会礼貌结束。</p></div>
      </section>

      <section className="experience-boundaries" id="boundaries">
        <div><span className="section-kicker">清晰的边界</span><h2>这是一次轻量、独立的公开体验。</h2></div>
        <ul><li>明确告知您正在与智能助手交流</li><li>公开体验不会调用企业业务工具</li><li>不同访客之间不共享会话上下文</li><li>体验记录采用更短的数据保留周期</li></ul>
      </section>
    </main>
  );
}
