const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
const modeLabels = { active: "正常记忆", readonly: "只读记忆", paused: "暂停记忆" };
const modeHelp = {
  active: "读取已有记忆，并保存新的记忆。",
  readonly: "读取已有记忆，不保存新的记忆。",
  paused: "不读取记忆，也不保存新的记忆。",
};
let data = null;
let settingsRevision = "";
let scopeId = "";
let modeDirty = false;
let forgetState = null;
let maintenanceState = null;
let refreshTimer;
let refreshRunning = false;
let refreshFailures = 0;
let modelRequestId = "";
let modelWatch = null;
let statusSignature = "";
const locks = new Set();
let feedbackTimer;

function text(value) {
  if (value == null) return "";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = text(content);
  return node;
}

function notify(message, error = false, target = "feedback") {
  if ($(target).closest("dialog") && !$(target).closest("dialog").open) target = "feedback";
  const node = $(target);
  (target === "feedback" ? $("feedback-message") : node).textContent = message;
  node.hidden = false;
  node.classList.toggle("error", error);
  node.setAttribute("role", error ? "alert" : "status");
  if (target === "feedback") {
    clearTimeout(feedbackTimer);
    if (!error) feedbackTimer = setTimeout(() => { node.hidden = true; }, 6500);
  }
}

function errorMessage(error) {
  const message = error?.message || "";
  if (/timeout|timed out|超时/i.test(message)) return "请求超时，请稍后重试。";
  return message || "操作未完成，请稍后重试。";
}

async function perform(button, group, work, feedbackTarget = "feedback") {
  if (locks.has(group)) return;
  locks.add(group);
  const fieldset = $(`${group}-fields`);
  if (fieldset) fieldset.disabled = true;
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
  }
  try {
    await work();
  } catch (error) {
    notify(errorMessage(error), true, feedbackTarget);
  } finally {
    locks.delete(group);
    if (fieldset) fieldset.disabled = !data || (group === "memory" && !scopeId);
    if (button) {
      button.textContent = label;
      button.disabled = false;
    }
    if (group === "setup") updateProviderHelp();
    if (group === "memory") renderScope();
  }
}

function activateTab(name, focus = false) {
  if (name !== "setup") {
    clearCommands();
  }
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    const selected = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !selected;
    if (selected && focus) tab.focus();
  });
}

const tabs = [...document.querySelectorAll('[role="tab"]')];
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activateTab(tab.id.slice(4)));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next == null) return;
    event.preventDefault();
    activateTab(tabs[next].id.slice(4), true);
  });
});

function option(value, label, disabled = false) {
  const node = el("option", "", label);
  node.value = value;
  node.disabled = disabled;
  return node;
}

function providerSelect(kind, selected) {
  const select = $(`${kind}-provider`);
  select.replaceChildren(option("", kind === "rerank" ? "不使用重排模型" : "请选择模型"));
  const providers = data.providers?.[kind] || [];
  providers.forEach((provider) => {
    const label = [provider.name || provider.label || provider.id, provider.model].filter(Boolean).join(" / ");
    select.append(option(provider.id, provider.supported === false ? `${label}（${provider.reason || "暂不支持"}）` : label, provider.supported === false));
  });
  const found = providers.find((provider) => provider.id === selected);
  if (selected && !found) select.append(option(selected, `${selected}（供应商已不存在，请重新选择）`, true));
  select.value = selected || "";
}

function updateProviderHelp() {
  if (!data) return;
  const missing = [];
  for (const [kind, name] of [["chat", "聊天"], ["embedding", "嵌入"]]) {
    if (!(data.providers?.[kind] || []).some((provider) => provider.supported !== false)) missing.push(name);
  }
  $("provider-help").textContent = missing.length
    ? `缺少可用的${missing.join("、")}模型。请到 AstrBot「模型供应商」添加对应模型，模型列表会自动更新。`
    : "聊天模型用于整理记忆，嵌入模型用于查找记忆；重排模型可不选。";
  $("save-models").disabled = missing.length > 0 || locks.has("setup");
}

function clearUninstall() {
  $("uninstallation").hidden = true;
  $("uninstallation").open = false;
  $("uninstall-command").value = "";
}

