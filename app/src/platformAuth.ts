export type PlatformAuthSession =
  | {
      mode: "bearer";
      accessToken: string;
      refreshToken?: string;
      subject: string;
      expiresAt: number | null;
    }
  | { mode: "development"; userId: string };

const SESSION_KEY = "voicePlatformAuth";
const PKCE_KEY = "voicePlatformOidcPkce";

type OidcConfiguration = {
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
  scope: string;
  redirectUri: string;
};

type OidcTokenResponse = {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
};

let refreshInFlight: Promise<PlatformAuthSession> | null = null;

function base64Url(bytes: Uint8Array): string {
  let value = "";
  bytes.forEach((byte) => {
    value += String.fromCharCode(byte);
  });
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomValue(size = 32): string {
  const bytes = new Uint8Array(size);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

function jwtClaims(token: string): Record<string, unknown> {
  try {
    const encoded = token.split(".")[1];
    if (!encoded) return {};
    const normalized = encoded.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export function oidcConfiguration(): OidcConfiguration | null {
  const authorizationEndpoint = String(import.meta.env.VITE_OIDC_AUTHORIZATION_ENDPOINT || "").trim();
  const tokenEndpoint = String(import.meta.env.VITE_OIDC_TOKEN_ENDPOINT || "").trim();
  const clientId = String(import.meta.env.VITE_OIDC_CLIENT_ID || "").trim();
  if (!authorizationEndpoint || !tokenEndpoint || !clientId) return null;
  return {
    authorizationEndpoint,
    tokenEndpoint,
    clientId,
    scope: String(import.meta.env.VITE_OIDC_SCOPE || "openid profile email offline_access").trim(),
    redirectUri: String(import.meta.env.VITE_OIDC_REDIRECT_URI || window.location.origin + window.location.pathname).trim(),
  };
}

export function loadPlatformAuth(): PlatformAuthSession | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as PlatformAuthSession;
    if (session.mode === "bearer") {
      if (!session.accessToken || (session.expiresAt && session.expiresAt <= Date.now() + 10_000 && !session.refreshToken)) {
        sessionStorage.removeItem(SESSION_KEY);
        return null;
      }
      return session;
    }
    return session.mode === "development" && session.userId ? session : null;
  } catch {
    sessionStorage.removeItem(SESSION_KEY);
    return null;
  }
}

export function savePlatformAuth(session: PlatformAuthSession | null): void {
  if (session) sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  else sessionStorage.removeItem(SESSION_KEY);
}

export function platformAuthHeaders(session: PlatformAuthSession | null): Record<string, string> {
  if (!session) return {};
  return session.mode === "bearer"
    ? { Authorization: `Bearer ${session.accessToken}` }
    : { "X-User-ID": session.userId };
}

export function platformAuthSubject(session: PlatformAuthSession | null): string {
  if (!session) return "未登录";
  return session.mode === "bearer" ? session.subject : session.userId;
}

function bearerSession(
  tokens: OidcTokenResponse,
  previous?: Extract<PlatformAuthSession, { mode: "bearer" }>,
): Extract<PlatformAuthSession, { mode: "bearer" }> {
  if (!tokens.access_token) throw new Error("企业 IdP 未返回 access_token");
  const claims = jwtClaims(tokens.access_token);
  const expiresAt = typeof claims.exp === "number"
    ? claims.exp * 1000
    : typeof tokens.expires_in === "number"
      ? Date.now() + tokens.expires_in * 1000
      : null;
  return {
    mode: "bearer",
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token || previous?.refreshToken,
    subject: String(
      claims.preferred_username
      || claims.email
      || claims.sub
      || previous?.subject
      || "企业用户",
    ),
    expiresAt,
  };
}

export async function refreshPlatformAuth(
  session: PlatformAuthSession,
  force = false,
): Promise<PlatformAuthSession> {
  if (session.mode === "development") return session;
  if (!force && (!session.expiresAt || session.expiresAt > Date.now() + 60_000)) {
    return session;
  }
  if (!session.refreshToken) {
    throw new Error("登录令牌即将过期且 IdP 未签发 refresh_token，请重新登录");
  }
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const config = oidcConfiguration();
    if (!config) throw new Error("尚未配置企业 IdP");
    const response = await fetch(config.tokenEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        client_id: config.clientId,
        refresh_token: session.refreshToken || "",
      }),
    });
    if (!response.ok) {
      throw new Error(`企业 IdP 刷新令牌失败（${response.status}）`);
    }
    const next = bearerSession((await response.json()) as OidcTokenResponse, session);
    savePlatformAuth(next);
    return next;
  })().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

export async function beginOidcLogin(): Promise<void> {
  const config = oidcConfiguration();
  if (!config) throw new Error("尚未配置企业 IdP");
  const verifier = randomValue(64);
  const challenge = base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const state = randomValue();
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, createdAt: Date.now() }));
  const url = new URL(config.authorizationEndpoint);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", config.clientId);
  url.searchParams.set("redirect_uri", config.redirectUri);
  url.searchParams.set("scope", config.scope);
  url.searchParams.set("state", state);
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  window.location.assign(url.toString());
}

export async function completeOidcLogin(): Promise<PlatformAuthSession | null> {
  const query = new URLSearchParams(window.location.search);
  const providerError = query.get("error");
  if (providerError) {
    const description = query.get("error_description") || providerError;
    sessionStorage.removeItem(PKCE_KEY);
    query.delete("error");
    query.delete("error_description");
    query.delete("state");
    const remaining = query.toString();
    history.replaceState({}, document.title, window.location.pathname + (remaining ? `?${remaining}` : "") + window.location.hash);
    throw new Error(`企业 IdP 登录失败：${description}`);
  }
  const code = query.get("code");
  if (!code) return null;
  const config = oidcConfiguration();
  if (!config) throw new Error("OIDC 回调到达，但前端缺少 IdP 配置");
  const raw = sessionStorage.getItem(PKCE_KEY);
  sessionStorage.removeItem(PKCE_KEY);
  if (!raw) throw new Error("OIDC 登录状态已失效，请重新登录");
  const saved = JSON.parse(raw) as { verifier: string; state: string; createdAt: number };
  if (saved.state !== query.get("state") || Date.now() - saved.createdAt > 10 * 60_000) {
    throw new Error("OIDC state 校验失败或登录已超时");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    code,
    code_verifier: saved.verifier,
  });
  const response = await fetch(config.tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) throw new Error(`企业 IdP 换取令牌失败（${response.status}）`);
  const tokens = (await response.json()) as OidcTokenResponse;
  const session = bearerSession(tokens);
  savePlatformAuth(session);
  query.delete("code");
  query.delete("state");
  query.delete("session_state");
  const remaining = query.toString();
  history.replaceState({}, document.title, window.location.pathname + (remaining ? `?${remaining}` : "") + window.location.hash);
  return session;
}
