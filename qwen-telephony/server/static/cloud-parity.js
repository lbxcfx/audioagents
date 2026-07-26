const state = {
  userId: localStorage.getItem("cp.userId") || "local-admin",
  projectId: localStorage.getItem("cp.projectId") || "",
  projects: [],
  currentSpec: null,
  selectedSession: null,
  latestVersionId: "",
  telephonyPolicy: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const formatTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }).format(new Date(value)) : "—";

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (!options.public) {
    Object.assign(headers, authHeaders());
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function authHeaders() {
  const accessToken = localStorage.getItem("cp.accessToken") || "";
  return accessToken
    ? { Authorization: `Bearer ${accessToken}` }
    : { "X-User-ID": state.userId };
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = "toast"; }, 2800);
}

function requireProject() {
  if (!state.projectId) throw new Error("请先选择或创建项目");
  return state.projectId;
}

function statusBadge(value) {
  return `<span class="status ${escapeHtml(value)}">${escapeHtml(value || "unknown")}</span>`;
}

async function loadPlatform() {
  try {
    const health = await api("/api/platform/health");
    $("#platformDot").className = "status-dot online";
    $("#platformStatus").textContent = "Control plane online";
    $("#schemaVersion").textContent = `Schema v${health.schema_version}`;
  } catch (error) {
    $("#platformDot").className = "status-dot offline";
    $("#platformStatus").textContent = "Control plane offline";
    toast(error.message, true);
  }
}

async function loadProjects() {
  state.userId = $("#userId").value.trim() || "local-admin";
  localStorage.setItem("cp.userId", state.userId);
  const result = await api("/api/platform/projects");
  state.projects = result.items;
  if (!state.projects.some((item) => item.id === state.projectId)) state.projectId = state.projects[0]?.id || "";
  const select = $("#projectSelect");
  select.innerHTML = `<option value="">选择项目</option>${state.projects.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.role)}</option>`).join("")}`;
  select.value = state.projectId;
  localStorage.setItem("cp.projectId", state.projectId);
  $("#emptyProject").hidden = Boolean(state.projectId);
  $$(".cp-view").forEach((view) => { view.toggleAttribute("hidden", !state.projectId); });
  if (state.projectId) await loadCurrentView();
}

async function loadOverview() {
  const project = requireProject();
  const [summary, sessions] = await Promise.all([
    api(`/api/platform/projects/${project}/analytics/summary`),
    api(`/api/platform/projects/${project}/analytics/sessions?limit=6`),
  ]);
  const cost = summary.usage.reduce((total, item) => total + Number(item.cost_usd || 0), 0);
  $("#metricSessions").textContent = summary.sessions.total;
  $("#metricActive").textContent = summary.sessions.active;
  $("#metricInference").textContent = `${(summary.inference.success_rate * 100).toFixed(1)}%`;
  $("#metricCost").textContent = `$${cost.toFixed(4)}`;
  $("#recentSessionRows").innerHTML = sessions.items.length ? sessions.items.map((item) => `<tr><td><strong>${escapeHtml(item.room_name)}</strong></td><td>${escapeHtml(item.agent_name || "—")}</td><td>${statusBadge(item.status)}</td><td>${item.event_count}</td><td>${formatTime(item.started_at)}</td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">暂无 Session 数据</td></tr>`;
}

