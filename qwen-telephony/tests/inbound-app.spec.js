const { test, expect } = require("@playwright/test");

test("public inbound experience explains limits and handles quota errors", async ({ page }) => {
  await page.route("**/inbound-api/public/demo", (route) => route.fulfill({ json: {
    available: true,
    name: "温暖的服务助手",
    description: "公开体验",
    max_duration_seconds: 180,
    notice: "请勿提供敏感信息。",
  } }));
  await page.route("**/inbound-api/public/demo/web-sessions", (route) => route.fulfill({
    status: 429,
    json: { detail: "今日体验次数已经用完，请明天再来。" },
  }));
  await page.goto("/experience/voice");

  await expect(page.getByRole("heading", { name: /打一通自然的电话/ })).toBeVisible();
  await expect(page.getByText("无需注册")).toBeVisible();
  await expect(page.getByText("不开放业务工具")).toBeVisible();
  await page.getByRole("button", { name: "立即体验" }).click();
  await expect(page.getByRole("alert")).toContainText("今日体验次数已经用完");
  await expect(page.getByRole("button", { name: "再次体验" })).toBeVisible();
});

test("public inbound experience remains usable on a narrow mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/inbound-api/public/demo", (route) => route.fulfill({ json: {
    available: true, name: "温暖的服务助手", max_duration_seconds: 180,
    notice: "请勿提供敏感信息。",
  } }));
  await page.goto("/experience/voice");
  await expect(page.locator(".voice-stage")).toBeVisible();
  await expect(page.getByRole("button", { name: "立即体验" })).toBeInViewport();
  await expect(page.locator(".inbound-console-link")).toBeVisible();
});

test("public inbound experience attaches remote audio and controls the microphone", async ({ page }) => {
  await page.addInitScript(() => {
    window.__voiceTest = { attached: 0, microphone: [] };
    window.__inboundVoiceRoomFactory = () => {
      const handlers = {};
      return {
        on(event, callback) { handlers[event] = callback; return this; },
        async connect() {
          handlers.trackSubscribed?.({
            kind: "audio",
            attach() { window.__voiceTest.attached += 1; },
            detach() {},
          });
        },
        async disconnect() { handlers.disconnected?.(); },
        localParticipant: {
          async setMicrophoneEnabled(value) { window.__voiceTest.microphone.push(value); },
        },
      };
    };
  });
  await page.route("**/inbound-api/public/demo", (route) => route.fulfill({ json: {
    available: true, name: "温暖的服务助手", max_duration_seconds: 180, notice: "注意隐私",
  } }));
  await page.route("**/inbound-api/public/demo/web-sessions", (route) => route.fulfill({ status: 201, json: {
    session_id: "session-1", token: "token", url: "ws://livekit.test",
    max_duration_seconds: 180, remaining_calls: 2,
  } }));
  await page.route("**/inbound-api/public/demo/sessions/session-1", (route) => route.fulfill({ json: {
    status: "active", termination_reason: "",
  } }));
  await page.goto("/experience/voice");
  await page.getByRole("button", { name: "立即体验" }).click();
  await expect(page.getByRole("status")).toHaveText("正在通话");
  await expect.poll(() => page.evaluate(() => window.__voiceTest.attached)).toBe(1);
  await expect.poll(() => page.evaluate(() => window.__voiceTest.microphone)).toEqual([true]);
  await page.getByRole("button", { name: "静音" }).click();
  await expect.poll(() => page.evaluate(() => window.__voiceTest.microphone)).toEqual([true, false]);
  await page.getByRole("button", { name: "结束通话" }).click();
  await expect(page.getByText("通话已结束")).toBeVisible();
});

test("enterprise inbound console loads agents and opens version-pinned detail", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem(
    "voicePlatformAuth",
    JSON.stringify({ mode: "development", userId: "owner" }),
  ));
  await page.route("**/api/platform/projects", (route) => route.fulfill({ json: {
    items: [{ id: "project-1", name: "示例企业", role: "owner" }],
  } }));
  await page.route("**/inbound-api/projects/project-1/agents", (route) => route.fulfill({ json: {
    items: [{
      id: "agent-1", project_id: "project-1", kind: "enterprise", name: "客户服务助手",
      description: "处理预约与售后咨询", status: "published", draft_revision: 2,
      active_version_id: "version-2", binding_count: 1, session_count: 12,
      updated_at: "2026-07-30T10:00:00Z",
    }],
  } }));
  await page.route("**/inbound-api/projects/project-1/agents/agent-1", (route) => route.fulfill({ json: {
    id: "agent-1", project_id: "project-1", kind: "enterprise", name: "客户服务助手",
    description: "处理预约与售后咨询", status: "published", draft_revision: 2,
    active_version_id: "version-2", binding_count: 1, session_count: 12,
    updated_at: "2026-07-30T10:00:00Z",
    draft_config: {
      instructions: "你是一个耐心的服务助手，需要准确回答客户问题。",
      welcome_message: "您好，请问有什么可以帮您？", voice: "Cherry", language: "zh-CN",
      max_duration_seconds: 600, recording_mode: "off", recording_disclosure: "",
      tools: [], knowledge_sources: [],
    },
    bindings: [{
      id: "binding-1", entry_type: "sip_did", destination: "+8613800000000",
      trunk_id: "trunk-1", agent_version_id: "version-1", dispatch_rule_id: "rule-1", status: "active",
    }],
  } }));
  await page.route("**/inbound-api/projects/project-1/agents/agent-1/versions", (route) => route.fulfill({ json: {
    items: [
      { id: "version-2", revision: 2, config_sha256: "222222222222", published_at: "2026-07-30T10:00:00Z" },
      { id: "version-1", revision: 1, config_sha256: "111111111111", published_at: "2026-07-29T10:00:00Z" },
    ],
  } }));
  await page.goto("/app/inbound/agents");

  await expect(page.getByRole("heading", { name: "认真接好客户的每一次来电" })).toBeVisible();
  await expect(page.getByText("客户服务助手", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /客户服务助手/ }).click();
  await expect(page.getByRole("heading", { name: "客户服务助手" })).toBeVisible();
  await expect(page.getByText("线上版本可用")).toBeVisible();
  await page.getByRole("button", { name: "号码与网页" }).click();
  await expect(page.getByText("+8613800000000")).toBeVisible();
  await expect(page.getByText("运行正常")).toBeVisible();
});

test("enterprise inbound console protects unauthenticated deep links", async ({ page }) => {
  await page.goto("/app/inbound/agents");
  await expect(page.getByRole("heading", { name: "请先登录控制台" })).toBeVisible();
  await expect(page.getByRole("link", { name: "前往登录" })).toHaveAttribute("href", "/login");
});
