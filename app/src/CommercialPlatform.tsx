import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  beginOidcLogin,
  completeOidcLogin,
  oidcConfiguration,
  platformAuthHeaders,
  platformAuthSubject,
  savePlatformAuth,
  type PlatformAuthSession,
} from "./platformAuth";

type Project = { id: string; name: string; slug: string; role: string; retention_days: number };
type Metrics = {
  states: Record<string, number>;
  queue_depth: number;
  active_calls: number;
  stale_leases: number;
  attempts: { total: number; completed: number; failed: number };
};
type PlatformCall = {
  id: string;
  direction: "inbound" | "outbound";
  status: string;
  destination_number: string;
  source_number: string;
  agent_name: string;
  room_name: string;
  recording_status: string;
  created_at: string;
  failure_code: string;
};
type PlatformContact = { id: string; external_id: string; name: string; phone_number: string; status: string };
type PlatformCampaign = {
  id: string;
  name: string;
  status: string;
  contact_count: number;
  queued_count: number;
  terminal_count: number;
  blocked_count: number;
  max_concurrent_calls: number;
};
type Trunk = {
  id: string;
  name: string;
  direction: string;
  provider: string;
  livekit_trunk_id: string;
  status: string;
  numbers: string[];
  max_concurrent_calls: number;
  max_calls_per_second: number;
};
type Policy = {
  outbound_enabled: boolean;
  timezone: string;
  allowed_weekdays: number[];
  calling_window_start: string;
  calling_window_end: string;
  require_consent: boolean;
  consent_purpose: string;
  max_attempts_per_number_per_day: number;
  inbound_overflow_mode: "reject" | "transfer";
  inbound_overflow_destination_name: string;
  recording_mode: "off" | "always";
  recording_disclosure_text: string;
};
type Limits = {
  max_concurrent_calls: number;
  max_outbound_calls: number;
  max_inbound_calls: number;
  max_calls_per_minute: number;
  lease_seconds: number;
};
type Member = { user_id: string; role: string };
type Tab = "overview" | "calls" | "campaigns" | "configuration" | "access";

const emptyMetrics: Metrics = {
  states: {},
  queue_depth: 0,
  active_calls: 0,
  stale_leases: 0,
  attempts: { total: 0, completed: 0, failed: 0 },
};

function formData(event: FormEvent<HTMLFormElement>): Record<string, FormDataEntryValue> {
  return Object.fromEntries(new FormData(event.currentTarget));
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "操作失败，请稍后重试";
}

