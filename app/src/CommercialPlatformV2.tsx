import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  beginOidcLogin,
  completeOidcLogin,
  oidcConfiguration,
  platformAuthHeaders,
  platformAuthSubject,
  refreshPlatformAuth,
  savePlatformAuth,
  type PlatformAuthSession,
} from "./platformAuth";

type JsonObject = Record<string, unknown>;
type Role = "owner" | "admin" | "member" | "viewer" | "worker";
type Project = { id: string; name: string; slug: string; role: Role; retention_days: number };
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
  provider_call_id?: string;
  recording_status: string;
  recording_storage_uri?: string;
  recording_egress_id?: string;
  created_at: string;
  answered_at?: string;
  ended_at?: string;
  failure_code: string;
  failure_detail?: string;
  attempt_count?: number;
  max_attempts?: number;
  metadata?: JsonObject;
};
type PlatformContact = {
  id: string;
  external_id: string;
  name: string;
  phone_number: string;
  status: string;
  metadata?: JsonObject;
  updated_at?: string;
};
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
  secret_name?: string;
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
type Member = { user_id: string; role: Role; created_at?: string };
type DncEntry = {
  id: string;
  phone_last4: string;
  reason: string;
  source: string;
  expires_at?: string;
  created_at: string;
};
type Consent = {
  id: string;
  phone_last4: string;
  purpose: string;
  status: string;
  evidence_ref: string;
  valid_from: string;
  valid_until?: string;
  created_at: string;
};
type TransferDestination = {
  id: string;
  name: string;
  target_uri: string;
  mode: string;
  status: string;
};
type AuditLog = {
  id: string;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  payload?: JsonObject;
  created_at: string;
};
type Session = {
  id: string;
  room_name: string;
  agent_name: string;
  status: string;
  started_at: string;
  ended_at?: string;
  cost_usd?: number;
  event_count?: number;
};
type Timeline = {
  session: Session;
  events: Array<{ id: string; sequence: number; event_type: string; source: string; payload: JsonObject; occurred_at: string }>;
  usage: Array<{ id: string; category: string; provider: string; model: string; quantity: number; unit: string; cost_usd: number; latency_ms?: number }>;
  summary: { event_count: number; usage_count: number; cost_usd: number };
};
type ConsoleCommand = {
  id: string;
  command_type: string;
  payload: JsonObject;
  status: string;
  result?: JsonObject;
  created_at: string;
  completed_at?: string;
};
type AnalyticsSummary = {
  sessions: { total: number; active: number; completed: number; failed: number; avg_duration_seconds: number };
  usage: Array<{ category: string; provider: string; model: string; unit: string; quantity: number; cost_usd: number; avg_latency_ms: number; request_count: number }>;
  inference: { attempts: number; succeeded: number; success_rate: number; avg_latency_ms: number };
  events: Record<string, number>;
};
type AgentSpecRecord = { id: string; name: string; revision: number; status: string; spec: JsonObject; updated_at: string };
type AgentRecord = { id: string; name: string; description: string; status: string; created_at: string };
type AgentVersion = { id: string; agent_id: string; version_number: number; image_ref: string; status: string; created_at: string };
type Deployment = {
  id: string;
  name: string;
  agent_id: string;
  active_version_id: string;
  previous_version_id?: string;
  desired_replicas: number;
  status: string;
  updated_at: string;
};
type SecretRecord = { name: string; updated_at: string; created_at?: string };
type EmbedConfig = {
  id: string;
  name: string;
  agent_name: string;
  room_prefix: string;
  allowed_origins: string[];
  capabilities: Record<string, boolean>;
  enabled: boolean;
};
type InferenceRoute = {
  id: string;
  descriptor: string;
  modality: string;
  provider: string;
  provider_model: string;
  priority: number;
  timeout_seconds: number;
  enabled: boolean;
  config: JsonObject;
};
type Tab =
  | "overview"
  | "calls"
  | "campaigns"
  | "compliance"
  | "configuration"
  | "analytics"
  | "sessions"
  | "agents"
  | "integrations"
  | "access";

const emptyMetrics: Metrics = {
  states: {},
  queue_depth: 0,
  active_calls: 0,
  stale_leases: 0,
  attempts: { total: 0, completed: 0, failed: 0 },
};

const tabLabels: Record<Tab, string> = {
  overview: "运行概览",
  calls: "实时呼叫",
  campaigns: "活动与客户",
  compliance: "合规中心",
  configuration: "线路与策略",
  analytics: "分析报表",
  sessions: "会话与控制",
  agents: "Agent 与部署",
  integrations: "模型与集成",
  access: "权限与审计",
};

function formData(event: FormEvent<HTMLFormElement>): Record<string, FormDataEntryValue> {
  return Object.fromEntries(new FormData(event.currentTarget));
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "操作失败，请稍后重试";
}

function displayTime(value?: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJson(value: FormDataEntryValue | null, fallback: JsonObject = {}): JsonObject {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = JSON.parse(text) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON 配置必须是对象");
  }
  return parsed as JsonObject;
}