async function loadInsights() {
  const project = requireProject();
  const result = await api(`/api/platform/projects/${project}/analytics/sessions?limit=100`);
  $("#sessionRows").innerHTML = result.items.length ? result.items.map((item) => `<tr><td><strong>${escapeHtml(item.room_name)}</strong><br><small>${escapeHtml(item.agent_name || "No agent")}</small></td><td>${statusBadge(item.status)}</td><td>$${Number(item.cost_usd).toFixed(4)}</td><td>${item.event_count}</td><td><button class="text-button" data-session="${item.id}">查看 →</button></td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">暂无 Session</td></tr>`;
  $$('[data-session]').forEach((button) => button.addEventListener("click", () => selectSession(button.dataset.session)));
}

async function selectSession(sessionId) {
  const project = requireProject();
  const timeline = await api(`/api/platform/projects/${project}/sessions/${sessionId}`);
  state.selectedSession = sessionId;
  $("#timelineTitle").textContent = timeline.session.room_name;
  $("#timelineSummary").textContent = `${timeline.summary.event_count} 个事件 · ${timeline.summary.usage_count} 条用量 · $${Number(timeline.summary.cost_usd).toFixed(4)}`;
  $("#observerBtn").disabled = false;
  $("#timelineEvents").innerHTML = timeline.events.length ? timeline.events.map((event) => `<li><strong>${escapeHtml(event.event_type)}</strong><small>#${event.sequence} · ${escapeHtml(event.source)} · ${formatTime(event.occurred_at)}</small><pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre></li>`).join("") : `<li><strong>Session 已建立</strong><small>尚无 Agent 事件</small></li>`;
}

async function loadBuilder() {
  const result = await api(`/api/platform/projects/${requireProject()}/agent-specs`);
  $("#agentSpecList").innerHTML = result.items.length ? result.items.map((item) => `<div class="resource-item"><span><strong>${escapeHtml(item.name)}</strong><small>Revision ${item.revision} · ${escapeHtml(item.status)}</small></span><button data-spec="${item.id}">编辑</button></div>`).join("") : `<p class="muted">暂无 AgentSpec</p>`;
  $$('[data-spec]').forEach((button) => button.addEventListener("click", () => editSpec(button.dataset.spec)));
}

async function editSpec(specId) {
  const record = await api(`/api/platform/projects/${requireProject()}/agent-specs/${specId}`);
  state.currentSpec = record;
  const form = $("#builderForm");
  form.elements.name.value = record.spec.name;
  form.elements.instructions.value = record.spec.instructions;
  form.elements.welcome.value = record.spec.welcome_greeting;
  form.elements.stt.value = record.spec.models.stt;
  form.elements.llm.value = record.spec.models.llm;
  form.elements.tts.value = record.spec.models.tts;
  form.elements.tools.value = JSON.stringify(record.spec.tools, null, 2);
  $("#builderRevision").textContent = `${record.status} · Revision ${record.revision}`;
  $("#publishSpecBtn").disabled = false;
  $("#exportSpecBtn").disabled = false;
}

function builderPayload(form) {
  let tools;
  try { tools = JSON.parse(form.elements.tools.value || "[]"); } catch { throw new Error("工具配置不是合法 JSON"); }
  return {
    spec: {
      schema_version: "1.0",
      name: form.elements.name.value.trim(),
      instructions: form.elements.instructions.value,
      welcome_greeting: form.elements.welcome.value,
      models: { stt: form.elements.stt.value.trim(), llm: form.elements.llm.value.trim(), tts: form.elements.tts.value.trim() },
      conversation: { mode: "open", fields: [] }, tools, metadata_schema: {},
      end_call: { final_response: "", delete_room: false, summary_enabled: true, summary_instructions: "", result_endpoint: null, headers: {} },
    },
    expected_revision: state.currentSpec?.revision || null,
  };
}

async function loadDeployments() {
  const project = requireProject();
  const [agents, deployments, secrets] = await Promise.all([
    api(`/api/platform/projects/${project}/agents`),
    api(`/api/platform/projects/${project}/deployments`),
    api(`/api/platform/projects/${project}/secrets`),
  ]);
  const options = `<option value="">选择 Agent</option>${agents.items.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${item.version_count} versions</option>`).join("")}`;
  $("#buildAgentSelect").innerHTML = options; $("#deployAgentSelect").innerHTML = options;
  $("#deploymentRows").innerHTML = deployments.items.length ? deployments.items.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.agent_name)}</td><td>${statusBadge(item.status)}</td><td>${item.desired_replicas}</td><td><code>${escapeHtml((item.active_version_id || "—").slice(0, 12))}</code></td><td><button class="text-button" data-rollback="${item.id}">回滚</button></td></tr>`).join("") : `<tr><td colspan="6" class="empty-row">暂无部署</td></tr>`;
  $("#secretList").innerHTML = secrets.items.map((item) => `<span class="chip">${escapeHtml(item.name)} · ${escapeHtml(item.value_sha256.slice(0, 8))}</span>`).join("") || `<span class="muted">暂无 Secret</span>`;
  $$('[data-rollback]').forEach((button) => button.addEventListener("click", async () => { try { await api(`/api/platform/projects/${project}/deployments/${button.dataset.rollback}/rollback`, { method: "POST", body: "{}" }); toast("回滚已提交"); await loadDeployments(); } catch (error) { toast(error.message, true); } }));
}

