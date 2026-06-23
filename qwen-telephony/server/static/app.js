const state = {
  config: null,
  scenes: [],
  activeScene: null,
  selectedNodeId: null,
  unresolved: [],
  trainingMessages: [],
  runtimeControlsHydrated: false,
};

const pageTitles = {
  scripts: "流程编排",
  knowledge: "知识库",
  labels: "意向标签",
  learning: "问题学习",
  training: "训练测试",
  runtime: "运行参数",
};

const routeLabels = {
  positive: "肯定",
  negative: "否定",
  reject: "拒绝",
  neutral: "中性",
  unknown: "未识别",
};

const configurableIntents = ["positive", "negative", "reject", "neutral"];

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function showToast(message, type = "ok") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show ${type === "error" ? "error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.className = "toast";
  }, 2600);
}

function setBusy(button, text) {
  button.disabled = true;
  button.dataset.text = button.textContent;
  button.textContent = text;
}

function resetBusy(button) {
  if (button.dataset.text) {
    button.textContent = button.dataset.text;
    delete button.dataset.text;
  }
  button.disabled = false;
}

function activeFlow() {
  return state.activeScene?.flow || { nodes: [] };
}

function nodes() {
  return activeFlow().nodes || [];
}

function selectedNode() {
  return nodes().find((node) => node.id === state.selectedNodeId) || nodes()[0] || null;
}

function routeOptions(selected = "") {
  const options = ['<option value="">未设置</option>'];
  for (const node of nodes()) {
    options.push(`<option value="${escapeHtml(node.id)}" ${node.id === selected ? "selected" : ""}>${escapeHtml(node.name)} (${escapeHtml(node.id)})</option>`);
  }
  return options.join("");
}

function nodeClass(type) {
  if (type === "llm_fallback") return "fallback";
  if (type === "end") return "end";
  if (type === "common") return "common";
  return "scene";
}

function renderScenes() {
  const query = $("#sceneSearch").value.trim().toLowerCase();
  const industry = $("#industryFilter").value;
  const rows = state.scenes.filter((scene) => {
    const matchQuery = !query || scene.name.toLowerCase().includes(query);
    const matchIndustry = !industry || scene.industry === industry;
    return matchQuery && matchIndustry;
  });

  $("#sceneList").innerHTML = rows.length
    ? rows
        .map((scene) => `
          <button class="scene-item ${state.activeScene?.id === scene.id ? "active" : ""}" data-scene="${scene.id}">
            <strong>${escapeHtml(scene.name)}</strong>
            <span>${escapeHtml(scene.industry || "未分类")} · v${escapeHtml(scene.active_version || 1)} · ${escapeHtml(scene.status)}${state.config?.default_scene_id === scene.id ? " · 默认" : ""}</span>
            <small>知识库 ${escapeHtml(scene.knowledge_count || 0)} 条</small>
          </button>
        `)
        .join("")
    : `<div class="empty">没有匹配的话术</div>`;

  $$("[data-scene]").forEach((button) => {
    button.addEventListener("click", async () => {
      await loadScene(Number(button.dataset.scene));
    });
  });
}