function updateConnectionMode() {
  const managed = document.querySelector('[name="connection-mode"]:checked').value === "managed";
  $("managed-settings").hidden = !managed;
  $("models-settings").hidden = !managed;
  $("maintenance").hidden = !managed || !data?.has_installation;
  $("api-key-field").hidden = managed;
  $("api-url").readOnly = managed;
  $("connection-heading").textContent = managed ? "配套服务连接" : "连接已有 Hindsight";
  $("key-help").hidden = managed;
  $("check-connection").hidden = managed;
  $("key-help").textContent = data?.config.has_api_key ? "已保存连接密钥。留空沿用，填写新值则替换。" : "填写已有 Hindsight 的服务 API Key，与模型供应商密钥不同。";
}

function addAccount(account = {}) {
  const row = el("div", "account-row");
  const platformLabel = el("label", "", "QQ 机器人");
  const select = el("select");
  select.className = "account-platform";
  select.required = true;
  select.append(option("", "请选择已连接的机器人"));
  (data.platforms || []).forEach((platform) => select.append(option(platform.id, platform.name || platform.id)));
  if (account.platform_instance_id && !(data.platforms || []).some((item) => item.id === account.platform_instance_id)) {
    select.append(option(account.platform_instance_id, `${account.platform_instance_id}（机器人不可用，请重新选择）`, true));
  }
  select.value = account.platform_instance_id || "";
  platformLabel.append(select);
  const userLabel = el("label", "", "用户 QQ 号");
  const input = el("input");
  input.type = "text";
  input.inputMode = "numeric";
  input.pattern = "[0-9]+";
  input.maxLength = 20;
  input.required = true;
  input.autocomplete = "off";
  input.className = "account-user";
  input.placeholder = "与这个机器人私聊的 QQ 号";
  input.value = account.user_id || "";
  userLabel.append(input);
  const remove = el("button", "danger-quiet", "移除");
  remove.type = "button";
  remove.setAttribute("aria-label", account.user_id ? `移除用户 ${account.user_id}` : "移除此用户");
  remove.addEventListener("click", () => {
    row.remove();
    $("add-account").focus();
  });
  row.append(platformLabel, userLabel, remove);
  $("accounts").append(row);
  return select;
}

function refreshAccountPlatforms() {
  $("accounts").querySelectorAll(".account-platform").forEach((select) => {
    const selected = select.value;
    select.replaceChildren(option("", "请选择已连接的机器人"));
    (data.platforms || []).forEach((platform) => select.append(option(platform.id, platform.name || platform.id)));
    if (selected && !(data.platforms || []).some((platform) => platform.id === selected)) {
      select.append(option(selected, `${selected}（机器人不可用，请重新选择）`, true));
    }
    select.value = selected;
  });
}

function hydrateConfig() {
  const config = data.config || {};
  settingsRevision = data.config_revision;
  $("api-url").value = config.api_url || "";
  $("api-key").value = "";
  $("control-plane-url").value = config.control_plane_url || "";
  $("connection-result").hidden = true;
  $("api-key").placeholder = config.has_api_key ? "已保存，留空继续使用" : "填写记忆服务的 API Key";
  $("key-help").textContent = config.has_api_key ? "已保存 API Key。留空保留，填写新值则替换。" : "填写记忆服务的连接密钥。";
  $("setup-intro").hidden = config.configured === true;
  $("save-settings").textContent = config.configured ? "保存设置" : "完成设置";
  document.querySelector(`[name="connection-mode"][value="${config.managed || !config.api_url ? "managed" : "existing"}"]`).checked = true;
  updateConnectionMode();
  for (const kind of ["chat", "embedding", "rerank"]) providerSelect(kind, config[`${kind}_provider_id`]);
  $("accounts").replaceChildren();
  (config.allowed_accounts || []).forEach(addAccount);
  if (!config.allowed_accounts?.length) addAccount();
  updateProviderHelp();
}

function scopeName(scope) {
  const platform = (data?.platforms || []).find((item) => item.id === scope.platform_instance_id);
  return `QQ ${scope.user_id} · ${platform?.name || scope.platform_instance_id}`;
}

function countDescription(counts) {
  if (counts == null) return "任务统计暂未提供";
  if (typeof counts === "number") return `任务数：${counts}`;
  const labels = {
    pending: "等待处理", running: "正在处理", processing: "正在处理", queued: "排队中",
    failed: "失败", error: "错误", retry: "待重试", retrying: "重试中",
    completed: "已完成", done: "已完成", succeeded: "已完成", total: "总计",
    retained: "已保存", cancelled: "已取消", dead: "失败",
    ready: "等待处理", submitting: "正在提交", unknown: "结果待确认", tracking: "正在整理",
    reconcile_required: "需要核对", discarded: "已丢弃", collecting: "收集中", held: "等待回复",
    cancel_requested: "正在取消",
  };
  return Object.entries(counts).map(([key, value]) => `${labels[key] || key}：${text(value)}`).join("，") || "暂无后台任务";
}

