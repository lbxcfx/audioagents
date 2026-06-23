const { test, expect } = require("@playwright/test");

async function openView(page, view) {
  await page.locator(`.nav-item[data-view="${view}"]`).click();
  await expect(page.locator(`#${view}.view`)).toHaveClass(/active/);
}

test("dialogue workbench loads scenes and starts robot-first training", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/话术管理工作台/);
  await expect(page.locator("#healthText")).toHaveText("API healthy");
  await expect(page.locator("#scripts.view")).toHaveClass(/active/);
  await expect(page.locator("#nluSwitch")).toBeVisible();

  await expect(page.locator(".scene-item").first()).toBeVisible();
  await expect(page.locator(".flow-node").first()).toBeVisible();
  await expect(page.locator("#activeSceneMeta")).not.toHaveText("");

  await openView(page, "training");
  await expect(page.locator("#trainingSceneSelect option").first()).toBeAttached();
  await page.locator("#startTrainingBtn").click();
  await expect(page.locator("#trainingChat .chat-message.assistant").first()).toBeVisible();
  await expect(page.locator("#trainingResult")).toContainText("route_type");
});

test("dialogue workbench remains usable on mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator('.nav-item[data-view="training"]')).toBeVisible();
  await openView(page, "training");
  await expect(page.locator("#trainingSceneSelect")).toBeVisible();
  await expect(page.locator('#trainingForm button[type="submit"]')).toBeVisible();
});