function renderCanvas() {
  const flow = activeFlow();
  const canvas = $("#flowCanvas");
  if (!state.activeScene || !nodes().length) {
    canvas.innerHTML = `<div class="empty">请选择或新建一个话术场景</div>`;
    return;
  }

  const nodeIndex = new Map(nodes().map((node, index) => [node.id, index]));
  const nodeHtml = nodes()
    .map((node, index) => {
      const x = 80 + (index % 3) * 330;
      const y = 56 + Math.floor(index / 3) * 210;
      const routes = node.routes || {};
      const routeHtml = Object.entries(routeLabels)
        .map(([key, label]) => {
          const target = routes[key];
          return `<span class="route-pill ${target ? "" : "muted"}">${label}${target ? ` -> ${escapeHtml(target)}` : ""}</span>`;
        })
        .join("");
      return `
        <button class="flow-node ${nodeClass(node.type)} ${state.selectedNodeId === node.id ? "selected" : ""}"
                style="left:${x}px;top:${y}px" data-node="${escapeHtml(node.id)}">
          <span class="node-type">${escapeHtml(node.type)}</span>
          <strong>${escapeHtml(node.name)}</strong>
          <p>${escapeHtml(node.text || (node.type === "llm_fallback" ? "未命中时透传真实 LLM" : ""))}</p>
          <div class="route-pills">${routeHtml}</div>
        </button>
      `;
    })
    .join("");

  const lines = [];
  for (const [index, node] of nodes().entries()) {
    const routes = node.routes || {};
    const fromX = 80 + (index % 3) * 330 + 260;
    const fromY = 56 + Math.floor(index / 3) * 210 + 80;
    for (const [route, target] of Object.entries(routes)) {
      if (!target || !nodeIndex.has(target)) continue;
      const targetIndex = nodeIndex.get(target);
      const toX = 80 + (targetIndex % 3) * 330;
      const toY = 56 + Math.floor(targetIndex / 3) * 210 + 80;
      const width = Math.max(1, toX - fromX);
      const left = Math.min(fromX, toX);
      const top = Math.min(fromY, toY);
      lines.push(`<div class="flow-line" style="left:${left}px;top:${top}px;width:${Math.abs(width)}px" title="${escapeHtml(routeLabels[route] || route)}"></div>`);
      if (fromY !== toY) {
        lines.push(`<div class="flow-line vertical" style="left:${toX}px;top:${Math.min(fromY, toY)}px;height:${Math.abs(toY - fromY)}px"></div>`);
      }
    }
  }

  canvas.innerHTML = `<div class="canvas-stage">${lines.join("")}${nodeHtml}</div>`;
  $$("[data-node]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedNodeId = button.dataset.node;
      renderCanvas();
      renderInspector();
    });
  });
  $("#activeSceneMeta").textContent = `${state.activeScene.name} · ${state.activeScene.status}${state.config?.default_scene_id === state.activeScene.id ? " · 默认话术" : ""}`;
}

function renderInspector() {
  const node = selectedNode();
  const form = $("#nodeForm");
  if (!node) {
    form.reset();
    return;
  }
  state.selectedNodeId = node.id;
  form.elements.id.value = node.id;
  form.elements.name.value = node.name || "";
  form.elements.type.value = node.type || "scene";
  form.elements.text.value = node.text || "";
  const routes = node.routes || {};
  for (const key of Object.keys(routeLabels)) {
    form.elements[key].innerHTML = routeOptions(routes[key] || "");
  }
  const keywords = node.intent_keywords || {};
  for (const key of configurableIntents) {
    form.elements[`${key}_keywords`].value = Array.isArray(keywords[key])
      ? keywords[key].join(",")
      : (keywords[key] || "");
  }
}

function renderKnowledge() {
  const rows = state.activeScene?.knowledge || [];
  $("#knowledgeRows").innerHTML = rows.length
    ? rows
        .map((item) => `
          <tr>
            <td>${escapeHtml(item.title)}</td>
            <td>${escapeHtml(item.sort_order)}</td>
            <td class="wide-text">${escapeHtml(item.answer)}</td>
            <td>${escapeHtml(item.keywords)}</td>
            <td>${escapeHtml(item.hit_count)}</td>
            <td>${item.enabled ? "启用" : "停用"}</td>
            <td>
              <button class="secondary small" data-edit-knowledge="${item.id}">编辑</button>
              <button class="secondary small" data-delete-knowledge="${item.id}">删除</button>
            </td>
          </tr>
        `)
        .join("")
    : `<tr><td colspan="7"><div class="empty">暂无知识库内容</div></td></tr>`;

  $$("[data-edit-knowledge]").forEach((button) => {
    button.addEventListener("click", () => openKnowledgeDialog(Number(button.dataset.editKnowledge)));
  });
  $$("[data-delete-knowledge]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/dialogue/knowledge/${button.dataset.deleteKnowledge}`, { method: "DELETE" });
      showToast("知识已删除");
      await reloadActiveScene();
    });
  });
}