function renderStatus() {
  const status = data.status || {};
  const connected = status.connected;
  $("status-label").textContent = status.recovering ? "正在恢复记忆服务"
    : connected === true ? "服务已连接" : status.label || (connected === false ? "服务未连接" : "连接状态未知");
  $("status-dot").className = `status-dot${!status.recovering && connected === true ? " good" : connected === false ? " bad" : ""}`;
  $("status-description").textContent = status.recovering ? "恢复完成后自动更新。"
    : connected === false ? (data.config.managed
      ? "请检查配套服务是否运行；服务恢复后会自动连接。"
      : "请检查服务地址、密钥与服务状态，可点击「测试服务连接」查看原因。")
    : "";
  if (data.config.managed && data.managed_service?.installed && !status.recovering) {
    if (!data.managed_service.ready) {
      $("status-label").textContent = "服务已安装，请选择模型并保存";
      $("status-description").textContent = "";
    } else if (!data.config.configured) {
      $("status-label").textContent = "模型已保存，请添加使用者并完成设置";
    }
  }
  $("status-description").hidden = !$("status-description").textContent;
  $("scope-statuses").replaceChildren();
  if (!data.scopes?.length) {
    const empty = el("div", "empty");
    empty.append(el("h3", "", "还没有添加使用者"), el("p", "", "在「设置」中添加使用者并保存。"));
    $("scope-statuses").append(empty);
  }
  (data.scopes || []).forEach((scope) => {
    const row = el("article", "scope-status");
    const heading = el("div", "scope-status-header");
    const manage = el("button", "", "管理此账号");
    manage.type = "button";
    manage.addEventListener("click", () => {
      if (locks.has("memory")) return;
      selectScope(scope.scope_id);
      activateTab("memories", true);
    });
    heading.append(el("strong", "", scopeName(scope)), el("span", "badge", scope.pending ? "等待连接" : modeLabels[scope.mode] || "模式未知"), manage);
    row.append(heading, el("p", "hint", scope.pending ? pendingScopeHelp(scope) : countDescription(scope.summary?.counts)));
    if (scope.summary?.maintenance || scope.summary?.bank_missing) row.append(el("p", "hint", maintenanceDescription(scope.summary)));
    if (scope.summary?.error_message) row.append(el("p", "hint error-text", scope.summary.error_message));
    $("scope-statuses").append(row);
  });
}

async function refreshBootstrap({ hydrate = false, automatic = false, pollMaintenance = true } = {}) {
  const next = await bridge.apiGet("bootstrap");
  if (!next || !next.config || !Array.isArray(next.scopes)) throw new Error("设置数据不完整，请刷新页面重试。");
  data = next;
  maintenanceState = null;
  if (hydrate) hydrateConfig();
  else if (!automatic) {
    for (const kind of ["chat", "embedding", "rerank"]) providerSelect(kind, $(`${kind}-provider`).value);
    refreshAccountPlatforms();
    updateProviderHelp();
    updateConnectionMode();
  }
  if (automatic && document.activeElement?.tagName !== "SELECT") {
    for (const kind of ["chat", "embedding", "rerank"]) {
      const providers = next.providers?.[kind] || [];
      const select = $(`${kind}-provider`);
      const version = JSON.stringify(providers);
      if (select.dataset.providers !== version) {
        providerSelect(kind, select.value);
        select.dataset.providers = version;
      }
    }
    refreshAccountPlatforms();
    updateProviderHelp();
  }
  $("platform-help").textContent = data.platforms?.length ? "" : "未找到 QQ 机器人。请先在 AstrBot 中添加机器人。";
  $("platform-help").hidden = !$("platform-help").textContent;
  const nextStatusSignature = JSON.stringify([data.config.configured, data.status, data.managed_service, data.scopes]);
  if (!automatic || nextStatusSignature !== statusSignature) {
    renderStatus();
    statusSignature = nextStatusSignature;
  }
  const previous = scopeId;
  const scopes = data.scopes || [];
  scopeId = scopes.some((scope) => scope.scope_id === previous) ? previous : (scopes[0]?.scope_id || "");
  const choices = scopes.map((scope) => [scope.scope_id, scopeName(scope)]);
  if (!scopes.length) choices.push(["", "暂无用户"]);
  if (JSON.stringify([...$("scope-picker").options].map((item) => [item.value, item.textContent])) !== JSON.stringify(choices)) {
    $("scope-picker").replaceChildren(...choices.map(([id, name]) => option(id, name)));
  }
  $("scope-picker").value = scopeId;
  $("memory-fields").disabled = !scopeId || locks.has("memory");
  $("memory-fields").hidden = !scopes.length;
  $("setup-fields").disabled = locks.has("setup");
  $("no-scopes").hidden = scopes.length > 0;
  if (previous !== scopeId) modeDirty = false;
  if (!locks.has("memory")) renderScope();
  if (pollMaintenance) await readMaintenanceStatus();
  $("startup").hidden = true;
  if (data.model_job?.status === "running" && !modelWatch) void watchModels();
}

