import { FormEvent, useEffect, useMemo, useState } from "react";
import { inboundRequest } from "./inboundApi";
import { loadPlatformAuth, platformAuthHeaders, type PlatformAuthSession } from "./platformAuth";

type Project = { id: string; name: string; role: string };
type KnowledgeBase = { id: string; name: string; description: string; document_count: number; updated_at: string };
type Document = { id: string; filename: string; media_type: string; status: string; chunk_count: number; updated_at: string };
type SearchResult = { chunk_id: string; filename: string; heading: string; content: string; score: number };

const platformBase = import.meta.env.VITE_PLATFORM_API_BASE
  || (import.meta.env.DEV ? "http://127.0.0.1:8091" : window.location.origin);

async function platformRequest<T>(path: string, auth: PlatformAuthSession): Promise<T> {
  const response = await fetch(`${platformBase}${path}`, { headers: platformAuthHeaders(auth) });
  if (!response.ok) throw new Error(`项目服务请求失败（${response.status}）`);
  return response.json() as Promise<T>;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const step = 0x8000;
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode(...bytes.subarray(index, index + step));
  }
  return btoa(binary);
}

function mediaTypeFor(file: File): string {
  const suffix = file.name.toLowerCase().split(".").pop();
  if (suffix === "txt") return "text/plain";
  if (suffix === "md") return "text/markdown";
  if (suffix === "pdf") return "application/pdf";
  if (suffix === "docx") return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  return "";
}

