type LegalDocumentProps = { kind: "terms" | "privacy" };

export default function LegalDocument({ kind }: LegalDocumentProps) {
  const terms = kind === "terms";
  return (
    <main className="legal-page">
      <header><a href="/"><img src="/assets/brand/call-logo.svg" alt="云声通" /></a><a href="/login">返回登录</a></header>
      <article>
        <span>云声通账户与服务</span>
        <h1>{terms ? "服务条款" : "隐私政策"}</h1>
        <p className="legal-updated">最近更新：2026年7月30日</p>
        {terms ? <>
          <section><h2>1. 服务说明</h2><p>云声通提供智能客服、文本与电话交互、智能外呼及相关管理能力。具体可用功能以您的账户权限和工作空间配置为准。</p></section>
          <section><h2>2. 账户责任</h2><p>您应提供真实、准确的注册信息，妥善保管账户凭据，并对账户内发生的操作负责。发现未经授权的使用时，请及时联系平台管理员。</p></section>
          <section><h2>3. 合理使用</h2><p>不得利用服务实施骚扰、欺诈、侵犯他人权益或违反适用法律法规的活动。电话外呼能力仅向完成相应审核和授权的企业账户开放。</p></section>
          <section><h2>4. 服务变更</h2><p>我们可能因安全、合规或产品迭代调整服务能力，并在对用户权益有重大影响时提供合理通知。</p></section>
        </> : <>
          <section><h2>1. 收集的信息</h2><p>为创建账户和提供服务，我们可能处理您的邮箱、姓名、登录记录，以及您主动提交的Agent配置和业务数据。</p></section>
          <section><h2>2. 使用目的</h2><p>相关信息用于身份验证、提供智能客服和外呼服务、保障系统安全、处理故障以及履行适用的合规义务。</p></section>
          <section><h2>3. 数据保护</h2><p>我们通过权限控制、传输加密、操作审计和保留期限等措施保护数据。请勿在公开体验中提交密码、验证码或其他敏感信息。</p></section>
          <section><h2>4. 您的权利</h2><p>您可以根据适用规则申请访问、更正或删除账户信息。企业工作空间中的业务数据由相应企业管理员按照权限和保留策略管理。</p></section>
        </>}
        <aside>当前页面为产品内基础文本，正式商用前应由法务结合实际运营主体、地区和数据处理方式完成审定。</aside>
      </article>
    </main>
  );
}
