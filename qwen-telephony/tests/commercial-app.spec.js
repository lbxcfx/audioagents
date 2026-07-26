const { test, expect } = require("@playwright/test");

const call = {
  id: "call-1",
  direction: "inbound",
  status: "active",
  destination_number: "+8610",
  source_number: "+86138****0001",
  agent_name: "support",
  room_name: "room-1",
  provider_call_id: "provider-1",
  recording_status: "completed",
  recording_storage_uri: "s3://recordings/call-1.ogg",
  recording_egress_id: "EG_1",
  created_at: "2026-07-25T10:00:00Z",
  answered_at: "2026-07-25T10:00:05Z",
  failure_code: "",
  attempt_count: 1,
  max_attempts: 3,
};

async function authenticateAndMock(page) {
  await page.addInitScript(() => {
    sessionStorage.setItem(
      "voicePlatformAuth",
      JSON.stringify({ mode: "development", userId: "owner" }),
    );
  });

  // Generic optional-module fallback is registered first; exact routes below win.
  await page.route(/\/api\/platform\/projects\/project-1\/.*/, (route) => {
    const method = route.request().method();
    route.fulfill({ status: method === "POST" ? 201 : 200, json: method === "GET" ? { items: [] } : { id: "saved" } });
  });
  await page.route("**/api/platform/projects", async (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({ json: { id: "project-1", name: "Commercial", slug: "commercial", role: "owner", retention_days: 30 } });
    }
    return route.fulfill({ json: { items: [{ id: "project-1", name: "Commercial", slug: "commercial", role: "owner", retention_days: 30 }] } });
  });
  await page.route("**/api/platform/projects/project-1/telephony/metrics", (route) => route.fulfill({ json: { states: { "outbound.queued": 7, "inbound.active": 2 }, queue_depth: 7, active_calls: 2, stale_leases: 0, attempts: { total: 20, completed: 18, failed: 2 } } }));
  await page.route(/\/telephony\/calls\?limit=500$/, (route) => route.fulfill({ json: { items: [call] } }));
  await page.route(/\/telephony\/contacts\?.*$/, (route) => route.fulfill({ json: { items: [{ id: "contact-1", external_id: "crm-1", name: "测试客户", phone_number: "+86138****0001", status: "active", updated_at: "2026-07-25T09:00:00Z" }], next_cursor: null } }));
  await page.route(/\/telephony\/campaigns\?limit=500$/, (route) => route.fulfill({ json: { items: [{ id: "campaign-1", name: "续费提醒", status: "running", contact_count: 100, queued_count: 12, terminal_count: 63, blocked_count: 2, max_concurrent_calls: 10 }] } }));
  await page.route("**/api/platform/projects/project-1/telephony/trunks", (route) => route.fulfill({ json: { items: [{ id: "trunk-1", name: "primary", direction: "bidirectional", provider: "carrier", livekit_trunk_id: "ST_primary", status: "active", numbers: ["+8610"], max_concurrent_calls: 100, max_calls_per_second: 5 }] } }));
  await page.route("**/api/platform/projects/project-1/telephony/policy", (route) => route.fulfill({ json: { outbound_enabled: true, timezone: "Asia/Shanghai", allowed_weekdays: [0, 1, 2, 3, 4], calling_window_start: "09:00", calling_window_end: "18:00", require_consent: true, consent_purpose: "outbound", max_attempts_per_number_per_day: 3, inbound_overflow_mode: "reject", inbound_overflow_destination_name: "", recording_mode: "always", recording_disclosure_text: "本次通话将被录音" } }));
  await page.route("**/api/platform/projects/project-1/telephony/limits", (route) => route.fulfill({ json: { max_concurrent_calls: 100, max_outbound_calls: 80, max_inbound_calls: 40, max_calls_per_minute: 600, lease_seconds: 30 } }));
  await page.route("**/api/platform/projects/project-1/members", (route) => route.fulfill({ json: { items: [{ user_id: "owner", role: "owner" }] } }));
  await page.route(/\/telephony\/do-not-call.*/, (route) => {
    if (route.request().method() === "POST") return route.fulfill({ status: 201, json: { id: "dnc-2" } });
    return route.fulfill({ json: { items: [{ id: "dnc-1", phone_last4: "0001", reason: "客户拒接", source: "operator", created_at: "2026-07-25T09:00:00Z" }] } });
  });
  await page.route(/\/telephony\/consents.*/, (route) => route.fulfill({ json: { items: [{ id: "consent-1", phone_last4: "0001", purpose: "outbound", status: "granted", evidence_ref: "crm://42", valid_from: "2026-07-25T09:00:00Z", created_at: "2026-07-25T09:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/telephony/transfer-destinations", (route) => route.fulfill({ json: { items: [{ id: "destination-1", name: "human-support", target_uri: "tel:+861000000001", mode: "cold", status: "active" }] } }));
  await page.route("**/api/platform/projects/project-1/audit-logs?limit=500", (route) => route.fulfill({ json: { items: [{ id: "audit-1", actor_id: "owner", action: "project.create", resource_type: "project", resource_id: "project-1", payload: {}, created_at: "2026-07-25T08:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/analytics/summary", (route) => route.fulfill({ json: { sessions: { total: 1, active: 1, completed: 0, failed: 0, avg_duration_seconds: 12 }, usage: [], inference: { attempts: 1, succeeded: 1, success_rate: 1, avg_latency_ms: 50 }, events: { "agent.started": 1 } } }));
  await page.route("**/api/platform/projects/project-1/analytics/sessions?limit=100", (route) => route.fulfill({ json: { items: [{ id: "call-1", room_name: "room-1", agent_name: "support", status: "active", started_at: "2026-07-25T10:00:00Z", cost_usd: 0.01, event_count: 1 }], next_cursor: null } }));
  await page.route("**/api/platform/projects/project-1/sessions?limit=200", (route) => route.fulfill({ json: { items: [{ id: "call-1", room_name: "room-1", agent_name: "support", status: "active", started_at: "2026-07-25T10:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/agent-specs", (route) => route.fulfill({ json: { items: [{ id: "spec-1", name: "support-agent", revision: 2, status: "draft", spec: { schema_version: "1.0", name: "support-agent" }, updated_at: "2026-07-25T09:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/agents", (route) => route.fulfill({ json: { items: [{ id: "agent-1", name: "support", description: "", status: "active", created_at: "2026-07-25T09:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/deployments", (route) => route.fulfill({ json: { items: [{ id: "deployment-1", name: "support-prod", agent_id: "agent-1", active_version_id: "version-1", desired_replicas: 3, status: "ready", updated_at: "2026-07-25T09:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/secrets", (route) => route.fulfill({ json: { items: [{ name: "QWEN_API_KEY", updated_at: "2026-07-25T09:00:00Z" }] } }));
  await page.route("**/api/platform/projects/project-1/embed-configs", (route) => route.fulfill({ json: { items: [{ id: "embed-1", name: "website", agent_name: "support", room_prefix: "embed", allowed_origins: ["https://example.com"], capabilities: { audio: true, text: true }, enabled: true }] } }));
  await page.route("**/api/platform/projects/project-1/inference/routes", (route) => route.fulfill({ json: { items: [{ id: "route-1", descriptor: "qwen/primary", modality: "llm", provider: "qwen", provider_model: "qwen-plus", priority: 100, timeout_seconds: 30, enabled: true, config: {} }] } }));
}

test("commercial app loads tenant metrics and queues an authenticated outbound call", async ({ page }) => {
  await authenticateAndMock(page);
  let outboundHeader = "";
  await page.route("**/api/platform/projects/project-1/telephony/calls/outbound", async (route) => {
    outboundHeader = route.request().headers()["x-user-id"] || "";
    await route.fulfill({ status: 202, json: { id: "call-2", status: "queued" } });
  });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "商用语音平台" })).toBeVisible();
  await expect(page.locator(".commercial-kpis")).toContainText("7");
  await expect(page.locator(".commercial-kpis")).toContainText("90%");
  await page.locator('input[name="destination_number"]').fill("+8613800000000");
  await page.locator('select[name="trunk_id"]').first().selectOption("trunk-1");
  await page.getByRole("button", { name: "加入外呼队列" }).click();
  await expect(page.getByRole("status")).toContainText("外呼任务已进入可靠队列");
  expect(outboundHeader).toBe("owner");
});

test("commercial app exposes inbound calls, campaigns and responsive navigation", async ({ page }) => {
  await authenticateAndMock(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "实时呼叫" }).click();
  await expect(page.locator(".commercial-table-wrap").first()).toContainText("呼入");
  await expect(page.locator(".commercial-table-wrap").first()).toContainText("room-1");
  await page.getByRole("button", { name: "活动与客户" }).click();
  await expect(page.locator(".commercial-campaign-grid")).toContainText("续费提醒");
  await expect(page.locator(".commercial-campaign-grid")).toContainText("63/100");
});

test("call detail obtains protected recording access and queues managed transfer", async ({ page }) => {
  await authenticateAndMock(page);
  let queuedCommand = null;
  await page.route("**/api/platform/projects/project-1/telephony/calls/call-1", (route) => route.fulfill({ json: call }));
  await page.route("**/api/platform/projects/project-1/telephony/calls/call-1/cdr", (route) => route.fulfill({ json: { provider: "livekit", sip_status: "200" } }));
  await page.route("**/api/platform/projects/project-1/telephony/calls/call-1/transfers", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/platform/projects/project-1/telephony/calls/call-1/recording-access?ttl_seconds=300", (route) => route.fulfill({ json: { url: "https://media.example.com/call-1.ogg", temporary: true, expires_at: "2026-07-25T10:05:00Z" } }));
  await page.route("**/api/platform/projects/project-1/sessions/call-1", (route) => route.fulfill({ json: { session: { id: "call-1", room_name: "room-1", agent_name: "support", status: "active", started_at: "2026-07-25T10:00:00Z" }, events: [], usage: [], summary: { event_count: 0, usage_count: 0, cost_usd: 0 } } }));
  await page.route("**/api/platform/projects/project-1/sessions/call-1/console/commands", async (route) => {
    if (route.request().method() === "POST") {
      queuedCommand = route.request().postDataJSON();
      return route.fulfill({ status: 202, json: { id: "command-1", status: "queued" } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "实时呼叫" }).click();
  await page.getByRole("button", { name: "详情" }).click();
  await expect(page.getByRole("heading", { name: "通话详情" })).toBeVisible();
  await page.getByRole("button", { name: "获取安全访问链接" }).click();
  await expect(page.locator("audio")).toHaveAttribute("src", "https://media.example.com/call-1.ogg");
  await page.locator('select[name="destination_name"]').selectOption("human-support");
  await page.getByRole("button", { name: "人工转接" }).click();
  expect(queuedCommand.command_type).toBe("rpc");
  expect(queuedCommand.payload.method).toBe("call.transfer");
  expect(queuedCommand.payload.arguments.destination_name).toBe("human-support");
});

test("compliance, analytics, agent and integration modules render real API data", async ({ page }) => {
  await authenticateAndMock(page);
  await page.goto("/");
  await page.getByRole("button", { name: "合规中心" }).click();
  await expect(page.getByText("客户拒接")).toBeVisible();
  await expect(page.getByText("crm://42")).toBeVisible();
  await page.getByRole("button", { name: "分析报表" }).click();
  await expect(page.locator(".commercial-kpis")).toContainText("100%");
  await page.getByRole("button", { name: "Agent 与部署" }).click();
  await expect(page.getByText("support-agent · r2")).toBeVisible();
  await expect(page.getByText("support-prod · ready")).toBeVisible();
  await page.getByRole("button", { name: "模型与集成" }).click();
  await expect(page.locator(".commercial-list strong", { hasText: "qwen/primary · llm" })).toBeVisible();
  await expect(page.getByText("website · support")).toBeVisible();
});

test("production navigation hides replica-only modules and viewer actions are disabled", async ({ page }) => {
  await authenticateAndMock(page);
  await page.route("**/api/platform/projects", (route) => route.fulfill({ json: { items: [{ id: "project-1", name: "Commercial", slug: "commercial", role: "viewer", retention_days: 30 }] } }));
  await page.goto("/");

  await expect(page.getByRole("button", { name: "加入外呼队列" })).toBeDisabled();
  await expect(page.getByText("短信管理", { exact: true })).toHaveCount(0);
  await expect(page.getByText("财务管理", { exact: true })).toHaveCount(0);
  await expect(page.getByText("综合概况", { exact: true })).toHaveCount(0);
  await expect(page.getByText("话术配置", { exact: true })).toHaveCount(0);
  await expect(page.locator(".api-stick input")).toHaveCount(0);
  await expect(page.locator(".api-stick")).toContainText("同源 API");
});

test("worker role can be provisioned but cannot use human outbound controls", async ({ page }) => {
  await authenticateAndMock(page);
  await page.route("**/api/platform/projects", (route) => route.fulfill({ json: { items: [{ id: "project-1", name: "Worker", slug: "worker", role: "worker", retention_days: 30 }] } }));
  await page.goto("/");

  await expect(page.getByRole("button", { name: "加入外呼队列" })).toBeDisabled();
  await page.getByRole("button", { name: "活动与客户" }).click();
  await expect(page.getByRole("button", { name: "创建活动" })).toBeDisabled();
});

test("project RBAC form includes the dedicated service worker role", async ({ page }) => {
  await authenticateAndMock(page);
  await page.goto("/");
  await page.getByRole("button", { name: "权限与审计" }).click();

  const roleSelect = page.locator('select[name="role"]');
  await expect(roleSelect.locator('option[value="worker"]')).toHaveText("服务工作节点");
});

test("campaign creation preserves selected contacts independently of the current page", async ({ page }) => {
  await authenticateAndMock(page);
  let attachedContacts = [];
  await page.route("**/api/platform/projects/project-1/telephony/campaigns", (route) => route.fulfill({ status: 201, json: { id: "campaign-2", status: "draft" } }));
  await page.route("**/api/platform/projects/project-1/telephony/campaigns/campaign-2/contacts", (route) => {
    attachedContacts = route.request().postDataJSON().contact_ids;
    return route.fulfill({ json: { added: attachedContacts.length } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "活动与客户" }).click();
  await page.getByRole("checkbox", { name: "选择联系人 测试客户" }).check();
  await expect(page.getByText("已跨页选择 1 位联系人")).toBeVisible();
  const campaignForm = page.locator("form", { has: page.getByRole("heading", { name: "创建外呼活动" }) });
  await campaignForm.locator('input[name="name"]').fill("跨页活动");
  await campaignForm.locator('select[name="trunk_id"]').selectOption("trunk-1");
  await campaignForm.getByRole("button", { name: "创建活动" }).click();
  await expect.poll(() => attachedContacts).toEqual(["contact-1"]);
});
