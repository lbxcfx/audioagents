import { useEffect, useMemo, useState } from "react";
import { inboundRequest, type InboundAgentSummary } from "./inboundApi";
import { loadPlatformAuth, platformAuthHeaders, type PlatformAuthSession } from "./platformAuth";

type Project = { id: string; name: string };
type View = "studio" | "avatars" | "library" | "preview";
type Avatar = { id: string; name: string; style: string; provider: string; mode: "realtime" | "rendered"; accent: string };
type Asset = { id: string; name: string; kind: string; description: string; duration?: string; accent?: string; source_url?: string; status?: string };
type Presets = { avatars: Avatar[]; content: Asset[]; tutorial: { title: string; script: string[] } };

const builtInTutorial = {
  title: "两分钟学会视频客服",
  script: [
    "您好，我是云声通数字人向导。视频客服可以在通话中向客户展示安装视频、产品资料，并随时回答客户的问题。",
    "第一步，选择适合品牌形象的数字人讲解员。实时互动形象允许客户随时打断，预生成口播适合固定的产品介绍。",
    "第二步，从内容库加入经过企业审核的视频、图片或步骤卡片。只有已发布的素材才会展示给客户。",
    "最后，在测试与发布中检查知识库、业务工具、数字人和降级策略。通过隔离预演后，就可以开始第一场视频服务。",
  ],
};

const platformBase = import.meta.env.VITE_PLATFORM_API_BASE || (import.meta.env.DEV ? "http://127.0.0.1:8091" : window.location.origin);
async function platform<T>(path: string, auth: PlatformAuthSession) { const response = await fetch(`${platformBase}${path}`, { headers: platformAuthHeaders(auth) }); if (!response.ok) throw new Error("项目读取失败"); return response.json() as Promise<T>; }

const viewCopy: Record<View, { label: string; title: string; description: string }> = {
  studio: { label: "服务制作", title: "产品安装指导", description: "把数字人讲解、产品视频和客户问答编成一次完整服务。" },
  avatars: { label: "数字人", title: "选择出镜讲解员", description: "实时数字人支持客户打断；口播数字人适合固定介绍。" },
  library: { label: "内容库", title: "客户会看到的内容", description: "使用预置示例快速开始，也可以加入企业审核过的产品素材。" },
  preview: { label: "测试与发布", title: "上线前完整体验一次", description: "检查数字人、素材、知识问答和语音降级是否衔接自然。" },
};

const viewHashes: Record<View, string> = {
  studio: "#service-studio",
  avatars: "#service-avatar",
  library: "#service-assets",
  preview: "#service-preview",
};
const hashViews = Object.fromEntries(Object.entries(viewHashes).map(([view, hash]) => [hash, view])) as Record<string, View>;

function viewFromLocation(): View {
  return hashViews[window.location.hash] || "studio";
}