async function loadModels() {
  const result = await api(`/api/platform/projects/${requireProject()}/inference/routes`);
  $("#routeRows").innerHTML = result.items.length ? result.items.map((item) => `<tr><td><strong>${escapeHtml(item.descriptor)}</strong></td><td>${escapeHtml(item.modality)}</td><td>${escapeHtml(item.provider)}</td><td>${escapeHtml(item.provider_model)}</td><td>${item.priority}</td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">暂无模型路由</td></tr>`;
}

async function loadEmbed() {
  const result = await api(`/api/platform/projects/${requireProject()}/embed-configs`);
  $("#embedList").innerHTML = result.items.length ? result.items.map((item) => `<div class="resource-item"><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.agent_name)} · ${item.allowed_origins.length} origins · ${item.enabled ? "enabled" : "disabled"}</small><code>&lt;script src="${location.origin}/static/cloud-parity-widget.js"&gt;&lt;/script&gt;<br>&lt;livekit-agent-widget config-id="${item.id}"&gt;&lt;/livekit-agent-widget&gt;</code></span><button data-copy="${item.id}">复制 ID</button></div>`).join("") : `<p class="muted">暂无 Widget</p>`;
  $$('[data-copy]').forEach((button) => button.addEventListener("click", async () => { await navigator.clipboard.writeText(button.dataset.copy); toast("Config ID 已复制"); }));
}

async function loadTelephony() {
  const project = requireProject();
  const [policy, trunks, contacts, campaigns] = await Promise.all([
    api(`/api/platform/projects/${project}/telephony/policy`),
    api(`/api/platform/projects/${project}/telephony/trunks`),
    api(`/api/platform/projects/${project}/telephony/contacts?limit=1000`),
    api(`/api/platform/projects/${project}/telephony/campaigns?limit=200`),
  ]);
  state.telephonyPolicy = policy;
  const enabled = Boolean(policy.outbound_enabled);
  $("#outboundPolicyStatus").textContent = enabled ? "enabled" : "paused";
  $("#outboundPolicyStatus").className = `status ${enabled ? "ready" : "failed"}`;
  $("#outboundToggle").textContent = enabled ? "紧急停止全部外呼" : "恢复外呼";
  $("#outboundToggle").disabled = false;
  $("#campaignTrunk").innerHTML = `<option value="">请选择有效线路</option>${trunks.items
    .filter((item) => item.status === "active" && ["outbound", "bidirectional"].includes(item.direction) && item.livekit_trunk_id)
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.provider)}</option>`).join("")}`;
  $("#telephonyContactRows").innerHTML = contacts.items.length ? contacts.items.map((item) => `<tr><td><input class="contact-select" type="checkbox" value="${escapeHtml(item.id)}" ${item.status === "active" ? "" : "disabled"} /></td><td><strong>${escapeHtml(item.external_id)}</strong></td><td>${escapeHtml(item.name || "—")}</td><td>${escapeHtml(item.phone_number)}</td><td>${statusBadge(item.status)}</td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">暂无联系人</td></tr>`;
  $("#campaignRows").innerHTML = campaigns.items.length ? campaigns.items.map((item) => {
    const actions = item.status === "running"
      ? `<button class="text-button" data-campaign-status="paused" data-campaign="${item.id}">暂停</button> <button class="text-button" data-campaign-status="canceled" data-campaign="${item.id}">取消</button>`
      : ["draft", "paused"].includes(item.status)
        ? `<button class="text-button" data-campaign-status="running" data-campaign="${item.id}">启动</button> <button class="text-button" data-campaign-status="canceled" data-campaign="${item.id}">取消</button>`
        : "—";
    return `<tr><td><strong>${escapeHtml(item.name)}</strong><br><small>${formatTime(item.scheduled_at)}</small></td><td>${escapeHtml(item.agent_name)}<br><small>${escapeHtml(item.trunk_id || "—")}</small></td><td>${statusBadge(item.status)}</td><td>${Number(item.contact_count || 0)}</td><td>${Number(item.terminal_count || 0)} 完成 / ${Number(item.blocked_count || 0)} 阻止</td><td>${actions}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty-row">暂无外呼活动</td></tr>`;
  $$('[data-campaign-status]').forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/platform/projects/${project}/telephony/campaigns/${button.dataset.campaign}/status`, { method: "PUT", body: JSON.stringify({ status: button.dataset.campaignStatus }) });
      toast(`活动已${button.dataset.campaignStatus === "running" ? "启动" : button.dataset.campaignStatus === "paused" ? "暂停" : "取消"}`);
      await loadTelephony();
    } catch (error) { toast(error.message, true); }
  }));
}

async function loadAudit() {
  const result = await api(`/api/platform/projects/${requireProject()}/audit-logs?limit=200`);
  $("#auditRows").innerHTML = result.items.length ? result.items.map((item) => `<tr><td>${formatTime(item.created_at)}</td><td><strong>${escapeHtml(item.action)}</strong></td><td>${escapeHtml(item.actor_id)}</td><td>${escapeHtml(item.resource_type)}<br><small>${escapeHtml(item.resource_id.slice(0, 12))}</small></td><td><code>${escapeHtml(JSON.stringify(item.payload))}</code></td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">暂无审计记录</td></tr>`;
}

const loaders = { overview: loadOverview, insights: loadInsights, builder: loadBuilder, deployments: loadDeployments, models: loadModels, embed: loadEmbed, telephony: loadTelephony, audit: loadAudit };
async function loadCurrentView() {
  const view = $(".cp-nav.active").dataset.view;
  try { await loaders[view]?.(); } catch (error) { toast(error.message, true); }
}

function showView(view) {
  $$(".cp-nav").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".cp-view").forEach((item) => item.classList.toggle("active", item.id === view));
  $("#viewTitle").textContent = $(`.cp-nav[data-view="${view}"]`).textContent.trim().replace(/^\d+/, "");
  if (state.projectId) loadCurrentView();
}