function maintenanceDescription(summary = {}, state = null) {
  if (summary.bank_missing || state?.bank_missing) return "映射失效：原 Bank 已不存在。恢复原 Bank 后可结束维护并核对映射；也可通过完整遗忘显式登记新 Bank。";
  if (state?.phase === "inactive" || !summary.maintenance) return "未处于维护状态。";
  if (state?.error) return `维护状态暂时无法确认：${state.error} 请保持暂停，刷新维护状态后再操作。`;
  if (state?.ready === true && state.phase === "ready") return "可维护：服务已确认任务排空，可在官方管理页面操作此 Bank。";
  if (state?.pending || state?.in_flight || state?.remote_active) return "排空中：仍有待办或远端任务。未提交的待办须明确勾选丢弃并应用维护选项；状态不明或取消结果无法确认时保持暂停，可使用完整遗忘轮换 Bank。";
  return "排空中：等待服务确认。请刷新维护状态，确认可维护后再操作 Bank。";
}

async function readMaintenanceStatus() {
  const snapshot = data;
  const id = scopeId;
  const scope = snapshot?.scopes.find((item) => item.scope_id === id);
  if (!scope?.summary?.maintenance || scope.pending) return;
  let state;
  try {
    const response = await bridge.apiGet(endpoint("action", id), { action: "maintenance_status" });
    state = response.maintenance;
  } catch (error) {
    state = { phase: "draining", ready: false, error: errorMessage(error) };
  }
  if (snapshot !== data || id !== scopeId) return;
  maintenanceState = state;
  renderScope();
}

function renderScope() {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const summary = scope?.summary || {};
  if (!modeDirty) $("scope-mode").value = scope?.mode || "";
  $("mode-help").textContent = scope?.pending ? pendingScopeHelp(scope) : modeHelp[$("scope-mode").value] || "";
  for (const id of ["scope-mode", "save-mode", "retry", "maintenance-begin", "maintenance-status", "maintenance-resume", "discard-pending", "forget"]) $(id).disabled = !scope || scope.pending === true;
  const maintenanceActive = !!summary.maintenance;
  $("scope-mode").querySelector('[value="active"]').disabled = maintenanceActive || !!summary.bank_missing;
  $("scope-mode").querySelector('[value="readonly"]').disabled = maintenanceActive || !!summary.bank_missing;
  $("maintenance-begin").textContent = maintenanceActive ? "应用维护选项" : "开始维护";
  $("maintenance-resume").disabled ||= !maintenanceActive || maintenanceState?.ready !== true || maintenanceState.phase !== "ready";
  $("retry").disabled ||= maintenanceActive || !!summary.bank_missing;
  $("scope-error").hidden = !summary.error_message;
  $("scope-error").textContent = text(summary.error_message);
  $("control-plane-address").value = data?.config.control_plane_url || "";
  $("control-plane-missing").hidden = !!data?.config.control_plane_url;
  $("scope-bank").value = scope?.bank || "";
  $("queue-summary").textContent = scope?.pending ? pendingScopeHelp(scope) : countDescription(summary.counts);
  $("maintenance-state").textContent = maintenanceDescription(summary, maintenanceState);
}

function pendingScopeHelp(scope) {
  if (scope.pending_reason === "service") return "请先在「设置」中完成服务配置。";
  if (scope.pending_reason === "recovering") return "记忆服务正在恢复，完成后自动更新。";
  return "QQ 机器人尚未连接。连接后会自动更新。";
}

