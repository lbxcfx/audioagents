import { platformAuthHeaders, type PlatformAuthSession } from "./platformAuth";

export const inboundApiBase = import.meta.env.VITE_INBOUND_API_BASE
  || (import.meta.env.DEV ? "http://127.0.0.1:8092" : window.location.origin);

export class InboundApiError extends Error {
  status: number;
  code: string;
  retryAfter: number | null;

  constructor(message: string, status: number, code = "request_failed", retryAfter: number | null = null) {
    super(message);
    this.name = "InboundApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export async function inboundRequest<T>(
  path: string,
  options: RequestInit = {},
  auth: PlatformAuthSession | null = null,
): Promise<T> {
  const response = await fetch(`${inboundApiBase}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...platformAuthHeaders(auth),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({})) as { detail?: unknown; code?: string };
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail
      : Array.isArray(payload.detail)
        ? payload.detail.map((item) => typeof item === "object" && item && "msg" in item ? String(item.msg) : "输入内容有误").join("；")
        : `请求失败（${response.status}）`;
    const retryHeader = response.headers.get("Retry-After");
    throw new InboundApiError(detail, response.status, payload.code || "request_failed", retryHeader ? Number(retryHeader) : null);
  }
  return payload as T;
}

export type InboundAgentSummary = {
  id: string;
  project_id: string;
  kind: "enterprise" | "public_demo";
  name: string;
  description: string;
  status: string;
  draft_revision: number;
  active_version_id: string | null;
  binding_count: number;
  session_count: number;
  updated_at: string;
};

export type InboundAgentDetail = InboundAgentSummary & {
  draft_config: {
    instructions: string;
    welcome_message: string;
    voice: string;
    language: string;
    max_duration_seconds: number;
    recording_mode: "off" | "disclosed";
    recording_disclosure: string;
    tools: Array<Record<string, unknown>>;
    knowledge_sources: string[];
  };
  bindings: Array<{
    id: string;
    entry_type: "sip_did" | "web";
    destination: string;
    trunk_id: string;
    agent_version_id: string;
    dispatch_rule_id: string;
    status: string;
  }>;
};