function displayTime(value: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

export default function CommercialPlatform({
  apiBase,
  auth,
  onAuthChange,
}: {
  apiBase: string;
  auth: PlatformAuthSession | null;
  onAuthChange: (session: PlatformAuthSession | null) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(() => sessionStorage.getItem("voicePlatformProject") || "");
  const [tab, setTab] = useState<Tab>("overview");
  const [metrics, setMetrics] = useState<Metrics>(emptyMetrics);
  const [calls, setCalls] = useState<PlatformCall[]>([]);
  const [contacts, setContacts] = useState<PlatformContact[]>([]);
  const [campaigns, setCampaigns] = useState<PlatformCampaign[]>([]);
  const [trunks, setTrunks] = useState<Trunk[]>([]);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const request = useMemo(() => {
    return async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
      const headers = new Headers(options.headers);
      Object.entries(platformAuthHeaders(auth)).forEach(([name, value]) => headers.set(name, value));
      if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      const response = await fetch(`${apiBase}${path}`, { ...options, headers });
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const raw = await response.text();
          const payload = JSON.parse(raw) as { detail?: unknown };
          detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload);
        } catch {
          // Keep the status text when an upstream proxy returns a non-JSON body.
        }
        throw new Error(detail);
      }
      if (response.status === 204) return {} as T;
      const text = await response.text();
      return (text ? JSON.parse(text) : {}) as T;
    };
  }, [apiBase, auth]);

  function announce(message: string) {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 3000);
  }

  async function loadProjects() {
    if (!auth) return;
    setLoading(true);
    setError("");
    try {
      const result = await request<{ items: Project[] }>("/api/platform/projects");
      setProjects(result.items);
      const selected = result.items.some((item) => item.id === projectId) ? projectId : result.items[0]?.id || "";
      setProjectId(selected);
      if (selected) sessionStorage.setItem("voicePlatformProject", selected);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setLoading(false);
    }
  }

  async function loadProject() {
    if (!auth || !projectId) return;
    setLoading(true);
    setError("");
    try {
      const prefix = `/api/platform/projects/${projectId}`;
      const [metricsData, callData, contactData, campaignData, trunkData, policyData, limitData] = await Promise.all([
        request<Metrics>(`${prefix}/telephony/metrics`),
        request<{ items: PlatformCall[] }>(`${prefix}/telephony/calls?limit=200`),
        request<{ items: PlatformContact[] }>(`${prefix}/telephony/contacts?limit=500`),
        request<{ items: PlatformCampaign[] }>(`${prefix}/telephony/campaigns?limit=200`),
        request<{ items: Trunk[] }>(`${prefix}/telephony/trunks`),
        request<Policy>(`${prefix}/telephony/policy`),
        request<Limits>(`${prefix}/telephony/limits`),
      ]);
      setMetrics(metricsData);
      setCalls(callData.items);
      setContacts(contactData.items);
      setCampaigns(campaignData.items);
      setTrunks(trunkData.items);
      setPolicy(policyData);
      setLimits(limitData);
      try {
        const memberData = await request<{ items: Member[] }>(`${prefix}/members`);
        setMembers(memberData.items);
      } catch {
        setMembers([]);
      }
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!window.location.search.includes("code=")) return;
    setBusy(true);
    completeOidcLogin()
      .then((session) => {
        if (session) onAuthChange(session);
      })
      .catch((value) => setError(errorMessage(value)))
      .finally(() => setBusy(false));
  }, [onAuthChange]);

  useEffect(() => {
    loadProjects();
  }, [auth, apiBase]);

  useEffect(() => {
    if (projectId) {
      sessionStorage.setItem("voicePlatformProject", projectId);
      loadProject();
    }
  }, [projectId, auth, apiBase]);

  async function perform(action: () => Promise<void>, success: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      await loadProject();
      announce(success);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    setBusy(true);
    setError("");
    try {
      const created = await request<Project>("/api/platform/projects", {
        method: "POST",
        body: JSON.stringify({ name: data.name, slug: data.slug, retention_days: Number(data.retention_days || 30) }),
      });
      form.reset();
      await loadProjects();
      setProjectId(created.id);
      announce("项目已创建");
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function createOutbound(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/calls/outbound`, {
        method: "POST",
        body: JSON.stringify({
          idempotency_key: crypto.randomUUID(),
          destination_number: data.destination_number,
          source_number: data.source_number || "",
          agent_name: data.agent_name,
          trunk_id: data.trunk_id || null,
          priority: Number(data.priority || 100),
          max_attempts: Number(data.max_attempts || 3),
        }),
      });
      form.reset();
    }, "外呼任务已进入可靠队列");
  }

  async function saveContact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const externalId = String(data.external_id);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/contacts/${encodeURIComponent(externalId)}`, {
        method: "PUT",
        body: JSON.stringify({
          external_id: externalId,
          phone_number: data.phone_number,
          name: data.name || "",
          status: data.status || "active",
          metadata: {},
        }),
      });
      form.reset();
    }, "联系人已保存");
  }

  async function createCampaign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const selected = Array.from(form.querySelectorAll<HTMLSelectElement>("select[name=contact_ids] option:checked")).map((item) => item.value);
    await perform(async () => {
      const created = await request<PlatformCampaign>(`/api/platform/projects/${projectId}/telephony/campaigns`, {
        method: "POST",
        body: JSON.stringify({
          name: data.name,
          agent_name: data.agent_name,
          trunk_id: data.trunk_id || null,
          source_number: data.source_number || "",
          max_attempts: Number(data.max_attempts || 3),
          max_concurrent_calls: Number(data.max_concurrent_calls || 10),
          priority: 100,
          metadata: {},
        }),
      });
      if (selected.length) {
        await request(`/api/platform/projects/${projectId}/telephony/campaigns/${created.id}/contacts`, {
          method: "POST",
          body: JSON.stringify({ contact_ids: selected }),
        });
      }
      if (data.start_now === "on") {
        await request(`/api/platform/projects/${projectId}/telephony/campaigns/${created.id}/status`, {
          method: "PUT",
          body: JSON.stringify({ status: "running" }),
        });
      }
      form.reset();
    }, "外呼活动已创建");
  }

  async function changeCampaign(campaignId: string, status: "running" | "paused" | "canceled") {
    await perform(
      () => request(`/api/platform/projects/${projectId}/telephony/campaigns/${campaignId}/status`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      }).then(() => undefined),
      status === "running" ? "活动已启动" : status === "paused" ? "活动已暂停" : "活动已取消",
    );
  }

  async function saveTrunk(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const name = String(data.name);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/trunks/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: JSON.stringify({
          name,
          direction: data.direction,
          provider: data.provider,
          livekit_trunk_id: data.livekit_trunk_id,
          secret_name: data.secret_name || "",
          status: "active",
          numbers: String(data.numbers || "").split(/[\s,，]+/).filter(Boolean),
          max_concurrent_calls: Number(data.max_concurrent_calls || 100),
          max_calls_per_second: Number(data.max_calls_per_second || 5),
        }),
      });
      form.reset();
    }, "SIP 线路已保存");
  }

  async function saveLimits(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = formData(event);
    await perform(
      () => request(`/api/platform/projects/${projectId}/telephony/limits`, {
        method: "PUT",
        body: JSON.stringify({
          max_concurrent_calls: Number(data.max_concurrent_calls),
          max_outbound_calls: Number(data.max_outbound_calls),
          max_inbound_calls: Number(data.max_inbound_calls),
          max_calls_per_minute: Number(data.max_calls_per_minute),
          lease_seconds: Number(data.lease_seconds),
        }),
      }).then(() => undefined),
      "并发与速率限制已更新",
    );
  }

  async function savePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = formData(event);
    await perform(
      () => request(`/api/platform/projects/${projectId}/telephony/policy`, {
        method: "PUT",
        body: JSON.stringify({
          outbound_enabled: data.outbound_enabled === "on",
          timezone: data.timezone,
          allowed_weekdays: String(data.allowed_weekdays).split(",").map(Number),
          calling_window_start: data.calling_window_start,
          calling_window_end: data.calling_window_end,
          require_consent: data.require_consent === "on",
          consent_purpose: data.consent_purpose,
          max_attempts_per_number_per_day: Number(data.max_attempts_per_number_per_day),
          inbound_overflow_mode: data.inbound_overflow_mode,
          inbound_overflow_destination_name: data.inbound_overflow_mode === "transfer" ? data.inbound_overflow_destination_name : "",
          recording_mode: data.recording_mode,
          recording_disclosure_text: data.recording_mode === "always" ? data.recording_disclosure_text : "",
        }),
      }).then(() => undefined),
      "呼叫合规策略已更新",
    );
  }

  async function saveMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const userId = String(data.user_id);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/members/${encodeURIComponent(userId)}`, {
        method: "PUT",
        body: JSON.stringify({ user_id: userId, role: data.role }),
      });
      form.reset();
    }, "成员权限已更新");
  }

  async function logout() {
    if (auth?.mode === "bearer") {
      try {
        await request("/api/platform/auth/revoke", {
          method: "POST",
          body: JSON.stringify({ reason: "user_logout" }),
        });
      } catch {
        // Local logout still clears the browser session when the control plane is unavailable.
      }
    }
    savePlatformAuth(null);
    onAuthChange(null);
    setProjects([]);
    setProjectId("");
  }

  if (!auth) {
    const oidcReady = Boolean(oidcConfiguration());
    return (
      <section className="commercial-auth" aria-labelledby="commercial-auth-title">
        <div className="commercial-auth-copy">
          <span className="commercial-eyebrow">COMMERCIAL VOICE CLOUD</span>
          <h1 id="commercial-auth-title">企业级语音运营平台</h1>
          <p>统一管理外呼活动、呼入客服、SIP 线路、录音合规、并发容量与租户权限。</p>
          <ul>
            <li>OIDC Authorization Code + PKCE 登录</li>
            <li>项目级 RBAC 与令牌主动吊销</li>
            <li>可靠呼叫队列、录音终态与实时运行指标</li>
          </ul>
        </div>
        <div className="commercial-auth-card">
          <h2>登录控制台</h2>
          <p>{oidcReady ? "使用企业身份提供商继续。" : "请先在构建环境配置企业 IdP。"}</p>
          <button className="commercial-primary" disabled={!oidcReady || busy} onClick={() => beginOidcLogin().catch((value) => setError(errorMessage(value)))} type="button">
            {busy ? "正在验证…" : "企业 IdP 登录"}
          </button>
          {import.meta.env.DEV || import.meta.env.VITE_ALLOW_DEVELOPMENT_AUTH === "true" ? (
            <form
              className="commercial-dev-auth"
              onSubmit={(event) => {
                event.preventDefault();
                const userId = String(formData(event).user_id || "").trim();
                if (!userId) return;
                const session: PlatformAuthSession = { mode: "development", userId };
                savePlatformAuth(session);
                onAuthChange(session);
              }}
            >
              <label>本地开发身份<input name="user_id" defaultValue="owner" required /></label>
              <button type="submit">开发模式进入</button>
            </form>
          ) : null}
          {error ? <div className="commercial-error" role="alert">{error}</div> : null}
        </div>
      </section>
    );
  }

  const selectedProject = projects.find((item) => item.id === projectId);
  const completionRate = metrics.attempts.total ? Math.round((metrics.attempts.completed / metrics.attempts.total) * 100) : 0;
  return (
    <section className="commercial-platform" aria-busy={loading || busy}>
      <header className="commercial-heading">
        <div>
          <span className="commercial-eyebrow">VOICE OPERATIONS</span>
          <h1>商用语音平台</h1>
          <p>外呼增长与呼入客服共用一套高并发控制平面。</p>
        </div>
        <div className="commercial-session">
          <span>{platformAuthSubject(auth)}</span>
          <button onClick={logout} type="button">安全退出</button>
        </div>
      </header>

      {notice ? <div className="commercial-notice" role="status">{notice}</div> : null}
      {error ? <div className="commercial-error" role="alert">{error}</div> : null}

      <div className="commercial-projectbar">
        <label>当前项目
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">请选择项目</option>
            {projects.map((project) => <option value={project.id} key={project.id}>{project.name} · {project.role}</option>)}
          </select>
        </label>
        <button onClick={loadProject} disabled={!projectId || loading} type="button">刷新运行数据</button>
        <span>{selectedProject ? `${selectedProject.slug} · 保留 ${selectedProject.retention_days} 天` : "尚未创建项目"}</span>
      </div>

      {!projectId ? (
        <form className="commercial-empty commercial-form" onSubmit={createProject}>
          <h2>创建首个租户项目</h2>
          <label>项目名称<input name="name" required maxLength={120} /></label>
          <label>项目标识<input name="slug" required pattern="[A-Za-z0-9-]+" /></label>
          <label>数据保留天数<input name="retention_days" type="number" defaultValue={30} min={1} max={3650} /></label>
          <button className="commercial-primary" disabled={busy} type="submit">创建项目</button>
        </form>
      ) : (
        <>
          <nav className="commercial-tabs" aria-label="商用语音平台功能">
            {(["overview", "calls", "campaigns", "configuration", "access"] as Tab[]).map((item) => (
              <button className={tab === item ? "active" : ""} onClick={() => setTab(item)} key={item} type="button">
                {{ overview: "运行概览", calls: "实时呼叫", campaigns: "活动与客户", configuration: "线路与策略", access: "成员权限" }[item]}
              </button>
            ))}
          </nav>

          {tab === "overview" ? (
            <div className="commercial-stack">
              <div className="commercial-kpis">
                <article><span>排队呼叫</span><strong>{metrics.queue_depth}</strong><small>可靠队列深度</small></article>
                <article><span>当前并发</span><strong>{metrics.active_calls}</strong><small>呼入 + 外呼</small></article>
                <article><span>完成率</span><strong>{completionRate}%</strong><small>{metrics.attempts.total} 次尝试</small></article>
                <article className={metrics.stale_leases ? "attention" : ""}><span>过期租约</span><strong>{metrics.stale_leases}</strong><small>应保持为 0</small></article>
              </div>
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={createOutbound}>
                  <div className="commercial-panel-title"><div><span>QUICK ACTION</span><h2>发起单次外呼</h2></div><b>OUT</b></div>
                  <label>被叫号码<input name="destination_number" type="tel" placeholder="+8613800000000" required /></label>
                  <label>Agent 名称<input name="agent_name" defaultValue="commercial-agent" required /></label>
                  <label>外呼线路<select name="trunk_id" required><option value="">请选择</option>{trunks.filter((item) => item.direction !== "inbound" && item.status === "active").map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
                  <label>主叫号码<input name="source_number" type="tel" placeholder="线路允许的号码" /></label>
                  <div className="commercial-inline"><label>优先级<input name="priority" type="number" defaultValue={100} min={0} max={1000} /></label><label>最大尝试<input name="max_attempts" type="number" defaultValue={3} min={1} max={10} /></label></div>
                  <button className="commercial-primary" disabled={busy || !trunks.length} type="submit">加入外呼队列</button>
                  {!trunks.length ? <small>请先在“线路与策略”中配置可用的 LiveKit SIP Trunk。</small> : null}
                </form>
                <section className="commercial-panel">
                  <div className="commercial-panel-title"><div><span>LIVE CAPACITY</span><h2>双向通话容量</h2></div><b>IN / OUT</b></div>
                  <dl className="commercial-capacity">
                    <div><dt>总并发上限</dt><dd>{limits?.max_concurrent_calls ?? "—"}</dd></div>
                    <div><dt>外呼并发</dt><dd>{limits?.max_outbound_calls ?? "—"}</dd></div>
                    <div><dt>呼入并发</dt><dd>{limits?.max_inbound_calls ?? "—"}</dd></div>
                    <div><dt>每分钟呼叫</dt><dd>{limits?.max_calls_per_minute ?? "—"}</dd></div>
                  </dl>
                  <p className="commercial-muted">呼入超过容量时按当前策略{policy?.inbound_overflow_mode === "transfer" ? "转接到备用坐席" : "拒绝并记录原因"}。</p>
                </section>
              </div>
            </div>
          ) : null}

          {tab === "calls" ? (
            <section className="commercial-panel">
              <div className="commercial-panel-title"><div><span>CALL LEDGER</span><h2>最近呼叫</h2></div><b>{calls.length}</b></div>
              <div className="commercial-table-wrap"><table><thead><tr><th>方向</th><th>号码</th><th>状态</th><th>Agent / 房间</th><th>录音</th><th>创建时间</th></tr></thead><tbody>
                {calls.map((call) => <tr key={call.id}><td><span className={`direction ${call.direction}`}>{call.direction === "inbound" ? "呼入" : "外呼"}</span></td><td>{call.direction === "inbound" ? call.source_number : call.destination_number}</td><td><span className={`commercial-status ${call.status}`}>{call.status}</span>{call.failure_code ? <small>{call.failure_code}</small> : null}</td><td>{call.agent_name}<small>{call.room_name || "等待分配房间"}</small></td><td>{call.recording_status || "关闭"}</td><td>{displayTime(call.created_at)}</td></tr>)}
                {!calls.length ? <tr><td colSpan={6} className="commercial-empty-row">暂无呼叫记录</td></tr> : null}
              </tbody></table></div>
            </section>
          ) : null}

          {tab === "campaigns" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveContact}><div className="commercial-panel-title"><div><span>CRM</span><h2>新增或更新联系人</h2></div></div><label>外部 ID<input name="external_id" required /></label><label>姓名<input name="name" /></label><label>号码<input name="phone_number" type="tel" required placeholder="+8613800000000" /></label><label>状态<select name="status"><option value="active">可联系</option><option value="suppressed">禁止联系</option></select></label><button className="commercial-primary" disabled={busy} type="submit">保存联系人</button></form>
                <form className="commercial-panel commercial-form" onSubmit={createCampaign}><div className="commercial-panel-title"><div><span>CAMPAIGN</span><h2>创建外呼活动</h2></div></div><label>活动名称<input name="name" required /></label><label>Agent 名称<input name="agent_name" defaultValue="commercial-agent" required /></label><div className="commercial-inline"><label>线路<select name="trunk_id" required><option value="">请选择</option>{trunks.filter((item) => item.direction !== "inbound").map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>主叫号码<input name="source_number" /></label></div><div className="commercial-inline"><label>活动并发<input name="max_concurrent_calls" type="number" defaultValue={10} min={1} /></label><label>最大尝试<input name="max_attempts" type="number" defaultValue={3} min={1} max={10} /></label></div><label>选择联系人<select name="contact_ids" multiple size={5}>{contacts.filter((item) => item.status === "active").map((item) => <option value={item.id} key={item.id}>{item.name || item.external_id} · {item.phone_number}</option>)}</select></label><label className="commercial-check"><input name="start_now" type="checkbox" /> 创建后立即启动</label><button className="commercial-primary" disabled={busy || !contacts.length || !trunks.length} type="submit">创建活动</button></form>
              </div>
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>CAMPAIGN QUEUE</span><h2>活动运行状态</h2></div><b>{campaigns.length}</b></div><div className="commercial-campaign-grid">{campaigns.map((campaign) => <article key={campaign.id}><header><div><h3>{campaign.name}</h3><span className={`commercial-status ${campaign.status}`}>{campaign.status}</span></div><strong>{campaign.terminal_count || 0}/{campaign.contact_count || 0}</strong></header><div className="campaign-track"><i style={{ width: `${campaign.contact_count ? Math.min(100, ((campaign.terminal_count || 0) / campaign.contact_count) * 100) : 0}%` }} /></div><p>排队 {campaign.queued_count || 0} · 拦截 {campaign.blocked_count || 0} · 并发 {campaign.max_concurrent_calls}</p><footer>{campaign.status === "running" ? <button onClick={() => changeCampaign(campaign.id, "paused")} type="button">暂停</button> : campaign.status !== "completed" && campaign.status !== "canceled" ? <button onClick={() => changeCampaign(campaign.id, "running")} type="button">启动</button> : null}{!(["completed", "canceled"] as string[]).includes(campaign.status) ? <button className="danger" onClick={() => changeCampaign(campaign.id, "canceled")} type="button">取消</button> : null}</footer></article>)}</div></section>
            </div>
          ) : null}

          {tab === "configuration" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveTrunk}><div className="commercial-panel-title"><div><span>SIP CONNECTIVITY</span><h2>配置 LiveKit 线路</h2></div></div><label>线路名称<input name="name" required /></label><div className="commercial-inline"><label>方向<select name="direction"><option value="bidirectional">双向</option><option value="outbound">仅外呼</option><option value="inbound">仅呼入</option></select></label><label>供应商<input name="provider" required placeholder="carrier" /></label></div><label>LiveKit Trunk ID<input name="livekit_trunk_id" required placeholder="ST_..." /></label><label>Secret 名称<input name="secret_name" placeholder="k8s-secret-name" /></label><label>允许的主叫/接入号码<input name="numbers" placeholder="+8610..., +8621..." /></label><div className="commercial-inline"><label>线路并发<input name="max_concurrent_calls" type="number" defaultValue={100} min={1} /></label><label>每秒呼叫<input name="max_calls_per_second" type="number" defaultValue={5} min={1} /></label></div><button className="commercial-primary" disabled={busy} type="submit">保存线路</button></form>
                {limits ? <form className="commercial-panel commercial-form" onSubmit={saveLimits}><div className="commercial-panel-title"><div><span>CAPACITY GUARDRAILS</span><h2>并发与速率</h2></div></div><label>总并发<input name="max_concurrent_calls" type="number" defaultValue={limits.max_concurrent_calls} min={1} /></label><div className="commercial-inline"><label>外呼并发<input name="max_outbound_calls" type="number" defaultValue={limits.max_outbound_calls} min={1} /></label><label>呼入并发<input name="max_inbound_calls" type="number" defaultValue={limits.max_inbound_calls} min={1} /></label></div><label>每分钟呼叫<input name="max_calls_per_minute" type="number" defaultValue={limits.max_calls_per_minute} min={1} /></label><label>租约秒数<input name="lease_seconds" type="number" defaultValue={limits.lease_seconds} min={10} max={300} /></label><button className="commercial-primary" disabled={busy} type="submit">更新容量</button></form> : null}
              </div>
              {policy ? <form className="commercial-panel commercial-form policy-form" onSubmit={savePolicy}><div className="commercial-panel-title"><div><span>COMPLIANCE POLICY</span><h2>呼叫合规与录音</h2></div></div><div className="commercial-form-grid"><label>时区<input name="timezone" defaultValue={policy.timezone} /></label><label>工作日（0=周一）<input name="allowed_weekdays" defaultValue={policy.allowed_weekdays.join(",")} /></label><label>开始时间<input name="calling_window_start" type="time" defaultValue={policy.calling_window_start} /></label><label>结束时间<input name="calling_window_end" type="time" defaultValue={policy.calling_window_end} /></label><label>同意用途<input name="consent_purpose" defaultValue={policy.consent_purpose} /></label><label>每日同号码尝试<input name="max_attempts_per_number_per_day" type="number" defaultValue={policy.max_attempts_per_number_per_day} min={1} max={100} /></label><label>呼入溢出<select name="inbound_overflow_mode" defaultValue={policy.inbound_overflow_mode}><option value="reject">拒绝</option><option value="transfer">转接</option></select></label><label>溢出目标<input name="inbound_overflow_destination_name" defaultValue={policy.inbound_overflow_destination_name} /></label><label>录音模式<select name="recording_mode" defaultValue={policy.recording_mode}><option value="off">关闭</option><option value="always">始终录音</option></select></label><label>录音告知语<input name="recording_disclosure_text" defaultValue={policy.recording_disclosure_text} /></label></div><div className="commercial-check-row"><label className="commercial-check"><input name="outbound_enabled" type="checkbox" defaultChecked={policy.outbound_enabled} /> 允许外呼</label><label className="commercial-check"><input name="require_consent" type="checkbox" defaultChecked={policy.require_consent} /> 强制校验用户同意</label></div><button className="commercial-primary" disabled={busy} type="submit">保存合规策略</button></form> : null}
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>ACTIVE TRUNKS</span><h2>线路清单</h2></div><b>{trunks.length}</b></div><div className="commercial-trunks">{trunks.map((trunk) => <article key={trunk.id}><div><h3>{trunk.name}</h3><p>{trunk.provider} · {trunk.direction} · {trunk.livekit_trunk_id}</p></div><strong>{trunk.max_concurrent_calls} 并发 / {trunk.max_calls_per_second} CPS</strong></article>)}</div></section>
            </div>
          ) : null}

          {tab === "access" ? (
            <div className="commercial-two-column">
              <form className="commercial-panel commercial-form" onSubmit={saveMember}><div className="commercial-panel-title"><div><span>PROJECT RBAC</span><h2>添加或调整成员</h2></div></div><label>IdP 用户标识<input name="user_id" required /></label><label>角色<select name="role"><option value="viewer">只读观察者</option><option value="member">业务成员</option><option value="admin">管理员</option><option value="owner">所有者</option></select></label><button className="commercial-primary" disabled={busy} type="submit">保存权限</button><small>系统会在事务内锁定项目，阻止并发操作移除最后一位所有者。</small></form>
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>MEMBERS</span><h2>项目成员</h2></div><b>{members.length}</b></div>{members.length ? <div className="commercial-members">{members.map((member) => <div key={member.user_id}><span>{member.user_id}</span><strong>{member.role}</strong></div>)}</div> : <p className="commercial-muted">当前角色无权读取成员列表。</p>}</section>
            </div>
          ) : null}
        </>
      )}
      {(loading || busy) ? <div className="commercial-progress" aria-label="正在处理" /> : null}
    </section>
  );
}
