import { FormEvent, useEffect, useState } from "react";
import { savePlatformAuth, type PlatformAuthSession } from "./platformAuth";

type Page = "home" | "login";
type AccountView = "login" | "register" | "forgot";

const DEVELOPMENT_ACCOUNTS_KEY = "voicePlatformDevelopmentAccounts";
const accountApiBase = String(import.meta.env.VITE_ACCOUNT_API_BASE || window.location.origin).replace(/\/$/, "");

async function accountRequest<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${accountApiBase}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) throw new Error(String(payload.detail || payload.message || `账户服务请求失败（${response.status}）`));
  return payload as T;
}

async function passwordDigest(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function pageFromPath(): Page {
  return window.location.pathname === "/login" ? "login" : "home";
}

export default function PublicExperience({ onLogin }: { onLogin: (session: PlatformAuthSession) => void }) {
  const [page, setPage] = useState<Page>(pageFromPath);
  const [accountView, setAccountView] = useState<AccountView>("login");
  const [message, setMessage] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loginIdentifier, setLoginIdentifier] = useState("");

  useEffect(() => {
    const sync = () => setPage(pageFromPath());
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  function navigate(next: Page) {
    history.pushState({}, "", next === "login" ? "/login" : "/");
    setPage(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const identifier = String(values.get("identifier") || "").trim();
    if (import.meta.env.DEV || import.meta.env.VITE_ALLOW_DEVELOPMENT_AUTH === "true") {
      const accounts = JSON.parse(localStorage.getItem(DEVELOPMENT_ACCOUNTS_KEY) || "[]") as Array<{ email: string; passwordHash: string }>;
      const registered = accounts.find((account) => account.email === identifier.toLowerCase());
      if (registered && registered.passwordHash !== await passwordDigest(String(values.get("password") || ""))) {
        setMessage("邮箱或密码不正确。");
        return;
      }
      const session: PlatformAuthSession = { mode: "development", userId: identifier };
      savePlatformAuth(session);
      onLogin(session);
      window.location.assign("/app/home");
      return;
    }
    try {
      const result = await accountRequest<{ access_token: string; refresh_token?: string; expires_in?: number; subject?: string }>("/api/accounts/login", {
        identifier,
        password: String(values.get("password") || ""),
      });
      const session: PlatformAuthSession = {
        mode: "bearer",
        accessToken: result.access_token,
        refreshToken: result.refresh_token,
        subject: result.subject || identifier,
        expiresAt: result.expires_in ? Date.now() + result.expires_in * 1000 : null,
      };
      savePlatformAuth(session);
      onLogin(session);
      window.location.assign("/app/home");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "登录失败，请稍后重试。");
    }
  }

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const email = String(values.get("email") || "").trim().toLowerCase();
    const password = String(values.get("password") || "");
    if (password !== String(values.get("confirm_password") || "")) {
      setMessage("两次输入的密码不一致。");
      return;
    }
    if (import.meta.env.DEV || import.meta.env.VITE_ALLOW_DEVELOPMENT_AUTH === "true") {
      const accounts = JSON.parse(localStorage.getItem(DEVELOPMENT_ACCOUNTS_KEY) || "[]") as Array<{ email: string; name: string; passwordHash: string }>;
      if (accounts.some((account) => account.email === email)) {
        setMessage("该邮箱已经注册，请直接登录。");
        return;
      }
      accounts.push({ email, name: String(values.get("name") || "").trim(), passwordHash: await passwordDigest(password) });
      localStorage.setItem(DEVELOPMENT_ACCOUNTS_KEY, JSON.stringify(accounts));
      setLoginIdentifier(email);
      setAccountView("login");
      setMessage("注册成功，请使用邮箱和密码登录。");
      return;
    }
    try {
      await accountRequest("/api/accounts/register", {
        email,
        name: String(values.get("name") || "").trim(),
        password,
        accepted_terms: true,
      });
      setLoginIdentifier(email);
      setAccountView("login");
      setMessage("注册成功，请使用邮箱和密码登录。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "注册失败，请稍后重试。");
    }
  }

  function requestReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(import.meta.env.DEV
      ? "这是开发环境，未实际发送邮件；正式环境接入邮件服务后将发送重置链接。"
      : "密码重置服务尚未启用，请联系系统管理员。");
  }

  if (page === "login") {
    return (
      <main className="public-login">
        <section className="login-story" aria-label="产品介绍">
          <button className="brand-button" onClick={() => navigate("home")} type="button"><img src="/assets/brand/call-logo.svg" alt="云声通" width="176" height="42" /></button>
          <div className="login-story-copy"><span>让沟通更自然</span><h1>认真接好每一通电话</h1><p>从客户咨询到营销回访，让团队更从容地服务每一位客户。</p></div>
        </section>
        <section className="login-panel">
          <div className="login-box">
            <img className="login-mobile-logo" src="/assets/brand/call-logo.svg" alt="云声通" width="180" height="43" />
            <button className="login-back" onClick={() => accountView === "login" ? navigate("home") : setAccountView("login")} type="button">← {accountView === "login" ? "返回首页" : "返回登录"}</button>
            <p className="section-kicker">{accountView === "forgot" ? "账户帮助" : accountView === "register" ? "创建账号" : "登录控制台"}</p>
            <h2>{accountView === "forgot" ? "重置您的密码" : accountView === "register" ? "欢迎加入" : "欢迎回来"}</h2>
            <p>{accountView === "forgot" ? "输入注册邮箱，我们会将密码重置链接发送给您。" : accountView === "register" ? "先创建个人账号，登录后即可配置并测试您的智能客服。" : "登录您的工作空间，继续处理今天的重要沟通。"}</p>
            {accountView === "forgot" ? (
              <form className="login-form" key="forgot-form" onSubmit={requestReset}>
                <div className="login-field"><label htmlFor="reset-email">注册邮箱</label><input id="reset-email" name="email" type="email" autoComplete="email" inputMode="email" spellCheck={false} placeholder="例如：name@company.com" required /></div>
                <button className="account-primary" type="submit">发送重置链接</button>
              </form>
            ) : accountView === "register" ? (
              <form className="login-form" key="register-form" onSubmit={register}>
                <div className="login-field"><label htmlFor="register-name">您的称呼</label><input id="register-name" name="name" autoComplete="name" placeholder="请输入姓名" required /></div>
                <div className="login-field"><label htmlFor="register-email">邮箱</label><input id="register-email" name="email" type="email" autoComplete="email" inputMode="email" spellCheck={false} placeholder="例如：name@company.com" required /></div>
                <div className="login-field"><label htmlFor="register-password">设置密码</label><input id="register-password" name="password" type="password" autoComplete="new-password" minLength={8} placeholder="至少 8 位字符" required /></div>
                <div className="login-field"><label htmlFor="register-confirm">确认密码</label><input id="register-confirm" name="confirm_password" type="password" autoComplete="new-password" minLength={8} placeholder="再次输入密码" required /></div>
                <div className="register-consent"><input id="register-terms" name="terms" type="checkbox" required /><label htmlFor="register-terms">我已阅读并同意 <a href="/terms" target="_blank" rel="noreferrer">服务条款</a> 和 <a href="/privacy" target="_blank" rel="noreferrer">隐私政策</a></label></div>
                <button className="account-primary" type="submit">注册账号</button>
              </form>
            ) : (
              <form className="login-form" key="login-form" onSubmit={login}>
                <div className="login-field"><label htmlFor="login-identifier">邮箱或手机号</label><input id="login-identifier" name="identifier" autoComplete="username" spellCheck={false} placeholder="请输入邮箱或手机号" value={loginIdentifier} onChange={(event) => setLoginIdentifier(event.target.value)} required /></div>
                <div className="login-field"><label htmlFor="login-password">密码</label><div className="password-control"><input id="login-password" name="password" type={showPassword ? "text" : "password"} autoComplete="current-password" placeholder="请输入密码" minLength={6} required /><button aria-label={showPassword ? "隐藏密码" : "显示密码"} aria-pressed={showPassword} onClick={() => setShowPassword((value) => !value)} type="button">{showPassword ? "隐藏" : "显示"}</button></div></div>
                <div className="login-options"><label><input name="remember" type="checkbox" /> <span>记住密码</span></label><button onClick={() => { setAccountView("forgot"); setMessage(""); }} type="button">忘记密码？</button></div>
                <button className="account-primary" type="submit">登录</button>
                <div className="register-entry"><span>还没有账号？</span><button onClick={() => { setAccountView("register"); setMessage(""); }} type="button">免费注册</button></div>
              </form>
            )}
            {message ? <div className="account-message" role="status" aria-live="polite">{message}</div> : null}
            <small className="login-note">登录即表示您同意服务条款和隐私政策。</small>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="public-site">
      <header className="public-nav"><img src="/assets/brand/call-logo.svg" alt="云声通" width="190" height="45" /><nav><a href="#capabilities">产品能力</a><a href="#scenes">应用场景</a><a href="#security">安全可靠</a></nav><a className="nav-login" href="/login">登录控制台</a></header>
      <section className="public-hero">
        <img className="hero-photo" src="/assets/brand/natural-call-hero.png" alt="专业客服人员正在与客户通话" width="1672" height="940" fetchPriority="high" />
        <div className="hero-shade" aria-hidden="true" />
        <div className="hero-copy"><span className="section-kicker">智能语音服务平台</span><h1>让每一次沟通，<br />都及时而有温度</h1><p>覆盖客户咨询、营销回访和线索跟进，帮助团队专注于真正重要的客户关系。</p><div><a className="hero-primary" href="/login">进入控制台</a><a href="#capabilities">了解产品 <b aria-hidden="true">→</b></a></div></div>
        <div className="hero-promise" aria-label="服务特色"><span><b>持续响应客户来电</b><small>重要咨询不再等待</small></span><span><b>统一管理呼入与外呼</b><small>任务和进度清晰可见</small></span><span><b>留下完整沟通记录</b><small>每次跟进都有依据</small></span></div>
      </section>
      <section className="public-section" id="capabilities"><div className="section-heading"><span className="section-kicker">产品能力</span><h2>把复杂的电话工作，变得简单清晰</h2><p>从客户主动来电，到团队有序触达，再到沉淀每一次沟通，都在同一个工作空间自然衔接。</p></div><div className="capability-flow"><div className="flow-sources"><article><small>客户主动来电</small><h3>智能接听</h3><p>理解客户问题，自然应答；需要时把沟通背景完整交给人工。</p><strong>随时响应 · 自然转接</strong></article><article><small>团队主动联系</small><h3>高效外呼</h3><p>安排客户名单、联系时间和回访任务，让每一次触达适时、有序。</p><strong>批量任务 · 灵活安排</strong></article></div><div className="flow-lines" aria-hidden="true"><span /><i /><span /></div><article className="flow-outcome"><small>沟通结果统一沉淀</small><h3>通话洞察</h3><p>自动留下关键信息、客户意向和下一步安排，团队无需反复倾听，也不会错过重要跟进。</p><strong>通话摘要 · 意向记录</strong></article></div></section>
      <section className="scene-section" id="scenes"><div className="scene-copy"><span className="section-kicker">应用场景</span><h2>自然交流，服务真实业务</h2><p>一次贴心的预约回访，不应该像机械问答。系统理解客户的时间安排，确认信息，并把结果及时交给服务团队。</p><ul><li>理解客户自然表达</li><li>结合业务信息完成确认</li><li>将重要结果同步给团队</li></ul></div><div className="conversation-case"><header><div className="call-avatar">林</div><div><strong>林女士的预约回访</strong><small><span /> 通话已顺利完成 · 02:18</small></div></header><div className="conversation-body"><div className="dialogue service"><small>服务助手</small><p>林女士您好，您预约了本周的上门服务，想和您确认一下方便的时间。</p></div><div className="dialogue customer"><small>林女士</small><p>周五下午吧，三点以后我都在家。</p></div><div className="dialogue service"><small>服务助手</small><p>好的，为您安排周五下午三点。师傅出发前会再联系您，祝您生活愉快。</p></div></div><footer><span>✓ 预约时间已确认</span><span>✓ 服务人员已收到提醒</span></footer></div></section>
      <section className="trust-section" id="security"><div className="trust-heading"><p>稳定、清晰、值得信赖</p><h2>每一通电话都有记录，每一次操作都有边界。</h2><span>从人员权限到数据保存，关键业务始终清晰、可控。</span></div><div className="trust-list"><article><h3>权限分级</h3><p>按团队和职责开放功能，重要业务只由合适的人处理。</p><strong>职责清晰</strong></article><article><h3>录音管理</h3><p>统一保存和查找通话录音，访问过程有序、可控。</p><strong>访问可控</strong></article><article><h3>操作审计</h3><p>关键修改与操作完整留痕，出现问题时能够及时追溯。</p><strong>完整留痕</strong></article><article><h3>数据留存</h3><p>根据业务要求设置保存期限，妥善管理客户信息。</p><strong>按需管理</strong></article></div></section>
      <footer className="public-footer"><img src="/assets/brand/call-logo.svg" alt="云声通" width="190" height="45" /><p>让客户沟通更从容。</p><a href="/login">登录控制台</a></footer>
    </main>
  );
}