export default function VideoServiceConsole() {
  const auth = useMemo(() => loadPlatformAuth(), []);
  const [projects, setProjects] = useState<Project[]>([]), [projectId, setProjectId] = useState("");
  const [view, setView] = useState<View>(viewFromLocation), [notice, setNotice] = useState("");
  const [agents, setAgents] = useState<InboundAgentSummary[]>([]), [assets, setAssets] = useState<Asset[]>([]);
  const [presets, setPresets] = useState<Presets>({ avatars: [], content: [], tutorial: builtInTutorial });
  const [avatarId, setAvatarId] = useState(""), [assetId, setAssetId] = useState("");
  const [guideOpen, setGuideOpen] = useState(false), [guideStep, setGuideStep] = useState(0), [speaking, setSpeaking] = useState(false), [guidePlaying, setGuidePlaying] = useState(false);
  const allAssets = [...presets.content, ...assets];
  const avatar = presets.avatars.find((item) => item.id === avatarId) || presets.avatars[0];
  const asset = allAssets.find((item) => item.id === assetId) || allAssets[0];

  useEffect(() => {
    const syncView = () => setView(viewFromLocation());
    window.addEventListener("hashchange", syncView);
    return () => window.removeEventListener("hashchange", syncView);
  }, []);

  function selectView(nextView: View) {
    setView(nextView);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${viewHashes[nextView]}`);
  }

  useEffect(() => { if (auth) platform<{ items: Project[] }>("/api/platform/projects", auth).then(({ items }) => { setProjects(items); setProjectId(items[0]?.id || ""); if (!items.length) setNotice("当前账号还没有可用项目，请先在项目与成员中创建项目。"); }).catch(() => setNotice("项目列表暂时无法读取，请检查控制台服务后重试。")); }, [auth]);
  useEffect(() => {
    if (!auth || !projectId) return;
    setNotice("");
    Promise.allSettled([
      inboundRequest<{ items: InboundAgentSummary[] }>(`/inbound-api/projects/${projectId}/agents`, {}, auth),
      inboundRequest<{ items: Asset[] }>(`/inbound-api/projects/${projectId}/content-assets`, {}, auth),
      inboundRequest<Presets>(`/inbound-api/projects/${projectId}/video-service/presets`, {}, auth),
    ]).then(([agentResult, assetResult, presetResult]) => {
      if (agentResult.status === "fulfilled") setAgents(agentResult.value.items.filter((item) => item.status === "published"));
      if (assetResult.status === "fulfilled") setAssets(assetResult.value.items.filter((item) => item.status === "published"));
      if (presetResult.status === "fulfilled") {
        setPresets({ ...presetResult.value, tutorial: presetResult.value.tutorial?.script?.length ? presetResult.value.tutorial : builtInTutorial });
        setAvatarId(presetResult.value.avatars[0]?.id || "");
        setAssetId(presetResult.value.content[0]?.id || "");
      }
      const failed = [agentResult, assetResult, presetResult].filter((result) => result.status === "rejected").length;
      if (failed) setNotice(`${failed} 项配置暂时无法读取，其余功能仍可继续使用。`);
    });
  }, [auth, projectId]);

  function playGuide(index = 0) {
    setGuideOpen(true); setGuidePlaying(true); setGuideStep(index);
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(presets.tutorial.script[index]);
    utterance.lang = "zh-CN"; utterance.rate = 0.95;
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => {
      setSpeaking(false);
      if (index < presets.tutorial.script.length - 1) playGuide(index + 1);
      else setGuidePlaying(false);
    };
    window.speechSynthesis.speak(utterance);
  }
  function closeGuide() { window.speechSynthesis?.cancel(); setSpeaking(false); setGuidePlaying(false); setGuideOpen(false); }

  if (!auth) return <main className="inbound-auth-required"><h1>请先登录控制台</h1><a href="/login">前往登录</a></main>;
  const copy = viewCopy[view];
  return <main className="video-console-shell">
    <aside className="video-admin-nav">
      <a className="video-admin-brand" href="/app/home"><img src="/assets/brand/call-logo.svg" alt="云声通" /></a>
      <div className="video-nav-product"><span>VIDEO SERVICE</span><strong>视频客服</strong></div>
      <nav aria-label="视频客服功能">
        {(["studio", "avatars", "library", "preview"] as View[]).map((item, index) => <button className={view === item ? "active" : ""} key={item} onClick={() => selectView(item)}><i>{index + 1}</i><span>{viewCopy[item].label}</span></button>)}
      </nav>
      <div className="video-nav-help"><strong>第一次使用？</strong><p>由数字人带你完成第一场视频服务。</p><button onClick={() => playGuide(0)}>▶ 播放数字人介绍</button></div>
      <a className="video-back-home" href="/app/home">← 返回工作台</a>
    </aside>
    <section className="video-admin-main">
      <header className="video-admin-topbar"><div><span>{copy.label}</span><strong>{copy.title}</strong><p>{copy.description}</p></div><select aria-label="选择项目" value={projectId} onChange={(event) => setProjectId(event.target.value)}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></header>
      {notice ? <div className="video-admin-notice" role="status">{notice}<button onClick={() => setNotice("")}>×</button></div> : null}

      {view === "studio" ? <div className="video-studio-view">
        <section className="video-live-stage">
          <header><span><i /> 工作画面</span><button onClick={() => playGuide(0)}>观看数字人介绍</button></header>
          <div className="video-live-canvas">
            <div className="video-avatar-shot" style={{ "--avatar-accent": avatar?.accent || "#30445b" } as React.CSSProperties}><div className="video-avatar-person"><i /></div><span>{avatar?.name || "数字人讲解员"}</span><p>您好，接下来我会播放安装视频，您可以随时提问。</p></div>
            <div className="video-material-shot" style={{ "--asset-accent": asset?.accent || "#27384d" } as React.CSSProperties}><span>{asset?.kind === "video" ? "PRODUCT VIDEO" : "GUIDE"}</span><strong>{asset?.name || "选择讲解内容"}</strong><p>{asset?.description || "从内容库选择客户需要看到的内容。"}</p><button>▶ 播放示例</button></div>
          </div>
          <footer><span>客户视角预览</span><div><button>上一个环节</button><button className="primary">开始讲解</button><button>下一个环节</button></div></footer>
        </section>
        <aside className="video-program-panel"><header><span>本场服务</span><button>＋ 添加环节</button></header><ol><li className="current"><i>01</i><div><strong>数字人欢迎</strong><small>{avatar?.name || "未选择"} · 20 秒</small></div></li><li><i>02</i><div><strong>播放产品内容</strong><small>{asset?.name || "未选择"}</small></div></li><li><i>03</i><div><strong>客户语音问答</strong><small>知识库 + 受控 MCP</small></div></li><li><i>04</i><div><strong>结果确认</strong><small>安装检查卡片</small></div></li></ol><button className="video-next-action" onClick={() => selectView("preview")}>检查并预演 →</button></aside>
      </div> : null}

      {view === "avatars" ? <div className="video-resource-view"><div className="video-view-lead"><span>{presets.avatars.length} 个可用形象</span><strong>谁来向客户讲解？</strong><p>预置形象由后端目录提供。选择后会用于本场服务，不需要先填写 Avatar ID。</p></div><div className="video-avatar-grid">{presets.avatars.map((item) => <button className={avatar?.id === item.id ? "active" : ""} onClick={() => setAvatarId(item.id)} key={item.id}><div className="preset-avatar" style={{ "--avatar-accent": item.accent } as React.CSSProperties}><i /></div><span>{item.mode === "realtime" ? "实时互动" : "预生成口播"}</span><strong>{item.name}</strong><small>{item.style}</small><b>{avatar?.id === item.id ? "已选择" : "选择形象"}</b></button>)}</div></div> : null}

      {view === "library" ? <div className="video-resource-view"><div className="video-view-lead"><span>{allAssets.length} 项内容</span><strong>客户会看到什么？</strong><p>预置内容用于快速理解和演示；企业内容仍需经过审核后才能正式展示。</p><a href="/app/inbound/content">管理企业审核素材 →</a></div><div className="video-content-grid">{allAssets.map((item) => <button className={asset?.id === item.id ? "active" : ""} onClick={() => setAssetId(item.id)} key={item.id}><div style={{ "--asset-accent": item.accent || "#526071" } as React.CSSProperties}><span>{item.kind === "video" ? "▶" : "DOC"}</span><b>{item.duration || "企业"}</b></div><strong>{item.name}</strong><small>{item.description}</small><em>{asset?.id === item.id ? "已加入本场服务" : "加入本场服务"}</em></button>)}</div></div> : null}

      {view === "preview" ? <div className="video-preview-view"><section><span>READY CHECK</span><h2>发布前检查</h2><ul><li className={agents.length ? "ready" : ""}><i>{agents.length ? "✓" : "!"}</i><div><strong>语音问答 Agent</strong><small>{agents[0]?.name || "尚未发布企业 Agent"}</small></div></li><li className={avatar ? "ready" : ""}><i>{avatar ? "✓" : "!"}</i><div><strong>数字人形象</strong><small>{avatar?.name || "尚未选择"}</small></div></li><li className={asset ? "ready" : ""}><i>{asset ? "✓" : "!"}</i><div><strong>讲解内容</strong><small>{asset?.name || "尚未选择"}</small></div></li><li className="ready"><i>✓</i><div><strong>失败降级</strong><small>数字人或视频失败时继续纯语音</small></div></li></ul></section><aside><span>客户将看到</span><strong>{avatar?.name || "数字人"} 为客户讲解 {asset?.name || "产品内容"}</strong><p>客户可以在播放过程中随时开口，Qwen Audio Realtime 会结合企业知识回答，并只调用 Agent 白名单中的业务工具。</p><a href="/app/inbound/evaluation">创建隔离会话并开始预演</a></aside></div> : null}
    </section>

    {guideOpen ? <div className="video-guide-overlay" role="dialog" aria-modal="true" aria-labelledby="guide-title"><button className="video-guide-close" onClick={closeGuide} aria-label="关闭介绍">×</button><div className={`video-guide-avatar ${speaking ? "speaking" : ""}`}><img src="/assets/video-service/onboarding-presenter.png" alt="数字人使用向导" /><span><i /> 数字人使用向导</span></div><section><span>视频导览 · {guideStep + 1} / {presets.tutorial.script.length}</span><h2 id="guide-title">{presets.tutorial.title}</h2><p>{presets.tutorial.script[guideStep]}</p><div className="video-guide-progress">{presets.tutorial.script.map((_, index) => <i className={index <= guideStep ? "active" : ""} key={index} />)}</div><footer><button onClick={() => guidePlaying ? closeGuide() : playGuide(guideStep)}>{guidePlaying ? "■ 停止播放" : "▶ 从这里播放"}</button><button disabled={guideStep === 0} onClick={() => { window.speechSynthesis?.cancel(); setGuidePlaying(false); setGuideStep((value) => value - 1); }}>上一步</button>{guideStep < presets.tutorial.script.length - 1 ? <button className="primary" onClick={() => { window.speechSynthesis?.cancel(); setGuidePlaying(false); setGuideStep((value) => value + 1); }}>下一步</button> : <button className="primary" onClick={closeGuide}>开始配置</button>}</footer></section></div> : null}
  </main>;
}
