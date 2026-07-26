const { test, expect } = require("@playwright/test");

async function mockPlatform(page) {
  await page.route("**/api/platform/health", (route) => route.fulfill({
    json: { status: "ok", module: "platform-foundation", schema_version: 19 },
  }));
  await page.route("**/api/platform/projects", (route) => route.fulfill({
    json: { items: [{ id: "project-1", name: "Cloud Parity QA", slug: "qa", role: "owner" }] },
  }));
  await page.route("**/api/platform/projects/project-1/analytics/summary", (route) => route.fulfill({
    json: {
      sessions: { total: 12, active: 2, completed: 9, failed: 1, avg_duration_seconds: 42 },
      usage: [{ cost_usd: 0.125 }],
      inference: { success_rate: 0.975, attempts: 40, succeeded: 39, avg_latency_ms: 180 },
      events: {}, range: {},
    },
  }));
  await page.route("**/api/platform/projects/project-1/analytics/sessions?limit=6", (route) => route.fulfill({
    json: { items: [{ id: "session-1", room_name: "support-room", agent_name: "support-agent", status: "active", event_count: 8, started_at: "2026-07-25T10:00:00Z", cost_usd: 0.01 }], next_cursor: null },
  }));
  await page.route("**/api/platform/projects/project-1/agent-specs", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/platform/projects/project-1/embed-configs", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/platform/projects/project-1/telephony/policy", (route) => route.fulfill({
    json: { outbound_enabled: true, recording_mode: "always", recording_disclosure_text: "本次通话将被录音。" },
  }));
  await page.route("**/api/platform/projects/project-1/telephony/trunks", (route) => route.fulfill({
    json: { items: [{ id: "trunk-1", name: "primary", provider: "carrier", status: "active", direction: "bidirectional", livekit_trunk_id: "ST_primary" }] },
  }));
  await page.route("**/api/platform/projects/project-1/telephony/contacts?limit=1000", (route) => route.fulfill({
    json: { items: [{ id: "contact-1", external_id: "crm-1001", name: "测试客户", phone_number: "+86138****0001", status: "active" }] },
  }));
  await page.route("**/api/platform/projects/project-1/telephony/campaigns?limit=200", (route) => route.fulfill({
    json: { items: [{ id: "campaign-1", name: "续费提醒", agent_name: "commercial-agent", trunk_id: "trunk-1", status: "running", scheduled_at: "2026-07-25T10:00:00Z", contact_count: 1, terminal_count: 0, blocked_count: 0 }] },
  }));
}

test("Cloud-Parity console presents project metrics and Builder", async ({ page }) => {
  await mockPlatform(page);
  await page.goto("/cloud-parity");

  await expect(page).toHaveTitle(/Cloud-Parity/);
  await expect(page.locator("#platformStatus")).toHaveText("Control plane online");
  await expect(page.locator("#schemaVersion")).toHaveText("Schema v19");
  await expect(page.locator("#metricSessions")).toHaveText("12");
  await expect(page.locator("#metricInference")).toHaveText("97.5%");
  await expect(page.locator("#recentSessionRows")).toContainText("support-room");

  await page.locator('.cp-nav[data-view="builder"]').click();
  await expect(page.locator("#builder.cp-view")).toHaveClass(/active/);
  await expect(page.locator('#builderForm textarea[name="instructions"]')).toBeVisible();
  await expect(page.locator("#agentSpecList")).toContainText("暂无 AgentSpec");
});

test("Cloud-Parity console exposes commercial outbound operations", async ({ page }) => {
  await mockPlatform(page);
  await page.goto("/cloud-parity");

  await page.locator('.cp-nav[data-view="telephony"]').click();
  await expect(page.locator("#telephony.cp-view")).toHaveClass(/active/);
  await expect(page.locator("#outboundPolicyStatus")).toHaveText("enabled");
  await expect(page.locator("#campaignTrunk")).toContainText("primary · carrier");
  await expect(page.locator("#telephonyContactRows")).toContainText("crm-1001");
  await expect(page.locator("#campaignRows")).toContainText("续费提醒");
  await expect(page.locator('[data-campaign-status="paused"]')).toBeVisible();
});

test("Cloud-Parity console remains keyboard-accessible at mobile width", async ({ page }) => {
  await mockPlatform(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/cloud-parity");

  await expect(page.locator('.cp-nav[data-view="embed"]')).toBeVisible();
  await page.locator('.cp-nav[data-view="embed"]').focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#embedForm")).toBeVisible();
  await expect(page.locator('#embedForm textarea[name="allowed_origins"]')).toBeVisible();
});