function wireEvents() {
  $$(".cp-nav").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.go)));
  $("#refreshBtn").addEventListener("click", loadCurrentView);
  $("#reloadSessions").addEventListener("click", loadInsights);
  $("#userId").addEventListener("change", () => loadProjects().catch((error) => toast(error.message, true)));
  $("#projectSelect").addEventListener("change", () => { state.projectId = $("#projectSelect").value; localStorage.setItem("cp.projectId", state.projectId); loadCurrentView(); });
  const dialog = $("#projectDialog");
  $("#newProjectBtn").addEventListener("click", () => dialog.showModal());
  $$('[data-open-project]').forEach((button) => button.addEventListener("click", () => dialog.showModal()));
  $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#projectForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { const created = await api("/api/platform/projects", { method: "POST", body: JSON.stringify({ name: form.elements.name.value, slug: form.elements.slug.value, owner_id: state.userId, retention_days: Number(form.elements.retention_days.value) }) }); state.projectId = created.id; dialog.close(); form.reset(); toast("项目已创建"); await loadProjects(); } catch (error) { toast(error.message, true); } });
  $("#builderForm").addEventListener("submit", async (event) => { event.preventDefault(); try { const payload = builderPayload(event.currentTarget); const path = state.currentSpec ? `/api/platform/projects/${requireProject()}/agent-specs/${state.currentSpec.id}` : `/api/platform/projects/${requireProject()}/agent-specs`; const record = await api(path, { method: state.currentSpec ? "PUT" : "POST", body: JSON.stringify(payload) }); state.currentSpec = record; toast("AgentSpec 已保存"); await editSpec(record.id); await loadBuilder(); } catch (error) { toast(error.message, true); } });
  $("#publishSpecBtn").addEventListener("click", async () => { if (!state.currentSpec) return; try { state.currentSpec = await api(`/api/platform/projects/${requireProject()}/agent-specs/${state.currentSpec.id}/publish`, { method: "POST", body: JSON.stringify({ expected_revision: state.currentSpec.revision }) }); toast("AgentSpec 已发布"); await editSpec(state.currentSpec.id); await loadBuilder(); } catch (error) { toast(error.message, true); } });
  $("#exportSpecBtn").addEventListener("click", async () => {
    if (!state.currentSpec) return;
    try {
      const response = await fetch(`/api/platform/projects/${requireProject()}/agent-specs/${state.currentSpec.id}/export`, { headers: authHeaders() });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "导出失败");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = `agent-${state.currentSpec.spec.name}.zip`; link.click();
      URL.revokeObjectURL(url);
      toast("Agent 工程已导出");
    } catch (error) { toast(error.message, true); }
  });
  $("#observerBtn").addEventListener("click", async () => { if (!state.selectedSession) return; try { const result = await api(`/api/platform/projects/${requireProject()}/sessions/${state.selectedSession}/console/observer-token`, { method: "POST", body: JSON.stringify({ ttl_seconds: 300 }) }); await navigator.clipboard.writeText(result.token); toast("只订阅观察者 Token 已复制，5 分钟后过期"); } catch (error) { toast(error.message, true); } });
  $("#agentCreateForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/platform/projects/${requireProject()}/agents`, { method: "POST", body: JSON.stringify({ name: form.elements.name.value, description: form.elements.description.value }) }); form.reset(); toast("Agent 定义已创建"); await loadDeployments(); } catch (error) { toast(error.message, true); } });
  $("#buildForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { const result = await api(`/api/platform/projects/${requireProject()}/agents/${form.elements.agent_id.value}/builds`, { method: "POST", body: JSON.stringify({ source_ref: form.elements.source_ref.value, image_ref: form.elements.image_ref.value, spec: {} }) }); if (!result.version) throw new Error(result.build.error || "构建失败"); state.latestVersionId = result.version.id; $("#deploymentForm").elements.agent_id.value = form.elements.agent_id.value; $("#deploymentForm").elements.version_id.value = state.latestVersionId; toast(`Version ${result.version.version_number} 构建成功`); await loadDeployments(); } catch (error) { toast(error.message, true); } });
  $("#deploymentForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/platform/projects/${requireProject()}/deployments`, { method: "POST", body: JSON.stringify({ agent_id: form.elements.agent_id.value, version_id: form.elements.version_id.value, name: form.elements.name.value, desired_replicas: Number(form.elements.desired_replicas.value) }) }); toast("部署已提交"); await loadDeployments(); } catch (error) { toast(error.message, true); } });
  $("#secretForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/platform/projects/${requireProject()}/secrets/${encodeURIComponent(form.elements.name.value.trim())}`, { method: "PUT", body: JSON.stringify({ value: form.elements.value.value }) }); form.elements.value.value = ""; toast("Secret 已加密保存"); await loadDeployments(); } catch (error) { toast(error.message, true); } });
  $("#routeForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/platform/projects/${requireProject()}/inference/routes`, { method: "PUT", body: JSON.stringify({ descriptor: form.elements.descriptor.value, modality: form.elements.modality.value, provider: form.elements.provider.value, provider_model: form.elements.provider_model.value, priority: Number(form.elements.priority.value), timeout_seconds: Number(form.elements.timeout_seconds.value), enabled: true, config: {} }) }); toast("模型路由已保存"); await loadModels(); } catch (error) { toast(error.message, true); } });
  $("#embedForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/platform/projects/${requireProject()}/embed-configs`, { method: "POST", body: JSON.stringify({ name: form.elements.name.value, agent_name: form.elements.agent_name.value, room_prefix: form.elements.room_prefix.value, allowed_origins: form.elements.allowed_origins.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean), capabilities: { audio: true, text: form.elements.text.checked, camera: form.elements.camera.checked, screen_share: form.elements.screen_share.checked }, enabled: true }) }); toast("Widget 已创建"); await loadEmbed(); } catch (error) { toast(error.message, true); } });
  $("#outboundToggle").addEventListener("click", async () => {
    try {
      const policy = state.telephonyPolicy;
      if (!policy) throw new Error("外呼策略尚未加载");
      await api(`/api/platform/projects/${requireProject()}/telephony/policy`, { method: "PUT", body: JSON.stringify({
        outbound_enabled: !policy.outbound_enabled,
        timezone: policy.timezone,
        allowed_weekdays: policy.allowed_weekdays,
        calling_window_start: policy.calling_window_start,
        calling_window_end: policy.calling_window_end,
        require_consent: policy.require_consent,
        consent_purpose: policy.consent_purpose,
        max_attempts_per_number_per_day: policy.max_attempts_per_number_per_day,
        inbound_overflow_mode: policy.inbound_overflow_mode,
        inbound_overflow_destination_name: policy.inbound_overflow_destination_name,
        recording_mode: policy.recording_mode,
        recording_disclosure_text: policy.recording_disclosure_text,
      }) });
      toast(policy.outbound_enabled ? "全部外呼已紧急停止" : "外呼已恢复");
      await loadTelephony();
    } catch (error) { toast(error.message, true); }
  });
  $("#contactForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget;
    try {
      const externalId = form.elements.external_id.value.trim();
      await api(`/api/platform/projects/${requireProject()}/telephony/contacts/${encodeURIComponent(externalId)}`, { method: "PUT", body: JSON.stringify({ external_id: externalId, name: form.elements.name.value.trim(), phone_number: form.elements.phone_number.value.trim(), status: "active", metadata: {} }) });
      form.reset(); toast("联系人已保存"); await loadTelephony();
    } catch (error) { toast(error.message, true); }
  });
  $("#campaignForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget;
    try {
      const contactIds = $$(".contact-select:checked").map((node) => node.value);
      if (!contactIds.length) throw new Error("请至少勾选一个有效联系人");
      const campaign = await api(`/api/platform/projects/${requireProject()}/telephony/campaigns`, { method: "POST", body: JSON.stringify({ name: form.elements.name.value.trim(), agent_name: form.elements.agent_name.value.trim(), trunk_id: form.elements.trunk_id.value, source_number: form.elements.source_number.value.trim(), priority: 100, max_attempts: 3, max_concurrent_calls: Number(form.elements.max_concurrent_calls.value), metadata: {} }) });
      await api(`/api/platform/projects/${requireProject()}/telephony/campaigns/${campaign.id}/contacts`, { method: "POST", body: JSON.stringify({ contact_ids: contactIds }) });
      form.reset(); form.elements.agent_name.value = "commercial-agent"; form.elements.max_concurrent_calls.value = "10";
      toast("外呼活动已创建，可核对后启动"); await loadTelephony();
    } catch (error) { toast(error.message, true); }
  });
}

async function boot() {
  $("#userId").value = state.userId;
  wireEvents();
  await loadPlatform();
  try { await loadProjects(); } catch (error) { toast(error.message, true); }
}

document.addEventListener("DOMContentLoaded", boot);
