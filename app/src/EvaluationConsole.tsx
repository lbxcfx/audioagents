import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { inboundRequest, type InboundAgentSummary } from "./inboundApi";
import {
  loadPlatformAuth,
  platformAuthHeaders,
  type PlatformAuthSession,
} from "./platformAuth";

type Project = { id: string; name: string };
type TestSession = { token: string; url: string; session_id: string };
type Chat = { role: "user" | "assistant"; text: string };
const platformBase =
  import.meta.env.VITE_PLATFORM_API_BASE ||
  (import.meta.env.DEV ? "http://127.0.0.1:8091" : window.location.origin);
async function platform<T>(path: string, auth: PlatformAuthSession) {
  const response = await fetch(`${platformBase}${path}`, {
    headers: platformAuthHeaders(auth),
  });
  if (!response.ok) throw new Error("项目读取失败");
  return response.json() as Promise<T>;
}

export default function EvaluationConsole() {
  const auth = useMemo(() => loadPlatformAuth(), []);
  const [projects, setProjects] = useState<Project[]>([]),
    [projectId, setProjectId] = useState("");
  const [agents, setAgents] = useState<InboundAgentSummary[]>([]),
    [agentId, setAgentId] = useState("");
  const [state, setState] = useState<"idle" | "connecting" | "active">("idle"),
    [notice, setNotice] = useState("");
  const [messages, setMessages] = useState<Chat[]>([]);
  const [asset, setAsset] = useState<Record<string, string> | null>(null);
  const [confirmation, setConfirmation] = useState<Record<string, string> | null>(null);
  const [toolResult, setToolResult] = useState("");
  const roomRef = useRef<Room | null>(null),
    audioRef = useRef<HTMLAudioElement | null>(null);
  useEffect(() => {
    if (auth)
      platform<{ items: Project[] }>("/api/platform/projects", auth)
        .then(({ items }) => {
          setProjects(items);
          setProjectId(items[0]?.id || "");
        })
        .catch((error) => setNotice(String(error)));
  }, [auth]);
  useEffect(() => {
    if (auth && projectId)
      inboundRequest<{ items: InboundAgentSummary[] }>(
        `/inbound-api/projects/${projectId}/agents`,
        {},
        auth,
      ).then(({ items }) => {
        const published = items.filter((item) => item.status === "published");
        setAgents(published);
        setAgentId(published[0]?.id || "");
      });
  }, [projectId]);
  useEffect(
    () => () => {
      void roomRef.current?.disconnect();
    },
    [],
  );
  async function connect() {
    if (!auth || !agentId) return;
    setState("connecting");
    setNotice("");
    setMessages([]);
    try {
      const session = await inboundRequest<TestSession>(
        `/inbound-api/projects/${projectId}/agents/${agentId}/test-sessions`,
        { method: "POST" },
        auth,
      );
      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;
      room.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === Track.Kind.Audio && audioRef.current)
          track.attach(audioRef.current);
      });
      room.on(RoomEvent.Disconnected, () => {
        setState("idle");
      });
      room.registerTextStreamHandler("lk.transcription", async (reader) => {
        const text = await reader.readAll();
        setMessages((value) => [...value, { role: "assistant", text }]);
      });
      room.registerTextStreamHandler("inbound.content", async (reader) => {
        try {
          setAsset(JSON.parse(await reader.readAll()));
        } catch {
          setAsset(null);
        }
      });
      room.registerTextStreamHandler("inbound.tool.confirmation", async (reader) => setConfirmation(JSON.parse(await reader.readAll())));
      room.registerTextStreamHandler("inbound.tool.result", async (reader) => { const value = JSON.parse(await reader.readAll()); setToolResult(value.message || value.result || value.status); setConfirmation(null); });
      await room.connect(session.url, session.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setState("active");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "测试连接失败");
      setState("idle");
    }
  }
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget),
      text = String(form.get("text") || "").trim();
    if (!text || !roomRef.current) return;
    await roomRef.current.localParticipant.sendText(text, { topic: "lk.chat" });
    setMessages((value) => [...value, { role: "user", text }]);
    event.currentTarget.reset();
  }
  async function confirmTool() {
    if (!confirmation?.confirmation_id || !roomRef.current) return;
    await roomRef.current.localParticipant.sendText(JSON.stringify({ confirmation_id: confirmation.confirmation_id }), { topic: "inbound.tool.confirm" });
    setToolResult("正在执行已确认的业务操作…");
  }
  if (!auth)
    return (
      <main className="inbound-auth-required">
        <h1>请先登录控制台</h1>
        <a href="/login">前往登录</a>
      </main>
    );
  return (
    <main className="inbound-console">
      <aside className="inbound-sidebar">
        <a className="inbound-brand" href="/app/home">
          <img src="/assets/brand/call-logo.svg" alt="云声通" />
        </a>
        <a href="/app/inbound/agents">Agent 配置</a>
        <a href="/app/inbound/knowledge">知识库</a>
        <a href="/app/inbound/integrations">业务系统</a>
        <a className="active" href="/app/inbound/evaluation">
          体验与评测
        </a>
        <a href="/app/inbound/content">展示素材</a>
      </aside>
      <section className="inbound-workspace">
        <header className="inbound-topbar">
          <div>
            <span>智能客服</span>
            <strong>体验与评测</strong>
          </div>
          <select
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
          >
            {projects.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </header>
        {notice ? (
          <div className="inbound-notice" role="status">
            {notice}
          </div>
        ) : null}
        <div className="evaluation-page">
          <header>
            <div>
              <h1>真实链路测试</h1>
              <p>使用已发布版本和正式权限边界创建隔离 LiveKit 会话。</p>
            </div>
            <select
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
            >
              {agents.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {state !== "active" ? (
              <button
                disabled={!agentId || state === "connecting"}
                onClick={connect}
              >
                {state === "connecting" ? "连接中…" : "开始测试"}
              </button>
            ) : (
              <button onClick={() => roomRef.current?.disconnect()}>
                结束
              </button>
            )}
          </header>
          <audio ref={audioRef} autoPlay />
          <div className="evaluation-grid">
            <section className="evaluation-chat">
              <div>
                {messages.map((item, index) => (
                  <p className={item.role} key={`${item.role}-${index}`}>
                    <strong>{item.role === "user" ? "我" : "Agent"}</strong>
                    {item.text}
                  </p>
                ))}
              </div>
              <form onSubmit={send}>
                <input
                  name="text"
                  disabled={state !== "active"}
                  placeholder="输入知识或业务问题"
                  required
                />
                <button disabled={state !== "active"}>发送</button>
              </form>
            </section>
              <aside className="evaluation-inspector">
              <h2>素材与引用</h2>
              {asset ? (
                <article>
                  <strong>{asset.name}</strong>
                  <a href={asset.source_url} target="_blank" rel="noreferrer">
                    打开 {asset.kind}
                  </a>
                </article>
              ) : (
                <p>Agent 展示素材时会在这里显示。</p>
              )}
                {confirmation ? <article className="tool-confirmation"><strong>需要客户确认</strong><p>{confirmation.tool ? JSON.stringify(confirmation.tool) : "业务操作"}</p><button onClick={confirmTool}>确认执行</button><button onClick={() => setConfirmation(null)}>取消</button></article> : null}
                {toolResult ? <p role="status">{toolResult}</p> : null}
            </aside>
          </div>
        </div>
      </section>
    </main>
  );
}
