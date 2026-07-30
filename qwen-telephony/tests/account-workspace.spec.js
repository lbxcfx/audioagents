import { expect, test } from "@playwright/test";

test("user registers, logs in and sees the two product domains", async ({ page }) => {
  await page.route("**/api/accounts/register", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.email).toBe("lin@example.com");
    expect(body.accepted_terms).toBe(true);
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ id: "user-lin" }) });
  });
  await page.route("**/api/accounts/login", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.identifier).toBe("lin@example.com");
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ access_token: "test-access-token", subject: "林女士", expires_in: 3600 }) });
  });

  await page.goto("/login");
  await page.getByRole("button", { name: "免费注册" }).click();
  await expect(page.getByRole("heading", { name: "欢迎加入" })).toBeVisible();
  await page.getByLabel("您的称呼").fill("林女士");
  await page.getByLabel("邮箱", { exact: true }).fill("lin@example.com");
  await page.getByLabel("设置密码").fill("safe-password-123");
  await page.getByLabel("确认密码").fill("safe-password-123");
  await page.getByText("我已阅读并同意").click();
  await page.getByRole("button", { name: "注册账号" }).click();
  await expect(page.getByText("注册成功，请使用邮箱和密码登录。")).toBeVisible();

  await page.getByLabel("邮箱或手机号").fill("lin@example.com");
  await page.getByLabel("密码", { exact: true }).fill("safe-password-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/home$/);
  await expect(page.getByRole("heading", { name: "今天想从哪里开始？" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /让 Agent 理解业务/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /把批量联系/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /进入智能客服/ })).toHaveAttribute("href", "/app/inbound/agents");
  await expect(page.getByRole("link", { name: /进入智能外呼/ })).toHaveAttribute("href", "/app/dashboard");
});

test("registration and product choice remain usable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");
  await page.getByRole("button", { name: "免费注册" }).click();
  await expect(page.getByLabel("邮箱", { exact: true })).toBeVisible();

  await page.evaluate(() => sessionStorage.setItem("voicePlatformAuth", JSON.stringify({ mode: "development", userId: "mobile@example.com" })));
  await page.goto("/app/home");
  await expect(page.getByRole("heading", { name: "今天想从哪里开始？" })).toBeVisible();
  await expect(page.getByRole("link", { name: /进入智能客服/ })).toBeVisible();
});
