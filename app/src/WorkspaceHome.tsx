import { loadPlatformAuth, platformAuthSubject, savePlatformAuth } from "./platformAuth";

export default function WorkspaceHome() {
  const auth = loadPlatformAuth();
  if (!auth) {
    return <main className="workspace-auth-required"><img src="/assets/brand/call-logo.svg" alt="云声通" /><h1>请先登录</h1><p>登录后即可进入您的工作空间。</p><a href="/login">前往登录</a></main>;
  }

  function logout() {
    savePlatformAuth(null);
    window.location.assign("/login");
  }

  return (
    <main className="workspace-home">
      <header className="workspace-header">
        <a href="/app/home"><img src="/assets/brand/call-logo.svg" alt="云声通" /></a>
        <div><span>{platformAuthSubject(auth)}</span><button onClick={logout} type="button">退出登录</button></div>
      </header>
      <section className="workspace-intro">
        <span>工作空间</span>
        <h1>今天想从哪里开始？</h1>
        <p>客户主动咨询与团队主动触达，分开管理，也在同一个工作空间里自然协作。</p>
      </section>
      <section className="workspace-products" aria-label="产品功能">
        <a className="workspace-product service-product" href="/app/inbound/agents">
          <div className="workspace-product-channel"><i /> 客户来电</div>
          <div className="workspace-product-copy">
            <span>智能客服</span>
            <h2>让 Agent 理解业务，认真回答每一次咨询</h2>
            <p>统一配置服务角色、企业知识和回复边界，通过文本先测试效果，再连接网页语音或企业电话。</p>
            <ul><li>Agent 配置</li><li>知识库</li><li>文本测试</li><li>电话接听</li></ul>
          </div>
          <div className="workspace-product-scene" aria-hidden="true"><div><i>客户</i><p>我想确认一下明天下午的预约。</p></div><div><i>客服 Agent</i><p>好的，我先为您核对预约信息。</p></div></div>
          <span className="workspace-product-link">进入智能客服 <b>→</b></span>
        </a>
        <a className="workspace-product outbound-product" href="/app/dashboard">
          <div className="workspace-product-channel"><i /> 主动触达</div>
          <div className="workspace-product-copy">
            <span>智能外呼</span>
            <h2>把批量联系，变成清晰可控的业务流程</h2>
            <p>管理外呼场景、客户名单、线路与任务进度，完整记录每次触达结果和后续安排。</p>
            <ul><li>外呼场景</li><li>客户名单</li><li>任务调度</li><li>通话结果</li></ul>
          </div>
          <div className="workspace-product-scene outbound-scene" aria-hidden="true"><span>今日任务</span><strong>128</strong><div><i /><i /><i /><i /><i /></div><small>任务按计划有序进行</small></div>
          <span className="workspace-product-link">进入智能外呼 <b>→</b></span>
        </a>
      </section>
      <footer className="workspace-footer"><span>个人账号默认使用智能客服体验能力</span><span>企业管理员可在工作空间中邀请成员并分配权限</span></footer>
    </main>
  );
}