function csvRows(input: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(cell.trim());
      cell = "";
    } else if (char === "\n") {
      row.push(cell.trim());
      if (row.some(Boolean)) rows.push(row);
      row = [];
      cell = "";
    } else if (char !== "\r") {
      cell += char;
    }
  }
  row.push(cell.trim());
  if (row.some(Boolean)) rows.push(row);
  return rows;
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export default function CommercialPlatformV2({
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
  const [dnc, setDnc] = useState<DncEntry[]>([]);
  const [consents, setConsents] = useState<Consent[]>([]);
  const [destinations, setDestinations] = useState<TransferDestination[]>([]);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [analytics, setAnalytics] = useState<AnalyticsSummary | null>(null);
  const [analyticsSessions, setAnalyticsSessions] = useState<Session[]>([]);
  const [analyticsCursor, setAnalyticsCursor] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [consoleCommands, setConsoleCommands] = useState<ConsoleCommand[]>([]);
  const [selectedCallId, setSelectedCallId] = useState("");
  const [callDetail, setCallDetail] = useState<PlatformCall | null>(null);
  const [cdr, setCdr] = useState<JsonObject | null>(null);
  const [callTransfers, setCallTransfers] = useState<JsonObject[]>([]);
  const [recordingAccess, setRecordingAccess] = useState<{ url: string; temporary: boolean; expires_at?: string | null } | null>(null);
  const [agentSpecs, setAgentSpecs] = useState<AgentSpecRecord[]>([]);
  const [editingSpec, setEditingSpec] = useState<AgentSpecRecord | null>(null);
  const [agents, setAgents] = useState<AgentRecord[]>([]);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [runtimeView, setRuntimeView] = useState<{ deploymentId: string; instances: JsonObject[]; logs: JsonObject[] } | null>(null);
  const [secrets, setSecrets] = useState<SecretRecord[]>([]);
  const [embedConfigs, setEmbedConfigs] = useState<EmbedConfig[]>([]);
  const [inferenceRoutes, setInferenceRoutes] = useState<InferenceRoute[]>([]);
  const [inferenceResult, setInferenceResult] = useState<JsonObject | null>(null);
  const [contactSearch, setContactSearch] = useState("");
  const [contactPage, setContactPage] = useState(1);
  const [contactCursor, setContactCursor] = useState("");
  const [contactNextCursor, setContactNextCursor] = useState<string | null>(null);
  const [contactCursorHistory, setContactCursorHistory] = useState<string[]>([]);
  const [selectedContactIds, setSelectedContactIds] = useState<string[]>([]);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastRefreshedAt, setLastRefreshedAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refreshingRef = useRef(false);
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;

  const request = useMemo(() => {
    return async function api<T>(
      path: string,
      options: RequestInit = {},
      retry = true,
      authOverride: PlatformAuthSession | null = null,
    ): Promise<T> {
      let activeAuth = authOverride || auth;
      if (activeAuth?.mode === "bearer") {
        try {
          const refreshed = await refreshPlatformAuth(activeAuth);
          if (refreshed !== activeAuth) {
            activeAuth = refreshed;
            onAuthChange(refreshed);
          }
        } catch (value) {
          if (activeAuth?.mode === "bearer" && activeAuth.expiresAt && activeAuth.expiresAt <= Date.now()) {
            savePlatformAuth(null);
            onAuthChange(null);
            throw value;
          }
        }
      }
      const headers = new Headers(options.headers);
      Object.entries(platformAuthHeaders(activeAuth)).forEach(([name, value]) => headers.set(name, value));
      if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      const response = await fetch(`${apiBase}${path}`, { ...options, headers });
      if (response.status === 401 && retry && activeAuth?.mode === "bearer" && activeAuth.refreshToken) {
        const refreshed = await refreshPlatformAuth(activeAuth, true);
        onAuthChange(refreshed);
        const nextHeaders = new Headers(options.headers);
        Object.entries(platformAuthHeaders(refreshed)).forEach(([name, value]) => nextHeaders.set(name, value));
        if (options.body && !(options.body instanceof FormData) && !nextHeaders.has("Content-Type")) {
          nextHeaders.set("Content-Type", "application/json");
        }
        return api<T>(path, { ...options, headers: nextHeaders }, false, refreshed);
      }
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const payload = JSON.parse(await response.text()) as { detail?: unknown };
          detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload);
        } catch {
          // Preserve the HTTP status if an upstream proxy did not return JSON.
        }
        throw new Error(detail);
      }
      if (response.status === 204) return {} as T;
      const text = await response.text();
      return (text ? JSON.parse(text) : {}) as T;
    };
  }, [apiBase, auth, onAuthChange]);

  const optional = useCallback(async <T,>(path: string, fallback: T): Promise<T> => {
    try {
      return await request<T>(path);
    } catch {
      return fallback;
    }
  }, [request]);

  function announce(message: string) {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 3500);
  }

  const selectedProject = projects.find((item) => item.id === projectId);
  const role = selectedProject?.role || "viewer";
  const canOperate = role === "owner" || role === "admin" || role === "member";
  const canManage = role === "owner" || role === "admin";
  const canAgentWrite = role === "owner" || role === "admin" || role === "member";
  const canConsole = role === "owner" || role === "admin" || role === "member";

  const loadProjects = useCallback(async () => {
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
  }, [auth, projectId, request]);

  const refreshRealtime = useCallback(async () => {
    if (!auth || !projectId || refreshingRef.current) return;
    refreshingRef.current = true;
    const requestedProjectId = projectId;
    const prefix = `/api/platform/projects/${requestedProjectId}`;
    try {
      const [nextMetrics, nextCalls, nextCampaigns] = await Promise.all([
        request<Metrics>(`${prefix}/telephony/metrics`),
        request<{ items: PlatformCall[] }>(`${prefix}/telephony/calls?limit=500`),
        request<{ items: PlatformCampaign[] }>(`${prefix}/telephony/campaigns?limit=500`),
      ]);
      if (projectIdRef.current !== requestedProjectId) return;
      setMetrics(nextMetrics);
      setCalls(nextCalls.items);
      setCampaigns(nextCampaigns.items);
      setLastRefreshedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      if (selectedCallId) {
        const next = nextCalls.items.find((item) => item.id === selectedCallId);
        if (next) setCallDetail((current) => ({ ...(current || next), ...next }));
      }
    } catch (value) {
      if (projectIdRef.current === requestedProjectId) setError(errorMessage(value));
    } finally {
      refreshingRef.current = false;
    }
  }, [auth, projectId, request, selectedCallId]);

  const loadContactPage = useCallback(async (search: string, cursor = "") => {
    if (!auth || !projectId) return;
    const requestedProjectId = projectId;
    const params = new URLSearchParams({ limit: "25" });
    if (search.trim()) params.set("search", search.trim());
    if (cursor) params.set("cursor", cursor);
    try {
      const result = await request<{ items: PlatformContact[]; next_cursor: string | null }>(
        `/api/platform/projects/${requestedProjectId}/telephony/contacts?${params}`,
      );
      if (projectIdRef.current !== requestedProjectId) return;
      setContacts(result.items);
      setContactNextCursor(result.next_cursor);
    } catch (value) {
      if (projectIdRef.current === requestedProjectId) setError(errorMessage(value));
    }
  }, [auth, projectId, request]);

  const loadProject = useCallback(async () => {
    if (!auth || !projectId) return;
    const requestedProjectId = projectId;
    setLoading(true);
    setError("");
    const prefix = `/api/platform/projects/${requestedProjectId}`;
    try {
      const [metricsData, callData, contactData, campaignData, trunkData, policyData, limitData] = await Promise.all([
        request<Metrics>(`${prefix}/telephony/metrics`),
        request<{ items: PlatformCall[] }>(`${prefix}/telephony/calls?limit=500`),
        request<{ items: PlatformContact[]; next_cursor: string | null }>(`${prefix}/telephony/contacts?limit=25&search=${encodeURIComponent(contactSearch.trim())}`),
        request<{ items: PlatformCampaign[] }>(`${prefix}/telephony/campaigns?limit=500`),
        request<{ items: Trunk[] }>(`${prefix}/telephony/trunks`),
        request<Policy>(`${prefix}/telephony/policy`),
        request<Limits>(`${prefix}/telephony/limits`),
      ]);
      if (projectIdRef.current !== requestedProjectId) return;
      setMetrics(metricsData);
      setCalls(callData.items);
      setContacts(contactData.items);
      setContactNextCursor(contactData.next_cursor);
      setContactCursor("");
      setContactCursorHistory([]);
      setContactPage(1);
      setCampaigns(campaignData.items);
      setTrunks(trunkData.items);
      setPolicy(policyData);
      setLimits(limitData);
      setLastRefreshedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));

      const results = await Promise.all([
        optional<{ items: Member[] }>(`${prefix}/members`, { items: [] }),
        optional<{ items: DncEntry[] }>(`${prefix}/telephony/do-not-call?active_only=false&limit=500`, { items: [] }),
        optional<{ items: Consent[] }>(`${prefix}/telephony/consents?limit=500`, { items: [] }),
        optional<{ items: TransferDestination[] }>(`${prefix}/telephony/transfer-destinations`, { items: [] }),
        optional<{ items: AuditLog[] }>(`${prefix}/audit-logs?limit=500`, { items: [] }),
        optional<AnalyticsSummary | null>(`${prefix}/analytics/summary`, null),
        optional<{ items: Session[]; next_cursor: string | null }>(`${prefix}/analytics/sessions?limit=100`, { items: [], next_cursor: null }),
        optional<{ items: Session[] }>(`${prefix}/sessions?limit=200`, { items: [] }),
        optional<{ items: AgentSpecRecord[] }>(`${prefix}/agent-specs`, { items: [] }),
        optional<{ items: AgentRecord[] }>(`${prefix}/agents`, { items: [] }),
        optional<{ items: Deployment[] }>(`${prefix}/deployments`, { items: [] }),
        optional<{ items: SecretRecord[] }>(`${prefix}/secrets`, { items: [] }),
        optional<{ items: EmbedConfig[] }>(`${prefix}/embed-configs`, { items: [] }),
        optional<{ items: InferenceRoute[] }>(`${prefix}/inference/routes`, { items: [] }),
      ]);
      if (projectIdRef.current !== requestedProjectId) return;
      setMembers(results[0].items);
      setDnc(results[1].items);
      setConsents(results[2].items);
      setDestinations(results[3].items);
      setAuditLogs(results[4].items);
      setAnalytics(results[5]);
      setAnalyticsSessions(results[6].items);
      setAnalyticsCursor(results[6].next_cursor);
      setSessions(results[7].items);
      setAgentSpecs(results[8].items);
      setAgents(results[9].items);
      setDeployments(results[10].items);
      setSecrets(results[11].items);
      setEmbedConfigs(results[12].items);
      setInferenceRoutes(results[13].items);
    } catch (value) {
      if (projectIdRef.current === requestedProjectId) setError(errorMessage(value));
    } finally {
      if (projectIdRef.current === requestedProjectId) setLoading(false);
    }
  }, [auth, contactSearch, optional, projectId, request]);

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
      setSelectedCallId("");
      setSelectedSessionId("");
      setSelectedContactIds([]);
      loadProject();
    }
  }, [projectId, auth, apiBase]);

  useEffect(() => {
    if (!autoRefresh || !auth || !projectId) return undefined;
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refreshRealtime();
    };
    const interval = window.setInterval(refreshWhenVisible, 5000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [autoRefresh, auth, projectId, refreshRealtime]);

  useEffect(() => {
    if (!auth || !projectId) return undefined;
    const timeout = window.setTimeout(() => {
      setContactCursor("");
      setContactCursorHistory([]);
      setContactPage(1);
      loadContactPage(contactSearch, "");
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [auth, contactSearch, loadContactPage, projectId]);

  useEffect(() => {
    if (auth?.mode !== "bearer" || !auth.expiresAt) return undefined;
    const delay = Math.max(1000, auth.expiresAt - Date.now() - 60_000);
    const timeout = window.setTimeout(() => {
      refreshPlatformAuth(auth)
        .then(onAuthChange)
        .catch((value) => setError(errorMessage(value)));
    }, delay);
    return () => window.clearTimeout(timeout);
  }, [auth, onAuthChange]);

  useEffect(() => {
    if (!autoRefresh || !selectedSessionId || !projectId) return undefined;
    const refreshSession = async () => {
      if (document.visibilityState !== "visible") return;
      const prefix = `/api/platform/projects/${projectId}/sessions/${selectedSessionId}`;
      try {
        const [nextTimeline, commands] = await Promise.all([
          request<Timeline>(prefix),
          optional<{ items: ConsoleCommand[] }>(`${prefix}/console/commands`, { items: [] }),
        ]);
        setTimeline(nextTimeline);
        setConsoleCommands(commands.items);
      } catch {
        // The main page error channel is reserved for explicit user actions.
      }
    };
    const interval = window.setInterval(refreshSession, 3000);
    return () => window.clearInterval(interval);
  }, [autoRefresh, optional, projectId, request, selectedSessionId]);

  async function perform(action: () => Promise<void>, success: string, reload = true) {
    setBusy(true);
    setError("");
    try {
      await action();
      if (reload) await loadProject();
      announce(success);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function downloadAuthenticated(path: string, filename: string) {
    let activeAuth = auth;
    if (!activeAuth) throw new Error("请先登录");
    if (activeAuth.mode === "bearer") {
      activeAuth = await refreshPlatformAuth(activeAuth);
      onAuthChange(activeAuth);
    }
    let response = await fetch(`${apiBase}${path}`, { headers: platformAuthHeaders(activeAuth) });
    if (response.status === 401 && activeAuth.mode === "bearer" && activeAuth.refreshToken) {
      activeAuth = await refreshPlatformAuth(activeAuth, true);
      onAuthChange(activeAuth);
      response = await fetch(`${apiBase}${path}`, { headers: platformAuthHeaders(activeAuth) });
    }
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    downloadBlob(await response.blob(), filename);
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
          metadata: parseJson(data.metadata),
        }),
      });
      form.reset();
    }, "联系人已保存");
  }

  async function importContacts(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = new FormData(form).get("file");
    if (!(file instanceof File) || !file.size) {
      setError("请选择 CSV 或 JSON 文件");
      return;
    }
    await perform(async () => {
      const text = await file.text();
      let imported: Array<{ external_id: string; phone_number: string; name: string; status: string; metadata: JsonObject }>;
      if (file.name.toLowerCase().endsWith(".json")) {
        const parsed = JSON.parse(text) as unknown;
        if (!Array.isArray(parsed)) throw new Error("JSON 文件必须包含联系人数组");
        imported = parsed.map((item) => {
          const row = item as JsonObject;
          return {
            external_id: String(row.external_id || ""),
            phone_number: String(row.phone_number || ""),
            name: String(row.name || ""),
            status: String(row.status || "active"),
            metadata: (row.metadata && typeof row.metadata === "object" ? row.metadata : {}) as JsonObject,
          };
        });
      } else {
        const rows = csvRows(text);
        const headers = (rows.shift() || []).map((item) => item.toLowerCase());
        const column = (name: string) => headers.indexOf(name);
        if (column("external_id") < 0 || column("phone_number") < 0) {
          throw new Error("CSV 必须包含 external_id 和 phone_number 列");
        }
        imported = rows.map((row) => ({
          external_id: row[column("external_id")] || "",
          phone_number: row[column("phone_number")] || "",
          name: column("name") >= 0 ? row[column("name")] || "" : "",
          status: column("status") >= 0 ? row[column("status")] || "active" : "active",
          metadata: {},
        }));
      }
      if (!imported.length) throw new Error("导入文件没有有效联系人");
      if (imported.length > 1000) throw new Error("单次最多导入 1000 位联系人");
      if (imported.some((item) => !item.external_id || !item.phone_number)) {
        throw new Error("external_id 和 phone_number 不能为空");
      }
      await request(`/api/platform/projects/${projectId}/telephony/contacts/import`, {
        method: "POST",
        body: JSON.stringify({ contacts: imported }),
      });
      form.reset();
    }, "联系人批量导入完成");
  }

  async function deleteContact(contact: PlatformContact) {
    if (!window.confirm(`确认删除联系人“${contact.name || contact.external_id}”及其可擦除数据？`)) return;
    await perform(
      async () => {
        await request(`/api/platform/projects/${projectId}/telephony/contacts/${contact.id}`, { method: "DELETE" });
        setSelectedContactIds((items) => items.filter((id) => id !== contact.id));
      },
      "联系人已删除",
    );
  }

  async function createCampaign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const selected = selectedContactIds;
    if (selected.length > 5000) {
      setError("单个活动最多一次关联 5000 位联系人，请分批创建活动");
      return;
    }
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
          priority: Number(data.priority || 100),
          scheduled_at: data.scheduled_at ? new Date(String(data.scheduled_at)).toISOString() : null,
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
      setSelectedContactIds([]);
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

  async function saveDnc(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/do-not-call`, {
        method: "POST",
        body: JSON.stringify({
          phone_number: data.phone_number,
          reason: data.reason,
          source: data.source,
          expires_at: data.expires_at ? new Date(String(data.expires_at)).toISOString() : null,
        }),
      });
      form.reset();
    }, "DNC 记录已保存");
  }

  async function deleteDnc(entry: DncEntry) {
    if (!window.confirm(`确认移除尾号 ${entry.phone_last4} 的 DNC 限制？`)) return;
    await perform(
      () => request(`/api/platform/projects/${projectId}/telephony/do-not-call/${entry.id}`, { method: "DELETE" }).then(() => undefined),
      "DNC 记录已移除",
    );
  }

  async function saveConsent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/consents`, {
        method: "POST",
        body: JSON.stringify({
          phone_number: data.phone_number,
          purpose: data.purpose,
          status: data.status,
          evidence_ref: data.evidence_ref || "",
          valid_from: data.valid_from ? new Date(String(data.valid_from)).toISOString() : null,
          valid_until: data.valid_until ? new Date(String(data.valid_until)).toISOString() : null,
        }),
      });
      form.reset();
    }, "客户同意记录已保存");
  }

  async function saveDestination(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const name = String(data.name);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/telephony/transfer-destinations/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: JSON.stringify({ name, target_uri: data.target_uri, mode: "cold", status: data.status }),
      });
      form.reset();
    }, "人工转接目标已保存");
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
          status: data.status || "active",
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

  async function openCall(callId: string) {
    setSelectedCallId(callId);
    setBusy(true);
    setError("");
    const prefix = `/api/platform/projects/${projectId}/telephony/calls/${callId}`;
    try {
      setRecordingAccess(null);
      const [detail, cdrData, transfersData] = await Promise.all([
        request<PlatformCall>(prefix),
        optional<JsonObject | null>(`${prefix}/cdr`, null),
        optional<{ items: JsonObject[] }>(`${prefix}/transfers`, { items: [] }),
      ]);
      setCallDetail(detail);
      setCdr(cdrData);
      setCallTransfers(transfersData.items);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function openRecording() {
    if (!callDetail) return;
    setBusy(true);
    setError("");
    try {
      const access = await request<{ url: string; temporary: boolean; expires_at?: string | null }>(
        `/api/platform/projects/${projectId}/telephony/calls/${callDetail.id}/recording-access?ttl_seconds=300`,
      );
      setRecordingAccess(access);
      announce(access.temporary ? "已生成 5 分钟录音访问链接" : "录音访问链接已就绪");
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function queueSessionCommand(sessionId: string, commandType: "rpc" | "dtmf", payload: JsonObject, success: string) {
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/sessions/${sessionId}/console/commands`, {
        method: "POST",
        body: JSON.stringify({ command_type: commandType, payload }),
      });
      await openSession(sessionId);
      if (selectedCallId === sessionId) await openCall(sessionId);
    }, success, false);
  }

  async function transferActiveCall(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!callDetail) return;
    const data = formData(event);
    await queueSessionCommand(
      callDetail.id,
      "rpc",
      { method: "call.transfer", arguments: { destination_name: data.destination_name, reason: data.reason || "operator requested transfer" } },
      "人工转接命令已安全下发",
    );
  }

  async function sendCallDtmf(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!callDetail) return;
    const data = formData(event);
    await queueSessionCommand(callDetail.id, "dtmf", { digits: data.digits }, "DTMF 已下发");
  }

  async function hangupActiveCall() {
    if (!callDetail || !window.confirm("确认由运行时 Agent 挂断当前通话？")) return;
    await queueSessionCommand(callDetail.id, "rpc", { method: "call.hangup", arguments: {} }, "挂断命令已下发");
  }

  async function loadAnalytics(event?: FormEvent<HTMLFormElement>, append = false) {
    event?.preventDefault();
    const data = event ? formData(event) : {};
    const params = new URLSearchParams();
    if (data.start) params.set("start", new Date(String(data.start)).toISOString());
    if (data.end) params.set("end", new Date(String(data.end)).toISOString());
    params.set("limit", "100");
    if (append && analyticsCursor) params.set("cursor", analyticsCursor);
    const prefix = `/api/platform/projects/${projectId}/analytics`;
    await perform(async () => {
      const [summary, sessionPage] = await Promise.all([
        request<AnalyticsSummary>(`${prefix}/summary?${params}`),
        request<{ items: Session[]; next_cursor: string | null }>(`${prefix}/sessions?${params}`),
      ]);
      setAnalytics(summary);
      setAnalyticsSessions((current) => append ? [...current, ...sessionPage.items] : sessionPage.items);
      setAnalyticsCursor(sessionPage.next_cursor);
    }, "分析数据已更新", false);
  }

  async function exportAnalytics(form: HTMLFormElement) {
    const data = Object.fromEntries(new FormData(form));
    const params = new URLSearchParams();
    if (data.start) params.set("start", new Date(String(data.start)).toISOString());
    if (data.end) params.set("end", new Date(String(data.end)).toISOString());
    setBusy(true);
    setError("");
    try {
      await downloadAuthenticated(
        `/api/platform/projects/${projectId}/analytics/export.csv?${params}`,
        `analytics-${projectId}.csv`,
      );
      announce("分析报表已导出");
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function openSession(sessionId: string) {
    setSelectedSessionId(sessionId);
    setBusy(true);
    setError("");
    const prefix = `/api/platform/projects/${projectId}/sessions/${sessionId}`;
    try {
      const [nextTimeline, commands] = await Promise.all([
        request<Timeline>(prefix),
        optional<{ items: ConsoleCommand[] }>(`${prefix}/console/commands`, { items: [] }),
      ]);
      setTimeline(nextTimeline);
      setConsoleCommands(commands.items);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function sendConsoleCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedSessionId) return;
    const form = event.currentTarget;
    const data = formData(event);
    const kind = String(data.kind);
    const payload = kind === "dtmf"
      ? { digits: data.value }
      : { method: kind, arguments: kind === "agent.say" ? { text: data.value } : {} };
    await queueSessionCommand(selectedSessionId, kind === "dtmf" ? "dtmf" : "rpc", payload, "控制命令已进入运行时队列");
    form.reset();
  }

  async function closeSession(status: "completed" | "failed" | "cancelled") {
    if (!selectedSessionId || !window.confirm(`确认将会话标记为 ${status}？`)) return;
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/sessions/${selectedSessionId}/close`, {
        method: "POST",
        body: JSON.stringify({ status }),
      });
      await openSession(selectedSessionId);
    }, "会话状态已关闭", false);
  }

  async function saveAgentSpec(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const spec = parseJson(data.spec);
    await perform(async () => {
      const path = editingSpec
        ? `/api/platform/projects/${projectId}/agent-specs/${editingSpec.id}`
        : `/api/platform/projects/${projectId}/agent-specs`;
      await request(path, {
        method: editingSpec ? "PUT" : "POST",
        body: JSON.stringify({ spec, expected_revision: editingSpec?.revision || null }),
      });
      setEditingSpec(null);
      form.reset();
    }, editingSpec ? "AgentSpec 新修订已保存" : "AgentSpec 已创建");
  }

  async function publishSpec(spec: AgentSpecRecord) {
    await perform(
      () => request(`/api/platform/projects/${projectId}/agent-specs/${spec.id}/publish`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: spec.revision }),
      }).then(() => undefined),
      "AgentSpec 已发布",
    );
  }

  async function exportSpec(spec: AgentSpecRecord) {
    setBusy(true);
    setError("");
    try {
      await downloadAuthenticated(
        `/api/platform/projects/${projectId}/agent-specs/${spec.id}/export`,
        `agent-${spec.name}.zip`,
      );
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function createAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/agents`, {
        method: "POST",
        body: JSON.stringify({ name: data.name, description: data.description || "" }),
      });
      form.reset();
    }, "Agent 已创建");
  }

  async function loadVersions(agentId: string) {
    if (!agentId) {
      setVersions([]);
      return;
    }
    const result = await request<{ items: AgentVersion[] }>(`/api/platform/projects/${projectId}/agents/${agentId}/versions`);
    setVersions(result.items);
  }

  async function buildAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    const agentId = String(data.agent_id);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/agents/${agentId}/builds`, {
        method: "POST",
        body: JSON.stringify({ source_ref: data.source_ref, image_ref: data.image_ref, spec: parseJson(data.spec) }),
      });
      await loadVersions(agentId);
      form.reset();
    }, "Agent 构建已完成");
  }

  async function createDeployment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/deployments`, {
        method: "POST",
        body: JSON.stringify({
          agent_id: data.agent_id,
          version_id: data.version_id,
          name: data.name,
          desired_replicas: Number(data.desired_replicas || 1),
        }),
      });
      form.reset();
    }, "部署已创建");
  }

  async function rolloutDeployment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = formData(event);
    await perform(
      () => request(`/api/platform/projects/${projectId}/deployments/${data.deployment_id}/rollout`, {
        method: "POST",
        body: JSON.stringify({ version_id: data.version_id }),
      }).then(() => undefined),
      "新版本已滚动发布",
    );
  }

  async function rollbackDeployment(deployment: Deployment) {
    if (!window.confirm(`确认回滚部署“${deployment.name}”？`)) return;
    await perform(
      () => request(`/api/platform/projects/${projectId}/deployments/${deployment.id}/rollback`, { method: "POST" }).then(() => undefined),
      "部署已回滚",
    );
  }

  async function viewRuntime(deployment: Deployment) {
    setBusy(true);
    setError("");
    try {
      const prefix = `/api/platform/projects/${projectId}/deployments/${deployment.id}`;
      await request(`${prefix}/logs/collect?tail=500`, { method: "POST" });
      const [instances, logs] = await Promise.all([
        request<{ items: JsonObject[] }>(`${prefix}/instances`),
        request<{ items: JsonObject[] }>(`${prefix}/logs?limit=500`),
      ]);
      setRuntimeView({ deploymentId: deployment.id, instances: instances.items, logs: logs.items });
      announce(`实例 ${instances.items.length} 个；已采集日志 ${logs.items.length} 条`);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }

  async function saveSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/secrets/${encodeURIComponent(String(data.name))}`, {
        method: "PUT",
        body: JSON.stringify({ value: data.value }),
      });
      form.reset();
    }, "Secret 已加密保存");
  }

  async function saveInferenceRoute(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/inference/routes`, {
        method: "PUT",
        body: JSON.stringify({
          descriptor: data.descriptor,
          modality: data.modality,
          provider: data.provider,
          provider_model: data.provider_model,
          priority: Number(data.priority || 100),
          timeout_seconds: Number(data.timeout_seconds || 30),
          enabled: data.enabled === "on",
          config: parseJson(data.config),
        }),
      });
      form.reset();
    }, "推理路由已保存");
  }

  async function invokeInference(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = formData(event);
    await perform(async () => {
      const result = await request<JsonObject>(`/api/platform/projects/${projectId}/inference`, {
        method: "POST",
        body: JSON.stringify({
          descriptor: data.descriptor,
          modality: data.modality,
          input: parseJson(data.input),
          parameters: parseJson(data.parameters),
          session_id: data.session_id || null,
        }),
      });
      setInferenceResult(result);
    }, "推理测试已完成", false);
  }

  async function saveEmbedConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = formData(event);
    await perform(async () => {
      await request(`/api/platform/projects/${projectId}/embed-configs`, {
        method: "POST",
        body: JSON.stringify({
          name: data.name,
          agent_name: data.agent_name,
          room_prefix: data.room_prefix || "embed",
          allowed_origins: String(data.allowed_origins).split(/[\s,，]+/).filter(Boolean),
          capabilities: { audio: data.audio === "on", text: data.text === "on", video: data.video === "on" },
          enabled: data.enabled === "on",
        }),
      });
      form.reset();
    }, "嵌入组件配置已创建");
  }

  async function toggleEmbedConfig(config: EmbedConfig) {
    await perform(
      () => request(`/api/platform/projects/${projectId}/embed-configs/${config.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: config.name,
          agent_name: config.agent_name,
          room_prefix: config.room_prefix,
          allowed_origins: config.allowed_origins,
          capabilities: config.capabilities,
          enabled: !config.enabled,
        }),
      }).then(() => undefined),
      config.enabled ? "嵌入配置已停用" : "嵌入配置已启用",
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

  async function deleteMember(member: Member) {
    if (!window.confirm(`确认移除成员“${member.user_id}”？`)) return;
    await perform(
      () => request(`/api/platform/projects/${projectId}/members/${encodeURIComponent(member.user_id)}`, { method: "DELETE" }).then(() => undefined),
      "成员已移除",
    );
  }

  async function purgeRetention() {
    if (!window.confirm("确认立即清理超过项目保留期限的数据？此操作不可撤销。")) return;
    await perform(
      () => request(`/api/platform/projects/${projectId}/maintenance/retention/purge`, { method: "POST" }).then(() => undefined),
      "数据保留清理已执行",
    );
  }

  async function logout() {
    if (auth?.mode === "bearer") {
      try {
        await request("/api/platform/auth/revoke", {
          method: "POST",
          body: JSON.stringify({ reason: "user_logout" }),
        });
      } catch {
        // Local logout still clears the browser session if the control plane is unavailable.
      }
    }
    savePlatformAuth(null);
    onAuthChange(null);
    setProjects([]);
    setProjectId("");
  }

  const visibleContacts = contacts;
  const selectedContactSet = new Set(selectedContactIds);
  const completionRate = metrics.attempts.total ? Math.round((metrics.attempts.completed / metrics.attempts.total) * 100) : 0;

  if (!auth) {
    const oidcReady = Boolean(oidcConfiguration());
    return (
      <section className="commercial-auth" aria-labelledby="commercial-auth-title">
        <div className="commercial-auth-copy">
          <span className="commercial-eyebrow">COMMERCIAL VOICE CLOUD</span>
          <h1 id="commercial-auth-title">企业级语音运营平台</h1>
          <p>统一管理外呼活动、呼入客服、SIP 线路、录音合规、Agent 部署、运行分析与租户权限。</p>
          <ul>
            <li>OIDC Authorization Code + PKCE 登录与令牌续期</li>
            <li>项目级 RBAC、审计日志与数据保留</li>
            <li>可靠呼叫队列、人工控制、录音终态与实时指标</li>
          </ul>
        </div>
        <div className="commercial-auth-card">
          <h2>登录控制台</h2>
          <p>{oidcReady ? "使用企业身份提供商继续。" : "请先在构建环境配置企业 IdP。"}</p>
          <button className="commercial-primary" disabled={!oidcReady || busy} onClick={() => beginOidcLogin().catch((value) => setError(errorMessage(value)))} type="button">
            {busy ? "正在验证…" : "企业 IdP 登录"}
          </button>
          {import.meta.env.DEV || import.meta.env.VITE_ALLOW_DEVELOPMENT_AUTH === "true" ? (
            <form className="commercial-dev-auth" onSubmit={(event) => {
              event.preventDefault();
              const userId = String(formData(event).user_id || "").trim();
              if (!userId) return;
              const session: PlatformAuthSession = { mode: "development", userId };
              savePlatformAuth(session);
              onAuthChange(session);
            }}>
              <label>本地开发身份<input name="user_id" defaultValue="owner" required /></label>
              <button type="submit">开发模式进入</button>
            </form>
          ) : null}
          {error ? <div className="commercial-error" role="alert">{error}</div> : null}
        </div>
      </section>
    );
  }

  return (
    <section className="commercial-platform" aria-busy={loading || busy}>
      <header className="commercial-heading">
        <div>
          <span className="commercial-eyebrow">VOICE OPERATIONS</span>
          <h1>商用语音平台</h1>
          <p>外呼增长与呼入客服共用一套高并发、可审计控制平面。</p>
        </div>
        <div className="commercial-session">
          <span>{platformAuthSubject(auth)}</span>
          <b>{role}</b>
          <button onClick={logout} type="button">安全退出</button>
        </div>
      </header>

      {error ? <div className="commercial-error" role="alert"><span>{error}</span><button onClick={() => setError("")} type="button">关闭</button></div> : null}
      {notice ? <div className="commercial-notice" role="status">{notice}</div> : null}

      <div className="commercial-projectbar">
        <label>当前项目
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">请选择项目</option>
            {projects.map((project) => <option value={project.id} key={project.id}>{project.name} · {project.role}</option>)}
          </select>
        </label>
        <span>{selectedProject ? `${selectedProject.slug} · 保留 ${selectedProject.retention_days} 天` : "尚未创建项目"}</span>
        <label className="commercial-check"><input checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} type="checkbox" /> 5 秒自动刷新</label>
        <small>最近刷新 {lastRefreshedAt || "—"}</small>
        <button onClick={() => refreshRealtime()} type="button">立即刷新</button>
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
            {(Object.keys(tabLabels) as Tab[]).map((item) => (
              <button aria-current={tab === item ? "page" : undefined} className={tab === item ? "active" : ""} onClick={() => setTab(item)} key={item} type="button">
                {tabLabels[item]}
              </button>
            ))}
          </nav>

          {tab === "overview" ? (
            <div className="commercial-stack">
              <div className="commercial-kpis">
                <article><span>排队任务</span><strong>{metrics.queue_depth}</strong><small>可靠外呼队列</small></article>
                <article><span>当前并发</span><strong>{metrics.active_calls}</strong><small>呼入 + 外呼</small></article>
                <article><span>完成率</span><strong>{completionRate}%</strong><small>{metrics.attempts.completed}/{metrics.attempts.total} 次尝试</small></article>
                <article className={metrics.stale_leases ? "attention" : ""}><span>异常租约</span><strong>{metrics.stale_leases}</strong><small>需运行时回收</small></article>
              </div>
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={createOutbound}>
                  <div className="commercial-panel-title"><div><span>QUICK ACTION</span><h2>发起单次外呼</h2></div><b>OUT</b></div>
                  <label>被叫号码<input name="destination_number" type="tel" required placeholder="+8613800000000" /></label>
                  <label>主叫号码<input name="source_number" type="tel" placeholder="由线路默认值决定" /></label>
                  <label>外呼线路<select name="trunk_id" required><option value="">请选择</option>{trunks.filter((item) => item.direction !== "inbound" && item.status === "active").map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
                  <label>Agent 名称<input name="agent_name" defaultValue="commercial-agent" required /></label>
                  <div className="commercial-inline"><label>优先级<input name="priority" type="number" defaultValue={100} min={0} max={1000} /></label><label>最大尝试<input name="max_attempts" type="number" defaultValue={3} min={1} max={10} /></label></div>
                  <button className="commercial-primary" disabled={busy || !canOperate || !trunks.length} title={!canOperate ? "当前角色只有读取权限" : undefined} type="submit">加入外呼队列</button>
                </form>
                <section className="commercial-panel">
                  <div className="commercial-panel-title"><div><span>CAPACITY</span><h2>生产容量护栏</h2></div><b>{metrics.active_calls}/{limits?.max_concurrent_calls ?? "—"}</b></div>
                  <dl className="commercial-capacity">
                    <div><dt>总并发上限</dt><dd>{limits?.max_concurrent_calls ?? "—"}</dd></div>
                    <div><dt>外呼并发</dt><dd>{limits?.max_outbound_calls ?? "—"}</dd></div>
                    <div><dt>呼入并发</dt><dd>{limits?.max_inbound_calls ?? "—"}</dd></div>
                    <div><dt>每分钟呼叫</dt><dd>{limits?.max_calls_per_minute ?? "—"}</dd></div>
                  </dl>
                  <p className="commercial-muted">呼入超过容量时按当前策略{policy?.inbound_overflow_mode === "transfer" ? "转接到备用坐席" : "拒绝并记录原因"}。</p>
                  <div className="commercial-state-grid">{Object.entries(metrics.states).map(([state, count]) => <span key={state}><b>{count}</b>{state}</span>)}</div>
                </section>
              </div>
            </div>
          ) : null}

          {tab === "calls" ? (
            <div className="commercial-stack">
              <section className="commercial-panel">
                <div className="commercial-panel-title"><div><span>CALL LEDGER</span><h2>最近呼叫</h2></div><b>{calls.length}</b></div>
                <div className="commercial-table-wrap"><table><thead><tr><th>方向</th><th>号码</th><th>状态</th><th>Agent / Room</th><th>录音</th><th>时间</th><th>操作</th></tr></thead><tbody>
                  {calls.map((call) => <tr className={selectedCallId === call.id ? "selected" : ""} key={call.id}><td><span className={`direction ${call.direction}`}>{call.direction === "inbound" ? "呼入" : "外呼"}</span></td><td>{call.direction === "inbound" ? call.source_number : call.destination_number}</td><td><span className={`commercial-status ${call.status}`}>{call.status}</span>{call.failure_code ? <small>{call.failure_code}</small> : null}</td><td>{call.agent_name}<small>{call.room_name || "等待分配房间"}</small></td><td>{call.recording_status || "关闭"}</td><td>{displayTime(call.created_at)}</td><td><button onClick={() => openCall(call.id)} type="button">详情</button></td></tr>)}
                  {!calls.length ? <tr><td colSpan={7} className="commercial-empty-row">暂无呼叫记录</td></tr> : null}
                </tbody></table></div>
              </section>
              {callDetail ? (
                <section className="commercial-panel commercial-detail" aria-labelledby="call-detail-title">
                  <div className="commercial-panel-title"><div><span>CALL DETAIL</span><h2 id="call-detail-title">通话详情</h2></div><button onClick={() => { setSelectedCallId(""); setCallDetail(null); }} type="button">关闭</button></div>
                  <dl className="commercial-detail-grid">
                    <div><dt>Call ID</dt><dd>{callDetail.id}</dd></div><div><dt>Provider ID</dt><dd>{callDetail.provider_call_id || "—"}</dd></div>
                    <div><dt>状态</dt><dd>{callDetail.status}</dd></div><div><dt>尝试</dt><dd>{callDetail.attempt_count ?? 0}/{callDetail.max_attempts ?? "—"}</dd></div>
                    <div><dt>接通</dt><dd>{displayTime(callDetail.answered_at)}</dd></div><div><dt>结束</dt><dd>{displayTime(callDetail.ended_at)}</dd></div>
                    <div><dt>失败原因</dt><dd>{callDetail.failure_code || "—"} {callDetail.failure_detail || ""}</dd></div><div><dt>录音 Egress</dt><dd>{callDetail.recording_egress_id || "—"}</dd></div>
                  </dl>
                  {callDetail.recording_storage_uri ? (
                    <div className="commercial-recording">
                      <strong>录音</strong>
                      {recordingAccess ? <audio controls preload="none" src={recordingAccess.url} /> : <code>{callDetail.recording_storage_uri}</code>}
                      {recordingAccess ? <a href={recordingAccess.url} rel="noreferrer" target="_blank">打开/下载</a> : <button disabled={callDetail.recording_status !== "completed" || !canOperate} onClick={openRecording} type="button">获取安全访问链接</button>}
                      <small>{recordingAccess?.temporary ? `临时链接将于 ${displayTime(recordingAccess.expires_at || undefined)} 失效。` : "长期存储凭据不会发送到浏览器。"}</small>
                    </div>
                  ) : <p className="commercial-muted">当前没有可用录音对象。</p>}
                  <div className="commercial-two-column">
                    <div><h3>CDR</h3><pre className="commercial-code">{cdr ? pretty(cdr) : "尚未生成 CDR"}</pre></div>
                    <div><h3>转接记录</h3><pre className="commercial-code">{callTransfers.length ? pretty(callTransfers) : "暂无转接"}</pre></div>
                  </div>
                  {callDetail.status === "active" && canConsole ? (
                    <div className="commercial-command-row">
                      <form className="commercial-form" onSubmit={transferActiveCall}><label>转接目标<select name="destination_name" required><option value="">请选择</option>{destinations.filter((item) => item.status === "active").map((item) => <option value={item.name} key={item.id}>{item.name}</option>)}</select></label><label>转接原因<input name="reason" maxLength={2000} /></label><button disabled={!destinations.length} type="submit">人工转接</button></form>
                      <form className="commercial-form" onSubmit={sendCallDtmf}><label>DTMF<input name="digits" required pattern="[0-9*#A-D]+" /></label><button type="submit">发送按键</button></form>
                      <button className="danger" onClick={hangupActiveCall} type="button">挂断通话</button>
                    </div>
                  ) : null}
                </section>
              ) : null}
            </div>
          ) : null}

          {tab === "campaigns" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveContact}><div className="commercial-panel-title"><div><span>CRM</span><h2>新增或更新联系人</h2></div></div><label>外部 ID<input name="external_id" required /></label><label>姓名<input name="name" /></label><label>号码<input name="phone_number" type="tel" required placeholder="+8613800000000" /></label><label>状态<select name="status"><option value="active">可联系</option><option value="suppressed">禁止联系</option></select></label><label>元数据 JSON<textarea name="metadata" defaultValue="{}" rows={3} /></label><button className="commercial-primary" disabled={busy || !canOperate} type="submit">保存联系人</button></form>
                <form className="commercial-panel commercial-form" onSubmit={importContacts}><div className="commercial-panel-title"><div><span>BULK IMPORT</span><h2>批量导入联系人</h2></div></div><label>CSV / JSON 文件<input name="file" type="file" accept=".csv,.json,text/csv,application/json" required /></label><small>CSV 表头至少包含 external_id,phone_number；单次最多 1000 条。</small><button className="commercial-primary" disabled={busy || !canOperate} type="submit">校验并导入</button></form>
              </div>
              <section className="commercial-panel">
                <div className="commercial-panel-title"><div><span>CONTACTS</span><h2>联系人</h2></div><b>{contacts.length}</b></div>
                <div className="commercial-toolbar"><input aria-label="搜索联系人" onChange={(event) => setContactSearch(event.target.value)} placeholder="按姓名或外部 ID 搜索" value={contactSearch} /><span>第 {contactPage} 页</span><button disabled={!contactCursorHistory.length} onClick={() => { const history = [...contactCursorHistory]; const previous = history.pop() || ""; setContactCursorHistory(history); setContactCursor(previous); setContactPage((page) => Math.max(1, page - 1)); loadContactPage(contactSearch, previous); }} type="button">上一页</button><button disabled={!contactNextCursor} onClick={() => { if (!contactNextCursor) return; setContactCursorHistory((items) => [...items, contactCursor]); setContactCursor(contactNextCursor); setContactPage((page) => page + 1); loadContactPage(contactSearch, contactNextCursor); }} type="button">下一页</button></div>
                <div className="commercial-table-wrap"><table><thead><tr><th><input aria-label="选择当前页全部可联系客户" checked={visibleContacts.filter((item) => item.status === "active").length > 0 && visibleContacts.filter((item) => item.status === "active").every((item) => selectedContactSet.has(item.id))} onChange={(event) => { const pageIds = visibleContacts.filter((item) => item.status === "active").map((item) => item.id); setSelectedContactIds((current) => event.target.checked ? Array.from(new Set([...current, ...pageIds])) : current.filter((id) => !pageIds.includes(id))); }} type="checkbox" /></th><th>外部 ID</th><th>姓名</th><th>号码</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>{visibleContacts.map((contact) => <tr key={contact.id}><td><input aria-label={`选择联系人 ${contact.name || contact.external_id}`} checked={selectedContactSet.has(contact.id)} disabled={contact.status !== "active"} onChange={(event) => setSelectedContactIds((current) => event.target.checked ? Array.from(new Set([...current, contact.id])) : current.filter((id) => id !== contact.id))} type="checkbox" /></td><td>{contact.external_id}</td><td>{contact.name || "—"}</td><td>{contact.phone_number}</td><td>{contact.status}</td><td>{displayTime(contact.updated_at)}</td><td><button className="danger" disabled={!canManage} onClick={() => deleteContact(contact)} type="button">删除</button></td></tr>)}{!visibleContacts.length ? <tr><td colSpan={7} className="commercial-empty-row">没有匹配的联系人</td></tr> : null}</tbody></table></div>
                <div className="commercial-selection"><strong>已跨页选择 {selectedContactIds.length} 位联系人</strong><button disabled={!selectedContactIds.length} onClick={() => setSelectedContactIds([])} type="button">清空选择</button></div>
              </section>
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={createCampaign}><div className="commercial-panel-title"><div><span>CAMPAIGN</span><h2>创建外呼活动</h2></div></div><label>活动名称<input name="name" required /></label><label>Agent 名称<input name="agent_name" defaultValue="commercial-agent" required /></label><div className="commercial-inline"><label>线路<select name="trunk_id" required><option value="">请选择</option>{trunks.filter((item) => item.direction !== "inbound").map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>主叫号码<input name="source_number" /></label></div><div className="commercial-inline"><label>活动并发<input name="max_concurrent_calls" type="number" defaultValue={10} min={1} /></label><label>最大尝试<input name="max_attempts" type="number" defaultValue={3} min={1} max={10} /></label></div><div className="commercial-inline"><label>优先级<input name="priority" type="number" defaultValue={100} min={0} max={1000} /></label><label>计划时间<input name="scheduled_at" type="datetime-local" /></label></div><div className="commercial-selection"><strong>将加入 {selectedContactIds.length} 位已选联系人</strong><small>可在上方搜索并跨页勾选，选择会一直保留；单次最多 5000 位。</small></div><label className="commercial-check"><input name="start_now" type="checkbox" /> 创建后立即启动</label><button className="commercial-primary" disabled={busy || !canOperate || !selectedContactIds.length || selectedContactIds.length > 5000 || !trunks.length} type="submit">创建活动</button></form>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>CAMPAIGN QUEUE</span><h2>活动运行状态</h2></div><b>{campaigns.length}</b></div><div className="commercial-campaign-grid">{campaigns.map((campaign) => <article key={campaign.id}><header><div><h3>{campaign.name}</h3><span className={`commercial-status ${campaign.status}`}>{campaign.status}</span></div><strong>{campaign.terminal_count || 0}/{campaign.contact_count || 0}</strong></header><div className="campaign-track"><i style={{ width: `${campaign.contact_count ? Math.min(100, ((campaign.terminal_count || 0) / campaign.contact_count) * 100) : 0}%` }} /></div><p>排队 {campaign.queued_count || 0} · 拦截 {campaign.blocked_count || 0} · 并发 {campaign.max_concurrent_calls}</p><footer>{campaign.status === "running" ? <button disabled={!canOperate} onClick={() => changeCampaign(campaign.id, "paused")} type="button">暂停</button> : campaign.status !== "completed" && campaign.status !== "canceled" ? <button disabled={!canOperate} onClick={() => changeCampaign(campaign.id, "running")} type="button">启动</button> : null}{!(["completed", "canceled"] as string[]).includes(campaign.status) ? <button className="danger" disabled={!canOperate} onClick={() => changeCampaign(campaign.id, "canceled")} type="button">取消</button> : null}</footer></article>)}</div></section>
              </div>
            </div>
          ) : null}

          {tab === "compliance" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveDnc}><div className="commercial-panel-title"><div><span>DO NOT CALL</span><h2>禁止呼叫名单</h2></div></div><label>号码<input name="phone_number" type="tel" required /></label><label>原因<input name="reason" required maxLength={500} /></label><label>来源<input name="source" defaultValue="operator" required /></label><label>到期时间<input name="expires_at" type="datetime-local" /></label><button className="commercial-primary" disabled={!canManage} type="submit">保存 DNC</button></form>
                <form className="commercial-panel commercial-form" onSubmit={saveConsent}><div className="commercial-panel-title"><div><span>CONSENT</span><h2>客户同意证据</h2></div></div><label>号码<input name="phone_number" type="tel" required /></label><div className="commercial-inline"><label>用途<input name="purpose" defaultValue={policy?.consent_purpose || "outbound"} required /></label><label>状态<select name="status"><option value="granted">已同意</option><option value="revoked">已撤销</option><option value="expired">已过期</option></select></label></div><label>证据引用<input name="evidence_ref" placeholder="crm://consents/42" /></label><div className="commercial-inline"><label>生效时间<input name="valid_from" type="datetime-local" /></label><label>到期时间<input name="valid_until" type="datetime-local" /></label></div><button className="commercial-primary" disabled={!canManage} type="submit">记录同意状态</button></form>
              </div>
              <div className="commercial-two-column">
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>DNC LEDGER</span><h2>DNC 记录</h2></div><b>{dnc.length}</b></div><div className="commercial-list">{dnc.map((entry) => <article key={entry.id}><div><strong>尾号 {entry.phone_last4}</strong><span>{entry.reason}</span><small>{entry.source} · {entry.expires_at ? `至 ${displayTime(entry.expires_at)}` : "永久"}</small></div><button className="danger" disabled={!canManage} onClick={() => deleteDnc(entry)} type="button">移除</button></article>)}</div></section>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>CONSENT LEDGER</span><h2>同意记录</h2></div><b>{consents.length}</b></div><div className="commercial-list">{consents.map((entry) => <article key={entry.id}><div><strong>尾号 {entry.phone_last4} · {entry.status}</strong><span>{entry.purpose}</span><small>{entry.evidence_ref || "无证据引用"} · {displayTime(entry.valid_from)}</small></div></article>)}</div></section>
              </div>
            </div>
          ) : null}

          {tab === "configuration" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveTrunk}><div className="commercial-panel-title"><div><span>SIP CONNECTIVITY</span><h2>配置 LiveKit 线路</h2></div></div><label>线路名称<input name="name" required /></label><div className="commercial-inline"><label>方向<select name="direction"><option value="bidirectional">双向</option><option value="outbound">仅外呼</option><option value="inbound">仅呼入</option></select></label><label>状态<select name="status"><option value="active">正常</option><option value="degraded">降级</option><option value="disabled">停用</option></select></label></div><label>供应商<input name="provider" required placeholder="carrier" pattern="[a-z0-9_.-]+" /></label><label>LiveKit Trunk ID<input name="livekit_trunk_id" required placeholder="ST_..." /></label><label>Secret 名称<input name="secret_name" placeholder="k8s-secret-name" /></label><label>允许的主叫/接入号码<input name="numbers" placeholder="+8610..., +8621..." /></label><div className="commercial-inline"><label>线路并发<input name="max_concurrent_calls" type="number" defaultValue={100} min={1} /></label><label>每秒呼叫<input name="max_calls_per_second" type="number" defaultValue={5} min={1} /></label></div><button className="commercial-primary" disabled={busy || !canManage} type="submit">保存线路</button></form>
                {limits ? <form className="commercial-panel commercial-form" onSubmit={saveLimits}><div className="commercial-panel-title"><div><span>CAPACITY GUARDRAILS</span><h2>并发与速率</h2></div></div><label>总并发<input name="max_concurrent_calls" type="number" defaultValue={limits.max_concurrent_calls} min={1} /></label><div className="commercial-inline"><label>外呼并发<input name="max_outbound_calls" type="number" defaultValue={limits.max_outbound_calls} min={1} /></label><label>呼入并发<input name="max_inbound_calls" type="number" defaultValue={limits.max_inbound_calls} min={1} /></label></div><label>每分钟呼叫<input name="max_calls_per_minute" type="number" defaultValue={limits.max_calls_per_minute} min={1} /></label><label>租约秒数<input name="lease_seconds" type="number" defaultValue={limits.lease_seconds} min={10} max={300} /></label><button className="commercial-primary" disabled={busy || !canManage} type="submit">更新容量</button></form> : null}
              </div>
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveDestination}><div className="commercial-panel-title"><div><span>HUMAN HANDOFF</span><h2>人工转接目标</h2></div></div><label>目标名称<input name="name" required /></label><label>目标 URI<input name="target_uri" required placeholder="tel:+8610000000001 或 sip:user@host" /></label><label>状态<select name="status"><option value="active">启用</option><option value="disabled">停用</option></select></label><button className="commercial-primary" disabled={!canManage} type="submit">保存转接目标</button><div className="commercial-list compact">{destinations.map((item) => <article key={item.id}><div><strong>{item.name}</strong><span>{item.target_uri}</span></div><small>{item.status}</small></article>)}</div></form>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>ACTIVE TRUNKS</span><h2>线路清单</h2></div><b>{trunks.length}</b></div><div className="commercial-trunks">{trunks.map((trunk) => <article key={trunk.id}><div><h3>{trunk.name}</h3><p>{trunk.provider} · {trunk.direction} · {trunk.livekit_trunk_id}</p><small>{trunk.numbers.join(", ") || "未限制号码"}</small></div><strong>{trunk.max_concurrent_calls} 并发 / {trunk.max_calls_per_second} CPS</strong></article>)}</div></section>
              </div>
              {policy ? <form className="commercial-panel commercial-form policy-form" onSubmit={savePolicy}><div className="commercial-panel-title"><div><span>COMPLIANCE POLICY</span><h2>呼叫合规与录音</h2></div></div><div className="commercial-form-grid"><label>时区<input name="timezone" defaultValue={policy.timezone} /></label><label>工作日（0=周一）<input name="allowed_weekdays" defaultValue={policy.allowed_weekdays.join(",")} /></label><label>开始时间<input name="calling_window_start" type="time" defaultValue={policy.calling_window_start} /></label><label>结束时间<input name="calling_window_end" type="time" defaultValue={policy.calling_window_end} /></label><label>同意用途<input name="consent_purpose" defaultValue={policy.consent_purpose} /></label><label>每日同号码尝试<input name="max_attempts_per_number_per_day" type="number" defaultValue={policy.max_attempts_per_number_per_day} min={1} max={100} /></label><label>呼入溢出<select name="inbound_overflow_mode" defaultValue={policy.inbound_overflow_mode}><option value="reject">拒绝</option><option value="transfer">转接</option></select></label><label>溢出目标<select name="inbound_overflow_destination_name" defaultValue={policy.inbound_overflow_destination_name}><option value="">请选择</option>{destinations.filter((item) => item.status === "active").map((item) => <option value={item.name} key={item.id}>{item.name}</option>)}</select></label><label>录音模式<select name="recording_mode" defaultValue={policy.recording_mode}><option value="off">关闭</option><option value="always">始终录音</option></select></label><label>录音告知语<input name="recording_disclosure_text" defaultValue={policy.recording_disclosure_text} /></label></div><div className="commercial-check-row"><label className="commercial-check"><input name="outbound_enabled" type="checkbox" defaultChecked={policy.outbound_enabled} /> 允许外呼</label><label className="commercial-check"><input name="require_consent" type="checkbox" defaultChecked={policy.require_consent} /> 强制校验用户同意</label></div><button className="commercial-primary" disabled={busy || !canManage} type="submit">保存合规策略</button></form> : null}
            </div>
          ) : null}

          {tab === "analytics" ? (
            <div className="commercial-stack">
              <form className="commercial-panel commercial-toolbar commercial-range" onSubmit={(event) => loadAnalytics(event)}><label>开始<input name="start" type="datetime-local" /></label><label>结束<input name="end" type="datetime-local" /></label><button className="commercial-primary" type="submit">查询</button><button disabled={!canManage} onClick={(event) => { const form = event.currentTarget.form; if (form) exportAnalytics(form); }} type="button">导出 CSV</button></form>
              {analytics ? <><div className="commercial-kpis"><article><span>会话</span><strong>{analytics.sessions.total}</strong><small>活跃 {analytics.sessions.active}</small></article><article><span>已完成</span><strong>{analytics.sessions.completed}</strong><small>失败 {analytics.sessions.failed}</small></article><article><span>平均时长</span><strong>{Math.round(analytics.sessions.avg_duration_seconds)}s</strong><small>全部会话</small></article><article><span>推理成功率</span><strong>{Math.round(analytics.inference.success_rate * 100)}%</strong><small>{analytics.inference.succeeded}/{analytics.inference.attempts}</small></article></div><div className="commercial-two-column"><section className="commercial-panel"><div className="commercial-panel-title"><div><span>USAGE</span><h2>模型用量与成本</h2></div></div><div className="commercial-table-wrap"><table><thead><tr><th>类别</th><th>提供商/模型</th><th>用量</th><th>延迟</th><th>成本 USD</th></tr></thead><tbody>{analytics.usage.map((item, index) => <tr key={`${item.category}-${item.provider}-${index}`}><td>{item.category}</td><td>{item.provider}/{item.model}</td><td>{item.quantity} {item.unit}</td><td>{Math.round(item.avg_latency_ms)}ms</td><td>{item.cost_usd.toFixed(6)}</td></tr>)}</tbody></table></div></section><section className="commercial-panel"><div className="commercial-panel-title"><div><span>EVENTS</span><h2>事件分布</h2></div></div><div className="commercial-state-grid">{Object.entries(analytics.events).map(([name, count]) => <span key={name}><b>{count}</b>{name}</span>)}</div></section></div></> : <p className="commercial-muted">暂无分析数据。</p>}
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>SESSION ANALYTICS</span><h2>会话分析明细</h2></div><b>{analyticsSessions.length}</b></div><div className="commercial-table-wrap"><table><thead><tr><th>会话</th><th>Agent</th><th>状态</th><th>事件</th><th>成本</th><th>开始</th><th>操作</th></tr></thead><tbody>{analyticsSessions.map((session) => <tr key={session.id}><td>{session.room_name}<small>{session.id}</small></td><td>{session.agent_name}</td><td>{session.status}</td><td>{session.event_count ?? "—"}</td><td>${Number(session.cost_usd || 0).toFixed(6)}</td><td>{displayTime(session.started_at)}</td><td><button onClick={() => { setTab("sessions"); openSession(session.id); }} type="button">时间线</button></td></tr>)}</tbody></table></div>{analyticsCursor ? <button onClick={() => loadAnalytics(undefined, true)} type="button">加载更多</button> : null}</section>
            </div>
          ) : null}

          {tab === "sessions" ? (
            <div className="commercial-two-column commercial-master-detail">
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>INSIGHTS</span><h2>运行会话</h2></div><b>{sessions.length}</b></div><div className="commercial-list">{sessions.map((session) => <button className={selectedSessionId === session.id ? "selected" : ""} key={session.id} onClick={() => openSession(session.id)} type="button"><span><strong>{session.room_name}</strong><small>{session.agent_name} · {displayTime(session.started_at)}</small></span><i className={`commercial-status ${session.status}`}>{session.status}</i></button>)}</div></section>
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>LIVE CONSOLE</span><h2>时间线与运行时控制</h2></div><b>{timeline?.summary.event_count ?? 0}</b></div>{timeline ? <><dl className="commercial-detail-grid"><div><dt>状态</dt><dd>{timeline.session.status}</dd></div><div><dt>成本</dt><dd>${timeline.summary.cost_usd.toFixed(6)}</dd></div><div><dt>事件</dt><dd>{timeline.summary.event_count}</dd></div><div><dt>用量记录</dt><dd>{timeline.summary.usage_count}</dd></div></dl><form className="commercial-command-row commercial-form" onSubmit={sendConsoleCommand}><label>命令<select name="kind"><option value="agent.say">Agent 播报</option><option value="dtmf">发送 DTMF</option><option value="call.hangup">挂断通话</option></select></label><label>内容<input name="value" placeholder="播报文本或 DTMF 数字" /></label><button disabled={!canConsole} type="submit">下发命令</button>{timeline.session.status === "active" ? <><button onClick={() => closeSession("completed")} type="button">正常关闭</button><button className="danger" onClick={() => closeSession("failed")} type="button">标记失败</button></> : null}</form><div className="commercial-timeline">{timeline.events.map((event) => <article key={event.id}><b>#{event.sequence}</b><div><strong>{event.event_type}</strong><small>{event.source} · {displayTime(event.occurred_at)}</small><pre>{pretty(event.payload)}</pre></div></article>)}</div><h3>命令状态</h3><div className="commercial-list compact">{consoleCommands.map((command) => <article key={command.id}><div><strong>{command.command_type} · {command.status}</strong><span>{pretty(command.payload)}</span></div><small>{displayTime(command.completed_at || command.created_at)}</small></article>)}</div></> : <p className="commercial-muted">选择会话以查看完整时间线。</p>}</section>
            </div>
          ) : null}

          {tab === "agents" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveAgentSpec}><div className="commercial-panel-title"><div><span>AGENT BUILDER</span><h2>{editingSpec ? `编辑 ${editingSpec.name} · r${editingSpec.revision}` : "创建 AgentSpec"}</h2></div>{editingSpec ? <button onClick={() => setEditingSpec(null)} type="button">取消编辑</button> : null}</div><label>AgentSpec JSON<textarea name="spec" key={editingSpec?.id || "new"} defaultValue={editingSpec ? pretty(editingSpec.spec) : pretty({ schema_version: "1.0", name: "sales-agent", instructions: "请专业、合规地服务客户。", welcome_greeting: "您好，请问有什么可以帮您？", models: { stt: "qwen/stt:zh", llm: "qwen/qwen-plus", tts: "qwen/qwen3-tts:Cherry" }, conversation: { mode: "open", fields: [] }, tools: [], metadata_schema: {}, end_call: { final_response: "感谢您的来电，再见。", delete_room: false, summary_enabled: true, summary_instructions: "总结客户诉求与处理结果。", result_endpoint: null, headers: {} } })} rows={18} spellCheck={false} required /></label><button className="commercial-primary" disabled={!canAgentWrite} type="submit">{editingSpec ? "保存新修订" : "创建规范"}</button></form>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>SPEC REGISTRY</span><h2>AgentSpec</h2></div><b>{agentSpecs.length}</b></div><div className="commercial-list">{agentSpecs.map((spec) => <article key={spec.id}><div><strong>{spec.name} · r{spec.revision}</strong><span>{spec.status}</span><small>{displayTime(spec.updated_at)}</small></div><div><button onClick={() => setEditingSpec(spec)} type="button">编辑</button><button disabled={!canAgentWrite || spec.status === "published"} onClick={() => publishSpec(spec)} type="button">发布</button><button onClick={() => exportSpec(spec)} type="button">导出</button></div></article>)}</div></section>
              </div>
              <div className="commercial-three-column">
                <form className="commercial-panel commercial-form" onSubmit={createAgent}><div className="commercial-panel-title"><div><span>AGENT</span><h2>运行 Agent</h2></div></div><label>名称<input name="name" required /></label><label>说明<textarea name="description" rows={3} /></label><button className="commercial-primary" disabled={!canAgentWrite} type="submit">创建 Agent</button><div className="commercial-list compact">{agents.map((agent) => <button key={agent.id} onClick={() => loadVersions(agent.id)} type="button"><span><strong>{agent.name}</strong><small>{agent.status}</small></span></button>)}</div></form>
                <form className="commercial-panel commercial-form" onSubmit={buildAgent}><div className="commercial-panel-title"><div><span>BUILD</span><h2>构建版本</h2></div></div><label>Agent<select name="agent_id" onChange={(event) => loadVersions(event.target.value)} required><option value="">请选择</option>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select></label><label>Source Ref<input name="source_ref" required placeholder="git+https://...#commit" /></label><label>Image Ref<input name="image_ref" required placeholder="registry.example.com/voice:v1" /></label><label>构建配置 JSON<textarea name="spec" defaultValue="{}" rows={4} /></label><button className="commercial-primary" disabled={!canAgentWrite} type="submit">构建</button><div className="commercial-list compact">{versions.map((version) => <article key={version.id}><div><strong>v{version.version_number}</strong><span>{version.image_ref}</span></div><small>{version.status}</small></article>)}</div></form>
                <form className="commercial-panel commercial-form" onSubmit={createDeployment}><div className="commercial-panel-title"><div><span>DEPLOY</span><h2>创建部署</h2></div></div><label>部署名称<input name="name" required /></label><label>Agent<select name="agent_id" required><option value="">请选择</option>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select></label><label>Version ID<input name="version_id" list="version-ids" required /></label><datalist id="version-ids">{versions.map((version) => <option value={version.id} key={version.id}>v{version.version_number}</option>)}</datalist><label>副本数<input name="desired_replicas" type="number" defaultValue={1} min={0} max={100} /></label><button className="commercial-primary" disabled={!canAgentWrite} type="submit">部署</button></form>
              </div>
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>DEPLOYMENTS</span><h2>生产部署</h2></div><b>{deployments.length}</b></div><form className="commercial-toolbar" onSubmit={rolloutDeployment}><select name="deployment_id" required><option value="">部署</option>{deployments.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select><input name="version_id" list="version-ids" placeholder="新 Version ID" required /><button disabled={!canAgentWrite} type="submit">滚动发布</button></form><div className="commercial-list">{deployments.map((deployment) => <article key={deployment.id}><div><strong>{deployment.name} · {deployment.status}</strong><span>副本 {deployment.desired_replicas} · active {deployment.active_version_id}</span><small>{displayTime(deployment.updated_at)}</small></div><div><button onClick={() => viewRuntime(deployment)} type="button">实例/日志</button><button disabled={!canAgentWrite || !deployment.previous_version_id} onClick={() => rollbackDeployment(deployment)} type="button">回滚</button></div></article>)}</div>{runtimeView ? <div className="commercial-two-column commercial-runtime"><div><h3>运行实例</h3><pre className="commercial-code">{pretty(runtimeView.instances)}</pre></div><div><h3>最近日志</h3><pre className="commercial-code">{pretty(runtimeView.logs)}</pre></div></div> : null}</section>
            </div>
          ) : null}

          {tab === "integrations" ? (
            <div className="commercial-stack">
              <div className="commercial-three-column">
                <form className="commercial-panel commercial-form" onSubmit={saveSecret}><div className="commercial-panel-title"><div><span>SECRETS</span><h2>运行密钥</h2></div></div><label>名称<input name="name" required pattern="[A-Za-z_][A-Za-z0-9_]*" /></label><label>值<input name="value" type="password" autoComplete="new-password" required /></label><button className="commercial-primary" disabled={!canManage} type="submit">加密保存</button><small>密钥值不会从 API 返回。</small><div className="commercial-list compact">{secrets.map((secret) => <article key={secret.name}><strong>{secret.name}</strong><small>{displayTime(secret.updated_at)}</small></article>)}</div></form>
                <form className="commercial-panel commercial-form" onSubmit={saveInferenceRoute}><div className="commercial-panel-title"><div><span>INFERENCE</span><h2>模型路由</h2></div></div><label>Descriptor<input name="descriptor" required placeholder="qwen/primary" /></label><div className="commercial-inline"><label>模态<select name="modality"><option value="llm">LLM</option><option value="stt">STT</option><option value="tts">TTS</option></select></label><label>优先级<input name="priority" type="number" defaultValue={100} min={0} /></label></div><label>Provider<input name="provider" required /></label><label>Provider Model<input name="provider_model" required /></label><label>超时秒<input name="timeout_seconds" type="number" defaultValue={30} min={1} max={300} /></label><label>配置 JSON<textarea name="config" defaultValue="{}" rows={3} /></label><label className="commercial-check"><input name="enabled" type="checkbox" defaultChecked /> 启用</label><button className="commercial-primary" disabled={!canManage} type="submit">保存路由</button></form>
                <form className="commercial-panel commercial-form" onSubmit={saveEmbedConfig}><div className="commercial-panel-title"><div><span>EMBED</span><h2>网页语音组件</h2></div></div><label>配置名称<input name="name" required /></label><label>Agent 名称<input name="agent_name" required /></label><label>Room 前缀<input name="room_prefix" defaultValue="embed" required /></label><label>允许来源<textarea name="allowed_origins" required placeholder="https://www.example.com" rows={3} /></label><div className="commercial-check-row"><label className="commercial-check"><input name="audio" type="checkbox" defaultChecked /> 音频</label><label className="commercial-check"><input name="text" type="checkbox" defaultChecked /> 文本</label><label className="commercial-check"><input name="video" type="checkbox" /> 视频</label><label className="commercial-check"><input name="enabled" type="checkbox" defaultChecked /> 启用</label></div><button className="commercial-primary" disabled={!canManage} type="submit">创建配置</button></form>
              </div>
              <div className="commercial-two-column">
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>ROUTES</span><h2>推理路由</h2></div><b>{inferenceRoutes.length}</b></div><div className="commercial-list">{inferenceRoutes.map((route) => <article key={route.id || `${route.descriptor}-${route.modality}`}><div><strong>{route.descriptor} · {route.modality}</strong><span>{route.provider}/{route.provider_model}</span><small>优先级 {route.priority} · {route.timeout_seconds}s</small></div><i className={`commercial-status ${route.enabled ? "active" : "disabled"}`}>{route.enabled ? "enabled" : "disabled"}</i></article>)}</div></section>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>EMBED CONFIGS</span><h2>嵌入配置</h2></div><b>{embedConfigs.length}</b></div><div className="commercial-list">{embedConfigs.map((config) => <article key={config.id}><div><strong>{config.name} · {config.agent_name}</strong><span>{config.allowed_origins.join(", ")}</span><code>{`<qwen-voice-agent config-id="${config.id}"></qwen-voice-agent>`}</code></div><div><i className={`commercial-status ${config.enabled ? "active" : "disabled"}`}>{config.enabled ? "enabled" : "disabled"}</i><button disabled={!canManage} onClick={() => toggleEmbedConfig(config)} type="button">{config.enabled ? "停用" : "启用"}</button></div></article>)}</div></section>
              </div>
              <form className="commercial-panel commercial-form" onSubmit={invokeInference}><div className="commercial-panel-title"><div><span>INFERENCE TEST</span><h2>受控模型调用测试</h2></div></div><div className="commercial-form-grid"><label>Descriptor<select name="descriptor" required><option value="">请选择</option>{inferenceRoutes.filter((route) => route.enabled).map((route) => <option value={route.descriptor} key={`${route.descriptor}-${route.modality}`}>{route.descriptor} · {route.modality}</option>)}</select></label><label>模态<select name="modality"><option value="llm">LLM</option><option value="stt">STT</option><option value="tts">TTS</option></select></label><label>Session ID<input name="session_id" /></label><label>参数 JSON<textarea name="parameters" defaultValue="{}" rows={4} /></label><label className="full">输入 JSON<textarea name="input" defaultValue={pretty({ text: "你好" })} rows={6} required /></label></div><button className="commercial-primary" disabled={!canOperate || !inferenceRoutes.length} type="submit">执行测试</button>{inferenceResult ? <pre className="commercial-code">{pretty(inferenceResult)}</pre> : null}</form>
            </div>
          ) : null}

          {tab === "access" ? (
            <div className="commercial-stack">
              <div className="commercial-two-column">
                <form className="commercial-panel commercial-form" onSubmit={saveMember}><div className="commercial-panel-title"><div><span>PROJECT RBAC</span><h2>添加或调整成员</h2></div></div><label>IdP 用户标识<input name="user_id" required /></label><label>角色<select name="role"><option value="viewer">只读观察者</option><option value="member">业务成员</option><option value="admin">管理员</option><option value="owner">所有者</option><option value="worker">服务工作节点</option></select></label><button className="commercial-primary" disabled={busy || !canManage} type="submit">保存权限</button><small>系统在同一事务内锁定项目，阻止并发操作移除最后一位所有者。worker 仅用于 Agent/Dispatcher 的 Client Credentials 身份。</small></form>
                <section className="commercial-panel"><div className="commercial-panel-title"><div><span>MEMBERS</span><h2>项目成员</h2></div><b>{members.length}</b></div>{members.length ? <div className="commercial-members">{members.map((member) => <div key={member.user_id}><span>{member.user_id}</span><strong>{member.role}</strong><button className="danger" disabled={!canManage || (member.user_id === platformAuthSubject(auth) && member.role === "owner")} onClick={() => deleteMember(member)} type="button">移除</button></div>)}</div> : <p className="commercial-muted">当前角色无权读取成员列表。</p>}</section>
              </div>
              <section className="commercial-panel"><div className="commercial-panel-title"><div><span>AUDIT</span><h2>不可抵赖审计日志</h2></div><div><button disabled={!canManage} onClick={purgeRetention} type="button">执行数据保留清理</button></div></div><div className="commercial-table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>资源</th><th>载荷</th></tr></thead><tbody>{auditLogs.map((log) => <tr key={log.id}><td>{displayTime(log.created_at)}</td><td>{log.actor_id}</td><td>{log.action}</td><td>{log.resource_type}<small>{log.resource_id}</small></td><td><code>{pretty(log.payload || {})}</code></td></tr>)}{!auditLogs.length ? <tr><td colSpan={5} className="commercial-empty-row">当前角色无审计读取权限或暂无记录</td></tr> : null}</tbody></table></div></section>
            </div>
          ) : null}
        </>
      )}
      {(loading || busy) ? <div className="commercial-progress" aria-label="正在处理" /> : null}
    </section>
  );
}