export default function KnowledgeConsole() {
  const auth = useMemo(() => loadPlatformAuth(), []);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const activeProject = projects.find((item) => item.id === projectId);
  const canEdit = ["owner", "admin", "member"].includes(activeProject?.role || "");

  useEffect(() => {
    if (!auth) return;
    platformRequest<{ items: Project[] }>("/api/platform/projects", auth)
      .then(({ items }) => { setProjects(items); setProjectId(items[0]?.id || ""); })
      .catch((error) => setMessage(error instanceof Error ? error.message : "无法读取项目"));
  }, [auth]);

  useEffect(() => { if (auth && projectId) void loadBases(); }, [projectId]);
  useEffect(() => { if (auth && selectedId) void loadDocuments(selectedId); else setDocuments([]); }, [selectedId]);

  async function loadBases() {
    if (!auth || !projectId) return;
    try {
      const value = await inboundRequest<{ items: KnowledgeBase[] }>(`/inbound-api/projects/${projectId}/knowledge-bases`, {}, auth);
      setBases(value.items);
      setSelectedId((current) => value.items.some((item) => item.id === current) ? current : value.items[0]?.id || "");
    } catch (error) { setMessage(error instanceof Error ? error.message : "知识库加载失败"); }
  }

  async function loadDocuments(baseId: string) {
    if (!auth) return;
    try {
      const value = await inboundRequest<{ items: Document[] }>(`/inbound-api/projects/${projectId}/knowledge-bases/${baseId}/documents`, {}, auth);
      setDocuments(value.items);
    } catch (error) { setMessage(error instanceof Error ? error.message : "文档加载失败"); }
  }

  async function createBase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !canEdit) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const created = await inboundRequest<KnowledgeBase>(`/inbound-api/projects/${projectId}/knowledge-bases`, { method: "POST", body: JSON.stringify({ name: form.get("name"), description: form.get("description") }) }, auth);
      event.currentTarget.reset();
      await loadBases();
      setSelectedId(created.id);
      setMessage("知识库已创建。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "创建失败"); }
    finally { setBusy(false); }
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !selectedId || !canEdit) return;
    const input = event.currentTarget.elements.namedItem("document") as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const mediaType = mediaTypeFor(file);
    if (!mediaType) { setMessage("仅支持 TXT、Markdown、PDF 和 DOCX。"); return; }
    if (file.size > 20_000_000) { setMessage("文件不能超过 20 MB。"); return; }
    setBusy(true);
    try {
      const contentBase64 = bytesToBase64(new Uint8Array(await file.arrayBuffer()));
      const job = await inboundRequest<{ id: string }>(`/inbound-api/projects/${projectId}/knowledge-bases/${selectedId}/documents`, { method: "POST", body: JSON.stringify({ filename: file.name, media_type: mediaType, content_base64: contentBase64 }) }, auth);
      event.currentTarget.reset();
      setMessage("文档已进入解析队列…");
      for (let attempt = 0; attempt < 90; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const status = await inboundRequest<{ status: string; progress: number; error_message: string }>(`/inbound-api/projects/${projectId}/knowledge-jobs/${job.id}`, {}, auth);
        setMessage(`文档处理中：${status.progress}%`);
        if (status.status === "completed") break;
        if (["failed", "dead"].includes(status.status)) throw new Error(status.error_message || "文档处理失败");
        if (attempt === 89) throw new Error("文档仍在处理中，请稍后刷新查看");
      }
      await Promise.all([loadDocuments(selectedId), loadBases()]);
      setMessage("文档解析和索引已完成。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "上传失败"); }
    finally { setBusy(false); }
  }

  async function removeDocument(documentId: string) {
    if (!auth || !selectedId || !canEdit || !window.confirm("确认删除文档及其全部索引分块？")) return;
    setBusy(true);
    try {
      await inboundRequest(`/inbound-api/projects/${projectId}/knowledge-bases/${selectedId}/documents/${documentId}`, { method: "DELETE" }, auth);
      await Promise.all([loadDocuments(selectedId), loadBases()]);
      setMessage("文档已删除。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "删除失败"); }
    finally { setBusy(false); }
  }

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !selectedId) return;
    const query = String(new FormData(event.currentTarget).get("query") || "");
    setBusy(true);
    try {
      const value = await inboundRequest<{ items: SearchResult[] }>(`/inbound-api/projects/${projectId}/knowledge/search`, { method: "POST", body: JSON.stringify({ knowledge_base_ids: [selectedId], query, limit: 8 }) }, auth);
      setResults(value.items);
    } catch (error) { setMessage(error instanceof Error ? error.message : "检索失败"); }
    finally { setBusy(false); }
  }

  if (!auth) return <main className="inbound-auth-required"><h1>请先登录控制台</h1><a href="/login">前往登录</a></main>;
  return <main className="inbound-console">
    <aside className="inbound-sidebar"><a href="/app/home" className="inbound-brand"><img src="/assets/brand/call-logo.svg" alt="云声通" /></a><span className="sidebar-group">智能客服</span><a href="/app/inbound/agents">Agent 配置</a><a className="active" href="/app/inbound/knowledge">知识库</a><a href="/app/inbound/integrations">业务系统</a><a href="/app/inbound/evaluation">体验与评测</a><a href="/app/inbound/content">展示素材</a><span className="sidebar-group">设置</span><a href="/app/dashboard">项目与成员</a></aside>
    <section className="inbound-workspace"><header className="inbound-topbar"><div><span>智能客服</span><strong>企业知识库</strong></div><select aria-label="选择项目" value={projectId} onChange={(event) => { setProjectId(event.target.value); setSelectedId(""); setResults([]); }}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></header>
      {message ? <div className="inbound-notice" role="status">{message}<button onClick={() => setMessage("")} type="button">×</button></div> : null}
      <div className="knowledge-page"><div className="inbound-page-heading"><div><span>GROUNDED ANSWERS</span><h1>让每个回答都有企业依据</h1><p>上传资料、验证检索结果，再将知识库绑定到 Agent。</p></div></div>
        <div className="knowledge-grid"><aside className="knowledge-bases"><h2>知识库</h2>{bases.map((base) => <button className={selectedId === base.id ? "active" : ""} key={base.id} onClick={() => { setSelectedId(base.id); setResults([]); }} type="button"><strong>{base.name}</strong><small>{base.document_count} 份文档</small></button>)}<form onSubmit={createBase}><input name="name" placeholder="新知识库名称" required disabled={!canEdit} /><textarea name="description" placeholder="用途说明" rows={2} disabled={!canEdit} /><button disabled={busy || !canEdit} type="submit">创建知识库</button></form></aside>
          <section className="knowledge-main">{selectedId ? <><form className="knowledge-upload" onSubmit={upload}><div><h2>文档</h2><p>支持 UTF-8 TXT、Markdown、PDF、DOCX，单文件最大 20 MB。</p></div><input aria-label="选择知识文档" name="document" type="file" accept=".txt,.md,.pdf,.docx" required disabled={!canEdit} /><button disabled={busy || !canEdit} type="submit">{busy ? "处理中…" : "上传并索引"}</button></form><div className="knowledge-documents">{documents.map((document) => <article key={document.id}><div><strong>{document.filename}</strong><small>{document.chunk_count} 个分块 · {document.status === "ready" ? "可检索" : document.status}</small></div><button disabled={!canEdit || busy} onClick={() => removeDocument(document.id)} type="button">删除</button></article>)}{!documents.length ? <p className="empty-copy">尚未上传文档。</p> : null}</div><form className="knowledge-search" onSubmit={search}><h2>检索测试</h2><div><input name="query" placeholder="输入客户可能提出的问题" required /><button disabled={busy || !documents.length} type="submit">检索</button></div></form><div className="knowledge-results">{results.map((result) => <article key={result.chunk_id}><header><strong>{result.filename}</strong><span>相关度 {result.score.toFixed(3)}</span></header><small>{result.heading || "正文"}</small><p>{result.content}</p></article>)}{results.length === 0 ? <p className="empty-copy">输入问题后，这里会展示命中的分块和引用来源。</p> : null}</div></> : <p className="empty-copy">先创建一个知识库。</p>}</section></div>
      </div></section>
  </main>;
}