function renderLabels() {
  const rules = state.activeScene?.label_rules || [];
  $("#labelRules").innerHTML = rules.length
    ? rules
        .map((rule) => {
          let condition = {};
          try {
            condition = JSON.parse(rule.condition_json || "{}");
          } catch {
            condition = {};
          }
          return `
            <article class="rule-item">
              <div>
                <strong>${escapeHtml(rule.label)}</strong>
                <span>优先级 ${escapeHtml(rule.priority)}</span>
                <p>${escapeHtml(JSON.stringify(condition))}</p>
              </div>
              <button class="secondary small" data-delete-rule="${rule.id}">删除</button>
            </article>
          `;
        })
        .join("")
    : `<div class="empty">暂无意向标签规则</div>`;

  $$("[data-delete-rule]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/dialogue/label-rules/${button.dataset.deleteRule}`, { method: "DELETE" });
      showToast("标签规则已删除");
      await reloadActiveScene();
    });
  });
}

function renderUnresolved() {
  $("#unresolvedRows").innerHTML = state.unresolved.length
    ? state.unresolved
        .map((item) => `
          <tr>
            <td>${escapeHtml(item.user_text)}</td>
            <td>${escapeHtml(item.hit_count)}</td>
            <td>${escapeHtml(item.status)}</td>
            <td>${escapeHtml(item.last_seen_at)}</td>
          </tr>
        `)
        .join("")
    : `<tr><td colspan="4"><div class="empty">暂无未命中问题</div></td></tr>`;
}

function renderTrainingChat() {
  const box = $("#trainingChat");
  if (!state.trainingMessages.length) {
    box.innerHTML = `<div class="empty">点击“开始会话 / 重置”后，机器人会先发起开场白。</div>`;
    return;
  }
  box.innerHTML = state.trainingMessages
    .map((message) => `
      <div class="chat-message ${message.role}">
        <span>${message.role === "assistant" ? "机器人" : "用户"}</span>
        <p>${escapeHtml(message.text)}</p>
      </div>
    `)
    .join("");
  box.scrollTop = box.scrollHeight;
}

function renderTrainingSceneSelect() {
  const select = $("#trainingSceneSelect");
  if (!select) return;
  select.innerHTML = state.scenes
    .map((scene) => `
      <option value="${scene.id}" ${state.activeScene?.id === scene.id ? "selected" : ""}>
        ${escapeHtml(scene.name)} · ${escapeHtml(scene.industry || "未分类")}
      </option>
    `)
    .join("");
}

function renderAll() {
  renderScenes();
  renderCanvas();
  renderInspector();
  renderKnowledge();
  renderLabels();
  renderUnresolved();
  renderTrainingChat();
  renderTrainingSceneSelect();
}

function renderRuntimeControls(force = false) {
  if (state.runtimeControlsHydrated && !force) return;
  $("#nluSwitch").checked = Boolean(state.config?.nlu_enabled);
  state.runtimeControlsHydrated = true;
}

async function preserveNluSwitch(action) {
  const switchInput = $("#nluSwitch");
  const checked = switchInput.checked;
  try {
    return await action();
  } finally {
    switchInput.checked = checked;
  }
}

async function loadScene(sceneId) {
  state.activeScene = await api(`/api/dialogue/scenes/${sceneId}`);
  state.selectedNodeId = state.activeScene.flow.entry_node || state.activeScene.flow.nodes?.[0]?.id;
  await loadUnresolved();
  renderAll();
}

async function reloadActiveScene() {
  if (state.activeScene?.id) {
    await loadScene(state.activeScene.id);
  }
}

async function loadUnresolved() {
  const suffix = state.activeScene?.id ? `?scene_id=${state.activeScene.id}` : "";
  state.unresolved = await api(`/api/dialogue/unresolved${suffix}`);
}