function selectScope(id) {
  scopeId = id;
  $("scope-picker").value = id;
  modeDirty = false;
  maintenanceState = null;
  $("discard-pending").checked = false;
  renderScope();
  void readMaintenanceStatus();
}

function endpoint(suffix, id = scopeId) {
  if (!id) throw new Error("请先选择用户。");
  return `scopes/${encodeURIComponent(id)}/${suffix}`;
}

function controlPlaneUrl(value) {
  value = value.trim();
  if (!value) return "";
  let parsed;
  try { parsed = new URL(value); } catch { throw new Error("请填写完整的官方管理地址。"); }
  if (!/^https?:\/\//i.test(value) || !["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || /[@]/.test(value.split("/")[2]) || /[?#\\\s]/.test(value)) throw new Error("管理地址仅支持 HTTP/HTTPS，不能包含登录凭据、查询参数或片段。");
  return value;
}

function connectionPayload() {
  const apiUrl = $("api-url").value.trim();
  let parsed;
  try { parsed = new URL(apiUrl); } catch { throw new Error("请填写完整的 API 地址，例如 http://hindsight:8888。"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("API 地址应使用 http 或 https，密钥请填写在 API Key 中。");
  return { api_url: apiUrl, api_key: $("api-key").value.trim() };
}

function settingsPayload() {
  const payload = {
    ...connectionPayload(),
    control_plane_url: controlPlaneUrl($("control-plane-url").value),
    allowed_accounts: [], config_revision: settingsRevision,
  };
  const seen = new Set();
  for (const row of $("accounts").children) {
    const platform = row.querySelector(".account-platform");
    const user = row.querySelector(".account-user");
    const userId = user.value.trim();
    if (!platform.value || platform.selectedOptions[0]?.disabled) throw new Error("请为每个用户选择可用的 QQ 机器人。");
    if (!/^[0-9]+$/.test(userId)) throw new Error("用户 QQ 号只能填写数字。");
    const identity = JSON.stringify([platform.value, userId]);
    if (seen.has(identity)) throw new Error(`用户 ${userId} 已经添加，请移除重复行。`);
    seen.add(identity);
    payload.allowed_accounts.push({ platform_instance_id: platform.value, user_id: userId });
  }
  return payload;
}

async function checkConnection(payload) {
  const response = await bridge.apiPost("connection/check", payload);
  if (response.ok !== true) throw new Error(response.message || "连接未成功，请检查 API 地址和 Key。");
  notify(response.message || "记忆服务连接成功。", false, "connection-result");
  return response;
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function watchModels(requestId = "") {
  if (modelWatch) return modelWatch;
  locks.add("setup");
  $("setup-fields").disabled = true;
  $("model-progress").hidden = false;
  modelWatch = (async () => {
    while (true) {
      let job;
      try {
        job = await bridge.apiGet("models/status");
      } catch {
        $("model-progress").textContent = "暂时无法获取进度，正在重新连接。请勿重复提交模型配置。";
        await delay(5000);
        continue;
      }
      if (requestId && job.request_id !== requestId) {
        $("model-progress").textContent = "模型保存请求未确认，请点击「验证并保存模型」重试。";
        if (job.status === "running") {
          requestId = job.request_id;
          continue;
        }
        return;
      }
      $("model-progress").textContent = job.message || "正在处理模型配置…";
      if (job.status === "running") {
        await delay(2000);
        continue;
      }
      if (job.status === "succeeded") {
        settingsRevision = job.config_revision;
        $("model-progress").textContent = "模型已保存，记忆服务连接正常。";
        await refreshBootstrap();
        notify("模型已保存，记忆服务连接正常。");
      } else {
        $("model-progress").textContent = job.status === "idle"
          ? "模型检查已中断，请重新验证并保存。"
          : `${job.message || "模型验证或保存失败。"} 请修正问题后重试。`;
        await refreshBootstrap({ automatic: true });
      }
      return;
    }
  })().catch((error) => {
    $("model-progress").textContent = errorMessage(error);
  }).finally(() => {
    modelWatch = null;
    modelRequestId = "";
    locks.delete("setup");
    $("setup-fields").disabled = !data;
    updateProviderHelp();
  });
  return modelWatch;
}

function scheduleRefresh(milliseconds = 5000) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => void autoRefresh(), milliseconds);
}

async function autoRefresh() {
  if (document.hidden || refreshRunning || !data || locks.size) {
    scheduleRefresh();
    return;
  }
  refreshRunning = true;
  try {
    await refreshBootstrap({ automatic: true });
    refreshFailures = 0;
    $("update-error").hidden = true;
  } catch {
    refreshFailures++;
    $("update-error").hidden = false;
    $("update-error-message").textContent = "暂时无法获取最新数据，正在自动重试。";
  } finally {
    refreshRunning = false;
    scheduleRefresh(Math.min(30000, 5000 * (refreshFailures + 1)));
  }
}

async function saveSettings() {
  // Validate before disabling fields; browser constraint validation ignores disabled controls.
  if (!$("settings-form").reportValidity()) return;
  const payload = settingsPayload();
  await perform($("save-settings"), "setup", async () => {
    const response = await bridge.apiPost("settings/save", payload);
    if (response.saved !== true) throw new Error(response.message || "设置未保存，请重试。");
    $("api-key").value = "";
    $("installation").open = false;
    await refreshBootstrap({ hydrate: true });
    activateTab("memories", true);
    notify(response.message || "设置已保存，正常聊天即可。");
  });
}

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void saveSettings().catch((error) => notify(errorMessage(error), true));
});
$("check-connection").addEventListener("click", () => void perform($("check-connection"), "setup", async () => {
  await checkConnection(connectionPayload());
  await refreshBootstrap();
}));
$("api-url").addEventListener("input", () => {
  const changed = data.config.api_url
    && $("api-url").value.trim().replace(/\/+$/, "") !== data.config.api_url.replace(/\/+$/, "");
  notify(changed
    ? "连接其他服务会使用独立记忆。旧记忆保存在原服务；同一服务搬迁请按数据维护指南操作。"
    : "保存设置时会检查连接。", false, "connection-result");
});
$("api-key").addEventListener("input", () => notify("保存设置时会检查新密钥。", false, "connection-result"));
$("add-account").addEventListener("click", () => addAccount().focus());
document.querySelectorAll('[name="connection-mode"]').forEach((radio) => radio.addEventListener("change", () => {
  $("installation").open = false;
  $("uninstallation").open = false;
  $("api-key").value = "";
  $("connection-result").hidden = true;
  $("api-url").value = radio.value === "managed" ? data.installation_api_url || "" : data.config.api_url || "";
  updateConnectionMode();
}));
async function prepareInstallation(button) {
  await perform(button, "setup", async () => {
    const response = await bridge.apiPost("install/prepare", {
      config_revision: settingsRevision,
      control_plane_url: controlPlaneUrl($("control-plane-url").value),
    });
    if (!response.command) throw new Error("未生成安装命令，请稍后重试。");
    if (response.config_revision) settingsRevision = response.config_revision;
    $("install-command").value = response.command;
    $("installation").hidden = false;
    $("installation").open = true;
    $("managed-settings").hidden = false;
    data.has_installation = true;
    data.installation_api_url = response.api_url;
    updateConnectionMode();
    if (response.api_url) $("api-url").value = response.api_url;
    if (response.control_plane_url && !$("control-plane-url").value.trim()) $("control-plane-url").value = controlPlaneUrl(response.control_plane_url);
    $("install-command").focus();
    notify("安装命令已生成。执行后在下方选择模型并保存。");
  });
}
$("prepare").addEventListener("click", () => void prepareInstallation($("prepare")));
$("reinstall").addEventListener("click", () => void prepareInstallation($("reinstall")));
$("save-models").addEventListener("click", () => void perform($("save-models"), "setup", async () => {
  modelRequestId ||= globalThis.crypto?.randomUUID?.() || `model-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const selection = { config_revision: settingsRevision, background: true, request_id: modelRequestId };
  for (const kind of ["chat", "embedding", "rerank"]) {
    const select = $(`${kind}-provider`);
    if ((kind !== "rerank" && !select.value) || select.selectedOptions[0]?.disabled) throw new Error("请选择可用的聊天和嵌入模型，重排可以留空。");
    selection[`${kind}_provider_id`] = select.value;
  }
  $("model-progress").hidden = false;
  $("model-progress").textContent = "正在提交模型配置…";
  let response;
  try {
    response = await bridge.apiPost("models/save", selection);
  } catch (error) {
    // A lost HTTP response does not prove the background operation failed.
    const status = await bridge.apiGet("models/status").catch(() => null);
    if (!status) {
      $("model-progress").textContent = "请求结果暂未确认，正在重新获取处理进度。";
      await watchModels(modelRequestId);
      return;
    }
    if (status.request_id !== modelRequestId && status.status !== "running") {
      modelRequestId = "";
      $("model-progress").textContent = `${errorMessage(error)} 请点击「验证并保存模型」重试。`;
      throw error;
    }
    response = status;
  }
  $("installation").open = false;
  await watchModels(response.request_id);
}));
$("copy-command").addEventListener("click", () => void perform($("copy-command"), "copy", async () => {
  const command = $("install-command");
  if (!command.value) throw new Error("请先生成安装命令。");
  command.focus();
  command.select();
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(command.value);
  } catch {
    if (!document.execCommand("copy")) throw new Error("浏览器限制了自动复制。命令已选中，请按 Ctrl+C，或长按选择复制。");
  }
  $("installation").open = false;
  notify("安装命令已复制。请在运行 AstrBot 的 Linux 主机终端执行，再选择模型并保存。");
}));
$("prepare-uninstall").addEventListener("click", () => void perform($("prepare-uninstall"), "setup", async () => {
  if (!$("delete-memory-data").checked) throw new Error("请先确认删除配套服务及全部记忆数据。");
  const response = await bridge.apiPost("uninstall/prepare", { config_revision: settingsRevision, delete_memory_data: true });
  $("uninstall-command").value = response.command;
  $("uninstallation").hidden = false;
  $("uninstallation").open = true;
}));
$("delete-memory-data").addEventListener("change", clearUninstall);
$("copy-uninstall").addEventListener("click", () => void perform($("copy-uninstall"), "copy", async () => {
  const field = $("uninstall-command");
  if (!field.value) throw new Error("请先生成卸载命令。");
  field.focus();
  field.select();
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(field.value);
  } catch {
    if (!document.execCommand("copy")) throw new Error("请按 Ctrl+C 复制选中的命令。");
  }
  $("uninstallation").open = false;
  notify("卸载命令已复制。请在运行 AstrBot 的 Linux 主机终端执行。");
}));
$("refresh").addEventListener("click", () => void perform($("refresh"), "refresh", async () => {
  const firstLoad = !data;
  await refreshBootstrap({ hydrate: firstLoad });
  if (firstLoad) activateTab(data.config.configured ? "memories" : "setup");
  notify("状态已刷新。");
  $("update-error").hidden = true;
  scheduleRefresh();
}));
$("go-setup").addEventListener("click", () => activateTab("setup", true));
$("dismiss-feedback").addEventListener("click", () => { $("feedback").hidden = true; clearTimeout(feedbackTimer); });
$("scope-picker").addEventListener("change", () => selectScope($("scope-picker").value));
$("scope-mode").addEventListener("change", () => { modeDirty = true; $("mode-help").textContent = modeHelp[$("scope-mode").value]; });
$("save-mode").addEventListener("click", () => void perform($("save-mode"), "memory", async () => {
  const mode = $("scope-mode").value;
  if (!modeLabels[mode]) throw new Error("请选择记忆模式。");
  const response = await bridge.apiPost(endpoint("action"), { action: "mode", mode });
  modeDirty = false;
  await refreshBootstrap();
  notify(response.message || `已切换为${modeLabels[mode]}。`);
}));
$("retry").addEventListener("click", () => void perform($("retry"), "memory", async () => {
  const response = await bridge.apiPost(endpoint("action"), { action: "retry" });
  await refreshBootstrap();
  notify(response.message || "已提交重试，处理结果会自动更新。");
}));
async function maintenanceAction(button, action, extra = {}) {
  await perform(button, "memory", async () => {
    const id = scopeId;
    maintenanceState = null;
    renderScope();
    const response = action === "maintenance_status"
      ? await bridge.apiGet(endpoint("action", id), { action })
      : await bridge.apiPost(endpoint("action", id), { action, ...extra });
    await refreshBootstrap({ pollMaintenance: action !== "maintenance_status" });
    if (scopeId === id && action === "maintenance_status") {
      maintenanceState = response.maintenance;
      renderScope();
    }
    if (action === "maintenance_begin") $("discard-pending").checked = false;
    notify(response.message || maintenanceDescription(data.scopes.find((item) => item.scope_id === id)?.summary, maintenanceState));
  });
}
$("maintenance-begin").addEventListener("click", () => void maintenanceAction($("maintenance-begin"), "maintenance_begin", { discard_pending: $("discard-pending").checked }));
$("maintenance-status").addEventListener("click", () => void maintenanceAction($("maintenance-status"), "maintenance_status"));
$("maintenance-resume").addEventListener("click", () => void maintenanceAction($("maintenance-resume"), "maintenance_resume"));

for (const id of ["control-plane-address", "scope-bank"]) $(id).addEventListener("focus", () => $(id).select());
for (const [details, field] of [["installation", "install-command"], ["uninstallation", "uninstall-command"]]) {
  $(details).addEventListener("toggle", () => { if (!$(details).open) $(field).value = ""; });
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearCommands();
});
window.addEventListener("pagehide", clearCommands);
function clearCommands() {
  $("installation").open = false;
  $("uninstallation").open = false;
  $("install-command").value = "";
  $("uninstall-command").value = "";
}

$("forget").addEventListener("click", () => void perform($("forget"), "memory", async () => {
  const id = scopeId;
  const response = await bridge.apiPost(endpoint("action", id), { action: "forget_prepare" });
  if (!response.confirmation) throw new Error("未取得遗忘确认信息，请重试。");
  forgetState = { id, confirmation: response.confirmation };
  $("forget-user").textContent = scopeName(data.scopes.find((scope) => scope.scope_id === id));
  $("forget-message").textContent = response.message || "";
  $("forget-feedback").textContent = "";
  $("forget-agree").checked = false;
  $("forget-agree").disabled = false;
  $("confirm-forget").disabled = true;
  $("forget-dialog").showModal();
}));
$("forget-agree").addEventListener("change", () => { $("confirm-forget").disabled = !$("forget-agree").checked; });
$("cancel-forget").addEventListener("click", () => { if (!locks.has("forget")) $("forget-dialog").close(); });
$("forget-dialog").addEventListener("cancel", (event) => { if (locks.has("forget")) event.preventDefault(); });
$("forget-dialog").addEventListener("close", () => { forgetState = null; $("forget-agree").checked = false; });
$("confirm-forget").addEventListener("click", () => {
  if (!forgetState || !$("forget-agree").checked) return;
  const pending = forgetState;
  void perform($("confirm-forget"), "forget", async () => {
    $("forget-agree").disabled = true;
    $("cancel-forget").disabled = true;
    try {
      const response = await bridge.apiPost(endpoint("action", pending.id), { action: "forget_confirm", confirmation: pending.confirmation });
      forgetState = null;
      $("forget-dialog").close();
      await refreshBootstrap();
      notify(response.message || "完整遗忘已登记。请等待旧 Bank 清理完成后，再开启记忆。");
    } finally {
      $("forget-agree").disabled = false;
      $("cancel-forget").disabled = false;
      // A failed confirmation may be expired. Require a fresh prepare instead of reusing it.
      if ($("forget-dialog").open) {
        forgetState = null;
        $("forget-agree").checked = false;
        $("forget-agree").disabled = true;
        $("forget-message").textContent = "操作未完成。请关闭此窗口，重新点击「完整遗忘并轮换 Bank」获取确认信息。";
      }
    }
  }, "forget-feedback").finally(() => { $("confirm-forget").disabled = true; });
});

async function start() {
  try {
    if (!bridge) throw new Error("请从 AstrBot 的插件详情页打开「记忆管理」。");
    const context = await bridge.ready();
    const theme = (value) => { document.documentElement.dataset.theme = (value || bridge.getContext?.())?.isDark ? "dark" : "light"; };
    theme(context);
    const off = bridge.onContext?.(theme);
    window.addEventListener("beforeunload", () => { off?.(); }, { once: true });
    await refreshBootstrap({ hydrate: true });
    $("refresh").disabled = false;
    activateTab(data.config.configured ? "memories" : "setup");
    scheduleRefresh();
  } catch (error) {
    $("startup").textContent = errorMessage(error);
    $("startup").classList.add("error");
    $("startup").setAttribute("role", "alert");
    $("refresh").disabled = !bridge;
    $("update-error").hidden = false;
    $("update-error-message").textContent = "页面加载失败，请重试。";
  }
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(refreshTimer);
  if (!document.hidden) scheduleRefresh(0);
});
window.addEventListener("beforeunload", () => clearTimeout(refreshTimer), { once: true });
void start();