async function loadAll({ syncRuntime = false } = {}) {
  const [health, config, scenes] = await Promise.all([
    api("/api/health"),
    api("/api/dialogue/config"),
    api("/api/dialogue/scenes"),
  ]);
  $("#healthText").textContent = health.status === "ok" ? "API healthy" : "API degraded";
  state.config = config;
  state.scenes = scenes;
  renderRuntimeControls(syncRuntime);
  const activeId = state.activeScene?.id || config.default_scene_id || scenes[0]?.id;
  if (activeId) {
    await loadScene(activeId);
  } else {
    renderAll();
  }
}

function updateNodeFromForm() {
  const node = selectedNode();
  if (!node) return;
  const form = $("#nodeForm");
  node.name = form.elements.name.value;
  node.type = form.elements.type.value;
  node.text = form.elements.text.value;
  if (node.type === "scene" || node.type === "common") {
    node.routes = {};
    for (const key of Object.keys(routeLabels)) {
      node.routes[key] = form.elements[key].value;
    }
    node.intent_keywords = {};
    for (const key of configurableIntents) {
      node.intent_keywords[key] = form.elements[`${key}_keywords`].value
        .split(/[,，;\n；]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }
  } else {
    delete node.routes;
    delete node.intent_keywords;
  }
}

async function saveFlow() {
  updateNodeFromForm();
  const payload = { flow: state.activeScene.flow };
  state.activeScene = await api(`/api/dialogue/scenes/${state.activeScene.id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  showToast("流程已保存");
  renderAll();
}

async function setDefaultScene(sceneId = state.activeScene?.id, silent = false) {
  if (!sceneId) return;
  state.config = await api(`/api/dialogue/scenes/${sceneId}/default`, { method: "POST" });
  renderAll();
  if (!silent) showToast("默认话术已更新");
}

async function runMicroSipTest() {
  if (!state.activeScene?.id) return;
  const testResult = await api(`/api/dialogue/scenes/${state.activeScene.id}/microsip-test`, {
    method: "POST",
    body: JSON.stringify({ phone: "1000@127.0.0.1:5066", visible: true }),
  });
  showToast(testResult.started ? "MicroSIP 测试呼叫已发起" : "MicroSIP 环境未就绪");
  $("#trainingResult").textContent = JSON.stringify(testResult, null, 2);
}

function validateFlowClient() {
  const ids = new Set(nodes().map((node) => node.id));
  const errors = [];
  if (!ids.has(activeFlow().entry_node)) errors.push("入口节点不存在");
  for (const node of nodes()) {
    if (!node.id || !node.name || !node.type) errors.push(`${node.id || "未知节点"} 缺少基础字段`);
    if (node.type === "scene") {
      if (!node.routes?.unknown) errors.push(`${node.name} 缺少未识别出口`);
      for (const target of Object.values(node.routes || {})) {
        if (target && !ids.has(target)) errors.push(`${node.name} 指向不存在的节点 ${target}`);
      }
    }
  }
  if (errors.length) {
    showToast(errors[0], "error");
  } else {
    showToast("校验通过");
  }
}

function addNode(type = "scene") {
  const id = `${type}_${Date.now().toString(36)}`;
  const node = {
    id,
    type,
    name: type === "llm_fallback" ? "LLM 兜底" : type === "end" ? "结束节点" : "新场景节点",
    text: type === "llm_fallback" ? "" : "请输入机器人话术",
  };
  if (type === "scene" || type === "common") {
    node.routes = { positive: "", negative: "", reject: "", neutral: "", unknown: "" };
    node.intent_keywords = {
      positive: ["有兴趣", "可以", "方便", "想了解"],
      negative: ["不需要", "没兴趣", "不方便"],
      reject: ["别打了", "挂了", "拉黑"],
      neutral: ["多少钱", "在哪里", "怎么做"],
    };
  }
  activeFlow().nodes.push(node);
  state.selectedNodeId = id;
  renderAll();
}

function deleteSelectedNode() {
  const flow = activeFlow();
  const node = selectedNode();
  if (!node || node.id === flow.entry_node) {
    showToast("入口节点不能删除", "error");
    return;
  }
  flow.nodes = flow.nodes.filter((item) => item.id !== node.id);
  for (const item of flow.nodes) {
    for (const key of Object.keys(item.routes || {})) {
      if (item.routes[key] === node.id) item.routes[key] = "";
    }
  }
  state.selectedNodeId = flow.entry_node;
  renderAll();
}

function openKnowledgeDialog(itemId = null) {
  const dialog = $("#knowledgeDialog");
  const form = $("#knowledgeForm");
  form.reset();
  if (itemId) {
    const item = state.activeScene.knowledge.find((row) => row.id === itemId);
    form.elements.id.value = item.id;
    form.elements.title.value = item.title;
    form.elements.answer.value = item.answer;
    form.elements.keywords.value = item.keywords;
    form.elements.sort_order.value = item.sort_order;
    form.elements.enabled.value = item.enabled ? "true" : "false";
  }
  dialog.showModal();
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.view;
      $$(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
      $$(".view").forEach((item) => item.classList.toggle("active", item.id === view));
      $("#pageTitle").textContent = pageTitles[view];
    });
  });
}

function bindEvents() {
  $("#refreshBtn").addEventListener("click", () => loadAll({ syncRuntime: true }).then(() => showToast("已刷新")).catch(handleError));
  $("#sceneSearch").addEventListener("input", renderScenes);
  $("#industryFilter").addEventListener("change", renderScenes);
  $("#saveFlowBtn").addEventListener("click", () => saveFlow().catch(handleError));
  $("#validateFlowBtn").addEventListener("click", validateFlowClient);
  $("#zoomFitBtn").addEventListener("click", () => $("#flowCanvas").scrollTo({ left: 0, top: 0, behavior: "smooth" }));
  $("#addNodeBtn").addEventListener("click", () => addNode("scene"));
  $("#deleteNodeBtn").addEventListener("click", deleteSelectedNode);
  $$(".palette-item").forEach((button) => button.addEventListener("click", () => addNode(button.dataset.nodeTemplate)));

  $("#nodeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    updateNodeFromForm();
    renderAll();
    showToast("节点已更新，请保存流程");
  });

  $("#nluSwitch").addEventListener("change", async (event) => {
    state.config = await api("/api/dialogue/config/nlu", {
      method: "POST",
      body: JSON.stringify({
        enabled: event.target.checked,
      }),
    });
    showToast(event.target.checked ? "NLU/状态机已启用" : "已切回原 ASR + LLM + TTS 链路");
    renderRuntimeControls(true);
  });

  $("#setDefaultBtn").addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    await preserveNluSwitch(() => setDefaultScene());
  });

  $("#microSipTestBtn").addEventListener("click", async () => {
    await runMicroSipTest();
  });

  $("#publishBtn").addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    await preserveNluSwitch(async () => {
      await saveFlow();
      state.activeScene = await api(`/api/dialogue/scenes/${state.activeScene.id}/publish`, { method: "POST" });
      state.scenes = await api("/api/dialogue/scenes");
      renderAll();
      showToast("话术已发布");
    });
  });

  $("#newSceneBtn").addEventListener("click", () => $("#sceneDialog").showModal());
  $("#copySceneBtn").addEventListener("click", async () => {
    if (!state.activeScene) return;
    const scene = await api("/api/dialogue/scenes", {
      method: "POST",
      body: JSON.stringify({
        name: `${state.activeScene.name} 副本`,
        industry: state.activeScene.industry,
        business_type: state.activeScene.business_type,
      }),
    });
    scene.flow = structuredClone(state.activeScene.flow);
    await api(`/api/dialogue/scenes/${scene.id}`, { method: "PUT", body: JSON.stringify({ flow: scene.flow }) });
    await loadAll();
    await loadScene(scene.id);
    showToast("话术已复制");
  });

  $("#sceneForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const scene = await api("/api/dialogue/scenes", { method: "POST", body: JSON.stringify(data) });
    form.reset();
    $("#sceneDialog").close();
    await loadAll();
    await loadScene(scene.id);
    showToast("话术已创建");
  });

  $("#newKnowledgeBtn").addEventListener("click", () => openKnowledgeDialog());
  $("#knowledgeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const id = data.id;
    delete data.id;
    data.sort_order = Number(data.sort_order || 10);
    data.enabled = data.enabled === "true";
    if (id) {
      await api(`/api/dialogue/knowledge/${id}`, { method: "PUT", body: JSON.stringify(data) });
    } else {
      await api(`/api/dialogue/scenes/${state.activeScene.id}/knowledge`, { method: "POST", body: JSON.stringify(data) });
    }
    $("#knowledgeDialog").close();
    showToast("知识已保存");
    await reloadActiveScene();
  });

  $("#newLabelBtn").addEventListener("click", () => $("#labelDialog").showModal());
  $("#labelForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const condition = {};
    for (const key of ["knowledge_hit_count", "positive_count", "reject_count"]) {
      const value = Number(values[key] || 0);
      if (value > 0) condition[key] = value;
    }
    await api(`/api/dialogue/scenes/${state.activeScene.id}/label-rules`, {
      method: "POST",
      body: JSON.stringify({
        label: values.label,
        priority: Number(values.priority || 10),
        condition,
        enabled: true,
      }),
    });
    form.reset();
    $("#labelDialog").close();
    showToast("标签规则已保存");
    await reloadActiveScene();
  });

  $("#reloadLearningBtn").addEventListener("click", async () => {
    await loadUnresolved();
    renderUnresolved();
    showToast("问题学习已刷新");
  });

  $("#startTrainingBtn").addEventListener("click", async () => {
    const sessionId = $("#trainingForm").elements.session_id.value || "studio-test";
    const sceneId = Number($("#trainingSceneSelect").value || state.activeScene?.id);
    if (sceneId && sceneId !== state.activeScene?.id) {
      await loadScene(sceneId);
    }
    const result = await api("/api/dialogue/start", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        scene_id: sceneId || state.activeScene?.id,
      }),
    });
    state.trainingMessages = [];
    if (result.text) {
      state.trainingMessages.push({ role: "assistant", text: result.text });
    }
    $("#trainingResult").textContent = JSON.stringify(result, null, 2);
    renderTrainingChat();
    showToast("训练会话已开始");
  });

  $("#trainingForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const sceneId = Number(data.scene_id || state.activeScene?.id);
    if (sceneId && sceneId !== state.activeScene?.id) {
      await loadScene(sceneId);
    }
    state.trainingMessages.push({ role: "user", text: data.text });
    const result = await api("/api/dialogue/turn", {
      method: "POST",
      body: JSON.stringify({
        session_id: data.session_id || "studio-test",
        scene_id: sceneId || state.activeScene?.id,
        text: data.text,
        channel: "studio",
      }),
    });
    if (result.text) {
      state.trainingMessages.push({ role: "assistant", text: result.text });
    } else {
      state.trainingMessages.push({ role: "assistant", text: "未命中固定话术，将进入 LLM 兜底。" });
    }
    $("#trainingResult").textContent = JSON.stringify(result, null, 2);
    form.elements.text.value = "";
    renderTrainingChat();
    await reloadActiveScene();
  });

  $("#trainingSceneSelect").addEventListener("change", async (event) => {
    const sceneId = Number(event.target.value);
    state.trainingMessages = [];
    $("#trainingResult").textContent = "已切换测试话术，请点击“开始会话 / 重置”。";
    await loadScene(sceneId);
    showToast("训练话术已切换");
  });

  $$("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });
}

function handleError(error) {
  console.error(error);
  $("#healthText").textContent = "API error";
  showToast(error.message, "error");
}

bindNavigation();
bindEvents();
loadAll({ syncRuntime: true }).catch(handleError);
