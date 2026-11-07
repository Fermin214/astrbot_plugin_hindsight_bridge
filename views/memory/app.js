const $ = (id) => document.getElementById(id);
let bridge = window.AstrBotPluginView;
const modeLabels = { active: "读写记忆", readonly: "只读记忆", paused: "停用记忆" };
let data = null;
let settingsRevision = "";
let parameterDraft = null;
const parameterKeys = ["background_max_tokens", "recall_max_tokens", "context_max_chars",
  "recall_timeout", "background_timeout", "recall_query_tokens", "query_tokenizer",
  "queue_max_tasks", "queue_max_mib", "command_prefixes"];
let scopeId = "";
let selectedAccount = "";
let maintenanceScopeId = "";
let forgetState = null;
let authorizationState = null;
let accountRemovalState = null;
let maintenanceState = null;
let maintenanceIdentity = "";
let maintenanceCheckedAt = 0;
let maintenanceExpiryTimer;
let maintenanceReadRevision = 0;
let backgroundReadRevision = 0;
let backgroundDraft = null;
let readingDraft = null;
let reflectDraft = null;
let bootstrapReadRevision = 0;
let versionCheckRevision = 0;
let versionCheck = null;
let refreshTimer;
let refreshRunning = false;
let refreshFailures = 0;
let modelRequestId = "";
let modelWatch = null;
let managedSetup = null;
let managedReadRevision = 0;
let statusSignature = "";
const locks = new Set();
let feedbackTimer;
let startRunning = false;
let viewReady = false;
let offContext;

async function withTimeout(work, milliseconds, message) {
  let timer;
  try {
    return await Promise.race([
      work,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

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
  if (/timeout|timed out/i.test(message)) return "请求超时，请稍后重试。";
  return message || "操作未完成，请稍后重试。";
}

async function perform(button, group, work, feedbackTarget = "feedback") {
  if (locks.has(group)) return;
  bootstrapReadRevision++;
  maintenanceReadRevision++;
  backgroundReadRevision++;
  locks.add(group);
  const fieldset = $(`${group}-fields`);
  if (fieldset) fieldset.disabled = true;
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = `正在${label}…`;
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
    if (group === "setup") renderServiceVersion();
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
  const providers = (managedMode() && managedSetup ? managedSetup.providers : data.providers)?.[kind] || [];
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
  for (const [kind, name] of [["chat", "记忆处理"], ["embedding", "向量"]]) {
    if (!((managedMode() && managedSetup ? managedSetup.providers : data.providers)?.[kind] || []).some((provider) => provider.supported !== false)) missing.push(name);
  }
  $("provider-help").textContent = missing.length ? `缺少可用的${missing.join("、")}模型，请到 AstrBot「模型供应商」添加。` : "";
  $("provider-help").hidden = !missing.length;
  $("save-models").disabled = missing.length > 0 || locks.has("setup") || !!data.backend?.pending || !!data.backend?.upgrade_paused;
}

function clearUninstall() {
  $("uninstallation").hidden = true;
  $("uninstall-command").value = "";
}

function managedMode() {
  return document.querySelector('[name="connection-mode"]:checked')?.value === "managed";
}

async function loadManagedSetup() {
  const revision = ++managedReadRevision;
  const response = await withTimeout(bridge.apiPost("managed/options", { mode: "managed", config_revision: settingsRevision }),
    10000, "获取配套安装记录超时，请重试。");
  if (revision !== managedReadRevision || !managedMode()) return;
  managedSetup = response;
  updateConnectionMode();
  $("api-url").value = response.api_url || "";
  for (const kind of ["chat", "embedding", "rerank"]) providerSelect(kind, response.selection?.[`${kind}_provider_id`]);
  updateProviderHelp();
  if (["running", "interrupted"].includes(response.job?.status) && !modelWatch) void watchModels(response.job.request_id, true);
}

function updateConnectionMode() {
  const managed = document.querySelector('[name="connection-mode"]:checked').value === "managed";
  $("setup-intro").hidden = !managed || (data?.config.configured === true
    && !!(managedSetup?.has_installation || data?.has_installation));
  $("managed-settings").hidden = !managed;
  $("models-settings").hidden = !managed;
  $("maintenance").hidden = !managed || !data?.has_installation;
  $("api-key-field").hidden = managed;
  $("api-url").readOnly = managed;
  $("api-url").required = !managed;
  $("save-settings").hidden = managed;
  $("save-access").hidden = !managed;
  $("check-connection").hidden = managed;
}

function renderServiceVersion() {
  const backend = data?.backend || {};
  $("service-version").hidden = !data;
  $("service-version-text").textContent = backend.version ? `Hindsight ${backend.version}` : "版本尚未确认";
  if (versionCheck && versionCheck.identity !== versionCheckIdentity()) { versionCheckRevision++; versionCheck = null; }
  $("version-check-result").hidden = !versionCheck;
  $("version-check-result").textContent = versionCheck?.message || "";
  $("version-check-result").classList.toggle("error", versionCheck?.error === true);
  const managed = data?.config.managed && backend.managed;
  const upgrading = managed && (!!backend.pending || !!backend.upgrade_paused);
  const available = managed && backend.upgrade_available === true;
  $("upgrade-notice").hidden = !managed || (!upgrading && (!available || backend.ignored === true));
  $("upgrade-notice-title").textContent = upgrading
    ? backend.resume_available ? "Hindsight 已升级，读写仍暂停" : "Hindsight 升级待完成"
    : `Hindsight 可升级：${backend.version} → ${backend.target}`;
  $("ignore-upgrade").hidden = upgrading;
  $("view-upgrade").textContent = upgrading ? "查看升级进度" : "查看升级";
  $("go-upgrade").hidden = !managed || (!available && !upgrading);
  $("go-upgrade").textContent = upgrading ? "查看升级进度" : "升级服务";
  $("service-upgrade").hidden = !managed || (!available && !upgrading);
  $("reinstall").disabled = locks.has("setup") || upgrading || !!(managed && available);
  $("upgrade-begin").disabled = locks.has("setup") || !available || upgrading;
  $("upgrade-prepare").disabled = locks.has("setup") || !available || !upgrading;
  $("upgrade-abandon").hidden = backend.version !== "1.1.0" || !upgrading;
  $("upgrade-cancel").disabled = locks.has("setup") || backend.version !== "1.1.0" || !upgrading;
  $("upgrade-verify").disabled = locks.has("setup") || !backend.pending;
  $("upgrade-resume").hidden = !backend.resume_available;
  $("upgrade-resume").disabled = locks.has("setup") || backend.version !== backend.target || !backend.resume_available;
  for (const id of ["save-settings", "add-account", "save-account", "remove-account", "prepare-uninstall"]) $(id).disabled = upgrading || locks.has("setup");
  updateProviderHelp();
}

function versionCheckIdentity() {
  return JSON.stringify([data?.config.connection_revision, $("api-url").value,
    document.querySelector('[name="connection-mode"]:checked')?.value, data?.installation_api_url]);
}

async function checkServiceVersion() {
  const revision = ++versionCheckRevision;
  const identity = versionCheckIdentity();
  versionCheck = { identity, message: "正在检查 Hindsight 版本…" };
  renderServiceVersion();
  try {
    const request = managedMode() && managedSetup?.installation_id
      ? bridge.apiPost("managed/version", { mode: "managed", installation_id: managedSetup.installation_id })
      : bridge.apiGet("service/status");
    const backend = await withTimeout(request, 10000, "检查版本超时，请重试。");
    if (revision !== versionCheckRevision || identity !== versionCheckIdentity()) return;
    let message = backend.message || "";
    if (!message && !backend.version) message = "版本尚未确认，请先完成服务连接后重试。";
    if (!message && backend.version) {
      if (!backend.managed) message = `检查完成：Hindsight ${backend.version}${backend.supported === false ? "，插件未确认兼容此版本" : ""}。`;
      else if (backend.version === backend.target) message = `已是插件支持的配套版本 ${backend.target}。`;
      else if (backend.upgrade_available) message = `检查完成：可从 ${backend.version} 升级到插件支持的配套版本 ${backend.target}。`;
      else message = `检查完成：Hindsight ${backend.version}，插件支持的配套版本为 ${backend.target}；此版本未确认兼容，不执行降级。`;
    }
    versionCheck = { identity, message, error: !!backend.message || !backend.version };
    data.backend = backend;
  } catch (error) {
    if (revision !== versionCheckRevision || identity !== versionCheckIdentity()) return;
    versionCheck = { identity, message: errorMessage(error), error: true };
  } finally {
    if (revision === versionCheckRevision) renderServiceVersion();
  }
}

async function upgradeAction(action, button) {
  await perform(button, "setup", async () => {
    if (action === "prepare" && !$("upgrade-backup-confirmed").checked) throw new Error("请先完成插件数据备份，并确认已停止其他写入。");
    const response = await bridge.apiPost("service/upgrade", {
      action, config_revision: settingsRevision, backup_confirmed: $("upgrade-backup-confirmed").checked,
    });
    if (response.config_revision) settingsRevision = response.config_revision;
    if (response.command) {
      $("upgrade-command").value = response.command;
      $("upgrade-installation").hidden = false;
    }
    if (["cancel", "verify", "resume"].includes(action)) {
      $("upgrade-command").value = "";
      $("upgrade-installation").hidden = true;
      $("upgrade-backup-confirmed").checked = false;
    }
    notify(response.message, false, "upgrade-result");
    await refreshBootstrap({ automatic: true, pollMaintenance: false });
    renderServiceVersion();
  }, "upgrade-result");
  renderServiceVersion();
}

function refreshAccountPlatforms() {
  const select = $("account-platform");
  const previous = select.value;
  select.replaceChildren(option("", "请选择平台实例"));
  (data.platforms || []).forEach((platform) => select.append(option(platform.id, platform.name || platform.id,
    platform.supported === false || platform.enabled === false)));
  if ((data.platforms || []).some((platform) => platform.id === previous)) select.value = previous;
  updateAccountHint();
}

function updateAccountHint() {
  const platform = data?.platforms?.find((item) => item.id === $("account-platform").value);
  const input = $("account-user");
  $("account-user-label").textContent = platform?.user_label || "用户 ID";
  input.inputMode = platform?.numeric_user_id ? "numeric" : "text";
  if (platform?.numeric_user_id) { input.pattern = "[0-9]+"; input.maxLength = 20; }
  else { input.removeAttribute("pattern"); input.removeAttribute("maxlength"); }
  input.placeholder = platform?.user_hint || "填写用户 ID";
}

function addAccount() {
  $("account-form").reset();
  refreshAccountPlatforms();
  $("account-feedback").hidden = true;
  $("account-dialog").showModal();
}

function hydrateConfig() {
  const config = data.config || {};
  settingsRevision = data.config_revision;
  $("api-url").value = config.api_url || "";
  $("api-key").value = "";
  $("control-plane-url").value = config.control_plane_url || "";
  $("connection-result").hidden = true;
  $("api-key").placeholder = config.has_api_key ? "留空沿用已保存的密钥" : "填写服务 API 密钥";
  document.querySelector(`[name="connection-mode"][value="${config.managed || !config.api_url ? "managed" : "existing"}"]`).checked = true;
  managedSetup = config.managed ? { installation_id: data.installation_id, api_url: data.installation_api_url,
    providers: data.providers, has_installation: data.has_installation } : null;
  updateConnectionMode();
  for (const kind of ["chat", "embedding", "rerank"]) providerSelect(kind, config[`${kind}_provider_id`]);
  refreshAccountPlatforms();
  updateProviderHelp();
}


function renderParameters({ discard = false } = {}) {
  const snapshot = data?.parameters;
  if (!snapshot) return;
  if (discard || !parameterDraft || !parameterDraft.dirty) {
    parameterDraft = { revision: data.config_revision, dirty: false };
    for (const key of parameterKeys) {
      $("memory-" + key).value = key === "command_prefixes"
        ? (Array.isArray(snapshot.values[key]) ? snapshot.values[key].join("\n") : "") : String(snapshot.values[key] ?? "");
    }
  }
  $("parameters-fields").disabled = locks.has("parameters");
  $("parameters-conflict").hidden = parameterDraft.revision === data.config_revision;
  const labels = { service_id: "记忆服务识别码", agent_id: "记忆归属识别码",
    service_address_revision: "服务地址变更序号", control_state_dir: "控制目录" };
  $("deployment-values").replaceChildren(...Object.entries(snapshot.deployment).flatMap(([key, value]) =>
    [el("dt", "", labels[key] || key), el("dd", "", value || (key === "control_state_dir"
      ? "data/plugin_data/astrbot_plugin_hindsight_bridge_control" : "未设置"))]));
  renderParameterLimits();
}

function renderParameterLimits() {
  const page = Number($("memory-background_max_tokens").value);
  const shared = Number($("memory-recall_max_tokens").value);
  const seconds = Number($("memory-background_timeout").value);
  const total = Number($("memory-recall_timeout").value);
  const messages = [];
  if (page > shared) messages.push("知识页长度超过记忆参考长度。");
  if (seconds > total / 4) messages.push(`知识页有效超时 ≤ ${total / 4} 秒。`);
  $("parameters-limits").textContent = messages.join(" ");
  $("parameters-limits").hidden = !messages.length;
}

function parameterPayload() {
  const parameters = {};
  for (const key of parameterKeys) {
    const raw = $("memory-" + key).value;
    if (key === "command_prefixes") parameters[key] = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    else if (key === "query_tokenizer") parameters[key] = raw.trim();
    else {
      const value = Number(raw);
      if (!raw.trim() || !Number.isFinite(value) || (!key.endsWith("timeout") && !Number.isSafeInteger(value))) {
        throw new Error("记忆参数须填写有效数值；数量和长度须为整数。");
      }
      parameters[key] = value;
    }
  }
  return { config_revision: parameterDraft.revision, parameters };
}

async function saveParameters() {
  if (!parameterDraft || !$("parameters-form").reportValidity()) return;
  const payload = parameterPayload();
  await perform($("save-parameters"), "parameters", async () => {
    const response = await withTimeout(bridge.apiPost("parameters/save", payload), 25000,
      "保存超时，请刷新后确认结果。");
    if (!response.saved || !response.parameters || !response.config_revision) throw new Error(response.message || "记忆设置未保存，请重试。");
    if (data.config_revision !== payload.config_revision && data.config_revision !== response.config_revision) {
      await refreshBootstrap({ automatic: true, pollMaintenance: false });
      throw new Error("设置已更新，请刷新页面。");
    }
    if (settingsRevision === payload.config_revision) settingsRevision = response.config_revision;
    data.parameters = response.parameters;
    data.config_revision = response.config_revision;
    renderParameters({ discard: true });
    if (!backgroundDraft?.dirty) backgroundDraft = null;
    notify("已保存", false, "parameters-feedback");
    await refreshBootstrap({ automatic: true, pollMaintenance: false });
  }, "parameters-feedback");
}

function accountKey(account) {
  return JSON.stringify([account.platform_instance_id, account.user_id]);
}

function accountScopes(key = selectedAccount) {
  const account = (data?.config.allowed_accounts || []).find((item) => accountKey(item) === key);
  if (!account) return [];
  const scopes = data.scopes.filter((item) => accountKey(item) === key);
  const primary = scopes.find((item) => !item.pending && !item.historical) || scopes.find((item) => item.pending);
  return [primary, ...scopes.filter((item) => item !== primary
    && ["historical", "disconnected", "confirmation"].includes(item.availability))].filter(Boolean);
}

function updateUserChoices() {
  const accounts = data.config.allowed_accounts || [];
  if (!accounts.some((item) => accountKey(item) === selectedAccount)) selectedAccount = accounts[0] ? accountKey(accounts[0]) : "";
  const choices = accounts.map((account) => {
    const platform = data.platforms?.find((item) => item.id === account.platform_instance_id);
    return [accountKey(account), `${account.user_id} · ${platform?.name || account.platform_instance_id}`];
  });
  $("scope-picker").replaceChildren(...(choices.length ? choices : [["", "还没有用户"]]).map(([id, name]) => option(id, name)));
  $("scope-picker").value = selectedAccount;
  $("scope-picker").disabled = !accounts.length;
  const scopes = accountScopes();
  if (!scopes.some((item) => item.scope_id === scopeId)) scopeId = scopes[0]?.scope_id || "";
  $("bank-picker").replaceChildren(...scopes.map((item) => option(item.scope_id,
    item.pending ? item.pending_reason === "platform" ? "等待连接" : "等待开通"
      : `${item.availability === "historical" ? "旧记忆库 · " : ""}机器人 ${item.bot_id}${item.availability === "disconnected" ? "（未连接）" : item.availability === "confirmation" ? "（身份待确认）" : ""}`)));
  $("bank-picker").value = scopeId;
  $("bank-range-field").hidden = scopes.length < 2;
  $("memory-fields").hidden = !accounts.length;
  $("no-scopes").hidden = !!accounts.length;
}

function scopeName(scope) {
  if (!scope) return "记忆库";
  const platform = data?.platforms?.find((item) => item.id === scope.platform_instance_id);
  return `${scope.user_id} · ${platform?.name || scope.platform_instance_id}${scope.bot_id ? ` · 机器人 ${scope.bot_id}` : ""}`;
}

function countDescription(counts) {
  if (counts == null) return "暂时无法获取保存进度";
  if (typeof counts === "number") return `待处理记录：${counts}`;
  const labels = {
    pending: "等待保存", running: "正在保存", processing: "正在保存", queued: "等待保存",
    failed: "失败", error: "错误", retry: "待重试", retrying: "重试中",
    completed: "已保存", done: "已保存", succeeded: "已保存", total: "总计",
    retained: "已保存", cancelled: "已取消", dead: "失败",
    ready: "等待发送", submitting: "正在发送", unknown: "正在确认保存结果", tracking: "Hindsight 正在整理",
    deferred: "暂停发送，继续确认保存结果",
    reconcile_required: "需要检查保存结果", discarded: "已放弃保存", collecting: "正在记录", held: "等待机器人回复",
    cancel_requested: "正在取消",
  };
  return Object.entries(counts).map(([key, value]) => `${labels[key] || key}：${text(value)}`).join("，") || "暂无等待保存的记录";
}

function renderStatus() {
  const status = data.status || {};
  const connected = status.connected;
  $("status-label").textContent = status.recovering ? "恢复中" : connected === true ? "已连接" : status.label || (connected === false ? "服务未连接" : "连接状态未知");
  $("status-dot").className = `status-dot${!status.recovering && connected === true ? " good" : connected === false ? " bad" : ""}`;
  let description = status.recovering ? "正在核对记忆库与保存任务。" : connected !== true ? status.message || "请检查服务地址、密钥和服务运行状态。" : "";
  if (data.config.managed && data.managed_service?.installed && !status.recovering && !data.managed_service.ready) description = "服务已安装，请配置记忆处理模型和向量模型。";
  $("status-description").textContent = description;
  $("status-description").hidden = !description;
  const setup = $("panel-setup");
  const connection = $("setup-fields");
  if (connected === true || data.config.configured) {
    if (setup.lastElementChild !== connection) setup.append(connection);
  } else if (setup.firstElementChild !== connection) setup.prepend(connection);
  const attention = !!status.recovering || !data.config.configured || connected !== true;
  $("service-alert").hidden = !attention;
  $("service-alert-message").textContent = attention ? description || $("status-label").textContent : "";
}

function recallDescription(recall) {
  if (!recall) return "尚未检索";
  const labels = { success: "成功", empty: "无相关结果", error: "失败", skipped: "已跳过", on_demand: "按需读取" };
  const label = recall.outcome === "error" && recall.code?.includes("timeout") ? "超时" : labels[recall.outcome] || "结果未知";
  let result = `${new Date(recall.last_at * 1000).toLocaleString()} · ${label}`;
  if (recall.error_message) result += `：${recall.error_message}`;
  return result;
}

async function refreshBootstrap({ hydrate = false, automatic = false, pollMaintenance = true } = {}) {
  const revision = ++bootstrapReadRevision;
  let next;
  try {
    next = await withTimeout(bridge.apiGet("bootstrap"), 15000, "获取插件状态超时，请点击重新获取。");
    if (!next || !next.config || !Array.isArray(next.scopes)) throw new Error("设置数据不完整，请刷新页面重试。");
  } catch (error) {
    if (revision !== bootstrapReadRevision) return;
    if (maintenanceIdentity) { maintenanceReadRevision++; maintenanceState = { ready: false, error: errorMessage(error) }; renderMaintenance(); }
    throw error;
  }
  if (revision !== bootstrapReadRevision) return;
  if (settingsRevision && next.config_revision !== settingsRevision) clearCommands();
  data = next;
  renderParameters();
  if (managedSetup?.installation_id && data.config.managed && data.installation_id === managedSetup.installation_id) managedSetup.providers = data.providers;
  if (hydrate) hydrateConfig();
  else if (!automatic || document.activeElement?.tagName !== "SELECT") {
    for (const kind of ["chat", "embedding", "rerank"]) {
      const select = $(`${kind}-provider`);
      const version = JSON.stringify(next.providers?.[kind] || []);
      if (!automatic || select.dataset.providers !== version) { providerSelect(kind, select.value); select.dataset.providers = version; }
    }
    if (!$("account-dialog").open) refreshAccountPlatforms();
    updateConnectionMode();
  }
  const nextStatusSignature = JSON.stringify([data.config.configured, data.status, data.managed_service]);
  if (!automatic || nextStatusSignature !== statusSignature) { renderStatus(); statusSignature = nextStatusSignature; }
  const previous = scopeId;
  updateUserChoices();
  $("memory-fields").disabled = !scopeId || locks.has("memory");
  $("setup-fields").disabled = locks.has("setup");
  if (previous !== scopeId) backgroundDraft = null;
  if (!locks.has("memory")) renderScope();
  const configured = new Set((data.config.allowed_accounts || []).map(accountKey));
  $("paused-banks-notice").hidden = !data.scopes.some((item) => !configured.has(accountKey(item))
    && (item.summary?.maintenance || item.summary?.bank_missing));
  if (pollMaintenance) await readMaintenanceStatus();
  renderServiceVersion();
  $("settings-conflict").hidden = !settingsRevision || data.config_revision === settingsRevision;
  $("startup").hidden = true;
  $("update-error").hidden = true;
  if (["running", "interrupted"].includes(data.connection_job?.status) && managedMode() && !modelWatch) void watchModels(data.connection_job.request_id, true);
  else if (data.model_job?.status === "running" && !modelWatch) void watchModels();
  else if (managedMode() && data.connection_job?.config_revision === data.config_revision && data.connection_job?.message) {
    $("model-progress").hidden = false;
    $("model-progress").textContent = data.connection_job.message;
  }
}

function maintenanceDescription(summary = {}, state = null) {
  if (summary.bank_missing || state?.bank_missing) return "找不到原记忆库，读写已暂停。恢复原库后点击「核对记忆库」，或通过「清空记忆」重新开始。";
  if (!summary.maintenance) return "";
  if (state?.error) return `无法确认保存进度：${state.error} 请检查服务后点击「重试检查」。`;
  if (state?.phase === "inactive") return "暂停已解除，正在更新用户状态。";
  if (state?.ready === true && state.phase === "ready") return "可以修改记忆";
  if (state?.pending) return summary.can_discard
    ? `仍有 ${state.pending} 条记录未保存；请等待已接收任务完成，或在「放弃记录」中处理未发送及失败记录。`
    : `仍有 ${state.pending} 条记录未保存，正在确认已发送请求的处理结果。`;
  if (state?.in_flight || state?.remote_active) return `正在等待 Hindsight 处理已接收的记录${state.remote_active ? `（${state.remote_active} 个任务）` : ""}。`;
  return "正在确认保存结果…";
}

function maintenanceSnapshot(scope, snapshot = data) {
  if (!scope?.summary?.maintenance || scope.pending || scope.summary.bank_missing || snapshot?.status?.recovering) return "";
  const record = scope.summary.maintenance;
  return JSON.stringify([
    scope.scope_id, scope.bank, scope.owner_id, scope.generation, scope.revision,
    snapshot.config.connection_revision, snapshot.config.api_url,
    record.connection_id, record.started_at, record.discard_pending,
  ]);
}

function syncMaintenanceState() {
  const scope = maintenanceScope();
  const identity = maintenanceSnapshot(scope);
  if (identity !== maintenanceIdentity) {
    maintenanceReadRevision++;
    maintenanceIdentity = identity;
    maintenanceState = null;
    maintenanceCheckedAt = 0;
    $("discard-pending").checked = false;
  } else if (maintenanceState?.ready && Date.now() - maintenanceCheckedAt > 30000) {
    maintenanceState = { ready: false, error: "暂停检查结果已过期。" };
  }
  if (identity && data.status?.connected === false) {
    maintenanceState = { ready: false, error: data.status.message || "记忆服务未连接。" };
  }
  if (!maintenanceState?.ready) clearTimeout(maintenanceExpiryTimer);
}

function scheduleMaintenanceExpiry() {
  clearTimeout(maintenanceExpiryTimer);
  if (!maintenanceIdentity || !maintenanceState?.ready || maintenanceState.phase !== "ready") return;
  const identity = maintenanceIdentity;
  const result = maintenanceState;
  const checkedAt = maintenanceCheckedAt;
  maintenanceExpiryTimer = setTimeout(() => {
    if (identity !== maintenanceIdentity || result !== maintenanceState || checkedAt !== maintenanceCheckedAt) return;
    maintenanceState = { ready: false, error: "暂停检查结果已过期。" };
    renderMaintenance();
    renderScope();
  }, Math.max(0, 30000 - (Date.now() - checkedAt)));
}

async function readMaintenanceStatus() {
  syncMaintenanceState();
  const revision = ++maintenanceReadRevision;
  const identity = maintenanceIdentity;
  const id = maintenanceScope()?.scope_id;
  if (!identity) { renderMaintenance(); return; }
  let state;
  try {
    const response = await withTimeout(
      bridge.apiGet(endpoint("action", id), { action: "maintenance_status" }),
      15000, "获取暂停进度超时。",
    );
    state = response.maintenance;
    if (!state || typeof state.ready !== "boolean") throw new Error("服务未返回有效的暂停进度。");
    const scope = data?.scopes.find((item) => item.scope_id === id);
    const record = scope?.summary?.maintenance;
    if (state.bank && (state.bank !== scope?.bank || state.connection_id !== record?.connection_id
        || state.started_at !== record?.started_at || state.discard_pending !== record?.discard_pending)) {
      throw new Error("暂停范围已变化，请重新获取用户状态。");
    }
  } catch (error) {
    state = { phase: "draining", ready: false, error: errorMessage(error) };
  }
  if (revision !== maintenanceReadRevision || identity !== maintenanceIdentity || id !== maintenanceScope()?.scope_id) return;
  maintenanceState = state;
  maintenanceCheckedAt = Date.now();
  renderMaintenance();
  renderScope();
  scheduleMaintenanceExpiry();
}

function renderScope() {
  renderMaintenance();
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const summary = scope?.summary || {};
  const account = (data?.config.allowed_accounts || []).find((item) => accountKey(item) === selectedAccount);
  $("user-bot").textContent = scope?.bot_id ? `机器人 ${scope.bot_id}` : "";
  $("user-availability").textContent = { historical: "旧记忆库", disconnected: "机器人未连接", confirmation: "身份待确认" }[scope?.availability] || "";
  $("pending-account").hidden = !scope?.pending;
  $("pending-account-message").textContent = scope?.pending ? pendingScopeHelp(scope) : "";
  $("authorize-account").hidden = scope?.pending_reason !== "authorization";
  const unavailable = !scope || scope.pending || scope.historical || data?.status?.recovering;
  $("mode-control").hidden = !!scope?.historical;
  $("historical-mode").hidden = !scope?.historical;
  $("historical-mode").textContent = scope?.historical ? `原记忆模式：${modeLabels[scope.mode] || scope.mode}` : "";
  for (const mode of ["active", "readonly", "paused"]) {
    const button = document.querySelector(`[data-mode="${mode}"]`);
    button.setAttribute("aria-pressed", String(scope?.mode === mode));
    button.disabled = !!unavailable || locks.has("memory") || (mode !== "paused" && !!(summary.maintenance || summary.bank_missing));
  }
  $("forget").disabled = !scope || !!scope.pending || !!data?.status?.recovering || locks.has("memory");
  $("remove-account").disabled = !account || locks.has("setup") || !!data?.backend?.pending || !!data?.backend?.upgrade_paused;
  $("scope-bank").value = scope?.bank || "";
  $("copy-bank").disabled = !scope?.bank;
  $("bank-state").textContent = scope?.bank_state === "new" ? "等待首次保存" : "";
  $("bank-state").hidden = !$("bank-state").textContent;
  const counts = summary.counts || {};
  $("saved-count").textContent = Number.isSafeInteger(summary.saved_count) && summary.saved_count >= 0 ? summary.saved_count : !scope || scope.pending ? "待开通" : "暂不可用";
  $("pending-count").textContent = ["ready", "submitting", "unknown", "tracking", "deferred", "reconcile_required", "held", "cancel_requested"].reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
  $("failed-count").textContent = (Number(counts.failed) || 0) + (Number(counts.dead) || 0);
  $("queue-summary").textContent = counts.reconcile_required ? `有 ${counts.reconcile_required} 条记录需要确认保存结果。` : "";
  $("queue-summary").hidden = !$("queue-summary").textContent;
  $("scope-error").hidden = !summary.error_message;
  $("scope-error").textContent = summary.error_message || "";
  $("retry").disabled = !!unavailable || !!summary.maintenance || !!summary.bank_missing || locks.has("memory");
  $("retry").hidden = $("retry").disabled || !(counts.failed || counts.dead);
  $("scope-recall-status").textContent = recallDescription(summary.recall);
  $("maintenance-state").textContent = summary.bank_missing ? "找不到原记忆库，读写已暂停。请恢复原库，或清空旧记忆后重新开始。" : summary.maintenance ? "这个记忆库的插件读写已暂停。" : "";
  $("maintenance-state").hidden = !$("maintenance-state").textContent;
  $("maintenance-view").hidden = !summary.maintenance && !summary.bank_missing;
  renderBackground();
  renderReading();
  renderReflect();
}

function renderReading() {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const saved = scope?.reading || { revision: 0, mode: "automatic" };
  const snapshot = JSON.stringify([scopeId, scope?.bank, saved.revision, saved.scope_revision, data?.config_revision]);
  if (!readingDraft || readingDraft.id !== scopeId || readingDraft.bank !== scope?.bank || !readingDraft.dirty) {
    if (!readingDraft || readingDraft.id !== scopeId || readingDraft.bank !== scope?.bank) $("reading-feedback").hidden = true;
    readingDraft = { snapshot, id: scopeId, bank: scope?.bank, revision: saved.revision,
      scope_revision: saved.scope_revision, config_revision: data?.config_revision, dirty: false };
    $("reading-mode").value = saved.mode || "automatic";
  }
  const select = $("reading-mode");
  select.disabled = !scope || !!scope.pending || !!scope.historical || !!data?.status?.recovering
    || !!saved.error || locks.has("memory");
  const conflict = readingDraft.snapshot !== snapshot;
  $("reading-save").disabled = select.disabled || conflict || !readingDraft.dirty;
  const status = saved.status || {};
  const messages = [];
  if (conflict) messages.push("设置已更新，请刷新页面。");
  else if (saved.error) messages.push(saved.error);
  else if (saved.invalidated) messages.push("记忆库已变化，已切回自动召回。");
  else if (saved.mode === "tools") {
    if (status.state === "error") messages.push(status.message || "工具读取失败");
    else if (status.state === "budget") messages.push("本条消息的工具额度已用完");
    else if (status.state === "unavailable") messages.push("此会话暂不支持按需工具");
  }
  $("reading-status").textContent = messages.join(" ");
  $("reading-status").hidden = !messages.length;
}

async function saveReading() {
  const draft = readingDraft;
  if (!draft) return;
  let saved = false;
  const payload = { mode: $("reading-mode").value, revision: draft.revision,
    scope_revision: draft.scope_revision, config_revision: draft.config_revision };
  await perform($("reading-save"), "memory", async () => {
    const response = await bridge.apiPost(endpoint("reading", draft.id), payload);
    if (readingDraft !== draft || scopeId !== draft.id) return;
    draft.dirty = false;
    await refreshBootstrap();
    saved = true;
  }, "reading-feedback");
  if (saved && scopeId === draft.id) notify("已保存", false, "reading-feedback");
}

function renderReflect({ discard = false } = {}) {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const saved = scope?.reflect || { revision: 0, enabled: false, timeout: 12 };
  const snapshot = JSON.stringify([scopeId, scope?.bank, saved.revision, saved.scope_revision, data?.config_revision]);
  const changedScope = !reflectDraft || reflectDraft.id !== scopeId || reflectDraft.bank !== scope?.bank;
  if (discard || changedScope || !reflectDraft.dirty) {
    reflectDraft = { snapshot, id: scopeId, bank: scope?.bank, revision: saved.revision,
      scope_revision: saved.scope_revision, config_revision: data?.config_revision, dirty: false };
    $("reflect-enabled").checked = !!saved.enabled;
    $("reflect-timeout").value = String(saved.timeout ?? 12);
    if (discard || changedScope) $("reflect-feedback").hidden = true;
  }
  const conflict = reflectDraft.snapshot !== snapshot;
  const unavailable = !scope || scope.pending || scope.historical || data?.status?.recovering || saved.error || locks.has("memory");
  $("reflect-enabled").disabled = !!unavailable || (!saved.enabled && !!scope?.summary?.maintenance);
  $("reflect-timeout").disabled = !!unavailable;
  $("reflect-save").disabled = !!unavailable || conflict || !reflectDraft.dirty;
  $("reflect-conflict").hidden = !conflict;
  const unsupported = saved.enabled && saved.status?.state === "unavailable";
  const messages = [];
  if (saved.error) messages.push("Reflect 设置读取失败");
  else if (saved.invalidated) messages.push("记忆库已变化，Reflect 已关闭。");
  if (saved.unknown) messages.push("请求状态待确认，暂时无法清空记忆。");
  else if (saved.pending) messages.push("Reflect 处理中…");
  else if (unsupported) messages.push("此会话暂不支持 Reflect");
  else if (saved.enabled && ["error", "budget"].includes(saved.status?.state)) {
    messages.push(saved.status.message || "Reflect 调用失败");
  }
  $("reflect-status").textContent = messages.join(" ");
  $("reflect-status").hidden = !messages.length;
}

async function saveReflect() {
  const draft = reflectDraft;
  if (!draft) return;
  await perform($("reflect-save"), "memory", async () => {
    const timeout = Number($("reflect-timeout").value);
    if (!Number.isSafeInteger(timeout) || timeout < 3 || timeout > 30) throw new Error("Reflect 等待须为 3 到 30 秒的整数。");
    const payload = { enabled: $("reflect-enabled").checked, timeout, revision: draft.revision,
      scope_revision: draft.scope_revision, config_revision: draft.config_revision };
    const response = await bridge.apiPost(endpoint("reflect", draft.id), payload);
    if (reflectDraft !== draft || scopeId !== draft.id) return;
    draft.dirty = false;
    await refreshBootstrap();
    if (scopeId === draft.id) notify("已保存", false, "reflect-feedback");
  }, "reflect-feedback");
}

function backgroundSnapshot(scope) {
  return JSON.stringify([
    scope?.scope_id, scope?.bank, scope?.background?.scope_revision, scope?.background?.revision,
    data?.config_revision,
    !!scope?.historical, !!scope?.pending, !!scope?.summary?.maintenance, !!scope?.summary?.bank_missing,
  ]);
}

function backgroundPages(selection) {
  return selection?.pages || (selection?.page_id ? [{ page_id: selection.page_id }] : []);
}

function renderBackground() {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const saved = scope?.background || { revision: 0, enabled: false, page_id: "" };
  const snapshot = backgroundSnapshot(scope);
  const changedScope = !backgroundDraft || backgroundDraft.id !== scopeId || backgroundDraft.bank !== scope?.bank;
  if (changedScope || (!backgroundDraft.dirty && backgroundDraft.snapshot !== snapshot)) {
    backgroundReadRevision++;
    backgroundDraft = { snapshot, id: scopeId, bank: scope?.bank, page_id: saved.page_id || "", enabled: saved.enabled === true,
      revision: saved.revision, scope_revision: saved.scope_revision, config_revision: data?.config_revision,
      selected: backgroundPages(saved).map((page) => ({ ...page })), dirty: false,
      pages: [], check: null, checks: [], loaded: false, loading: false, attempted: false };
    $("background-feedback").hidden = true;
  }
  const unavailable = !scope || scope.pending || scope.historical || data?.status?.recovering || saved.error;
  const blocked = unavailable || scope.summary?.maintenance || scope.summary?.bank_missing || scope.bank_state === "new";
  const select = $("background-page");
  const draft = backgroundDraft;
  const conflict = draft.snapshot !== snapshot;
  const selected = draft.selected;
  select.disabled = !!unavailable || locks.has("memory") || (blocked && !saved.enabled);
  const options = [option("", "不使用知识页")];
  backgroundDraft.pages.forEach((page) => options.push(option(page.page_id, page.path, !!blocked)));
  if (draft.page_id && !draft.pages.some((page) => page.page_id === draft.page_id)) options.push(option(draft.page_id, draft.page_id, !!blocked));
  select.replaceChildren(...options);
  select.value = draft.enabled ? draft.page_id : "";
  const rows = selected.map((page, index) => {
    const row = el("li", "knowledge-page");
    const native = draft.pages.find((item) => item.page_id === page.page_id);
    row.append(el("div", "knowledge-page-title", `${index + 1}. ${native?.path || page.page_id}`));
    const check = draft.enabled ? draft.checks.find((item) => item.page_id === page.page_id) : null;
    const usage = saved.status?.pages?.find((item) => item.page_id === page.page_id);
    if (check?.tokens != null) {
      const limit = check.limits?.tokens;
      const exceeded = limit != null && check.tokens > limit;
      row.append(el("span", `page-metrics${exceeded ? " error" : ""}`, limit == null
        ? `${check.tokens} token` : `${check.tokens} / ${limit} token${exceeded ? "（超限）" : ""}`));
      if (check.limits?.chars != null && check.chars > check.limits.chars) {
        row.append(el("span", "page-metrics error", `${check.chars} / ${check.limits.chars} 字符（超限）`));
      }
    }
    if (check?.code && check.code !== "background_too_large") row.append(el("p", "operation-status error", check.message));
    if (!draft.dirty && saved.enabled && usage) row.append(el("span", "page-status", usage.used ? "已使用" : usage.message));
    if (native?.index_stale === true && draft.enabled) row.append(el("span", "page-status", "有新记忆待刷新"));
    const actions = el("div", "knowledge-page-actions");
    for (const [label, direction] of [["上移", -1], ["下移", 1], ["移除", 0]]) {
      const button = el("button", "", label);
      button.type = "button";
      button.setAttribute("aria-label", `${label}第 ${index + 1} 篇知识页`);
      button.disabled = !!blocked || locks.has("memory") || (direction === -1 && index === 0)
        || (direction === 1 && index === selected.length - 1);
      button.addEventListener("click", () => {
        if (backgroundDraft !== draft) return;
        if (direction === 0) draft.selected.splice(index, 1);
        else [draft.selected[index], draft.selected[index + direction]] = [draft.selected[index + direction], draft.selected[index]];
        draft.page_id = draft.selected[0]?.page_id || "";
        draft.enabled = draft.enabled && draft.selected.length > 0;
        draft.dirty = true;
        $("background-feedback").hidden = true;
        renderBackground();
      });
      actions.append(button);
    }
    row.append(actions);
    return row;
  });
  $("background-selected").replaceChildren(...rows);
  const status = conflict ? "设置已更新，请刷新页面。" : saved.error || (saved.invalidated ? saved.message || "知识页选择已失效" : "");
  $("background-status").textContent = status;
  $("background-status").hidden = !status;
  $("background-recheck").disabled = !!blocked || backgroundDraft.loading || locks.has("memory");
  $("background-recheck").textContent = draft.loading ? "刷新中…" : "刷新知识页";
  $("background-save").disabled = !!unavailable || locks.has("memory") || conflict || !draft.dirty;
  if (viewReady && !blocked && !backgroundDraft.attempted && !locks.has("memory")) void loadBackground();
}

async function loadBackground({ manual = false } = {}) {
  const draft = backgroundDraft;
  if (!draft || draft.loading) return;
  const revision = ++backgroundReadRevision;
  draft.loading = true;
  draft.attempted = true;
  renderBackground();
  try {
    const response = await withTimeout(bridge.apiGet(endpoint("background", draft.id)), 8500, "获取知识页列表超时，请刷新页面重试。");
    if (revision !== backgroundReadRevision || backgroundDraft !== draft || scopeId !== draft.id) return;
    if (!Array.isArray(response.pages) || !response.selection) throw new Error("知识页列表无法读取，请刷新页面重试。");
    if (response.selection.revision !== draft.revision || response.scope_revision !== draft.scope_revision || response.config_revision !== draft.config_revision) throw new Error("记忆范围或知识页选择已变化，请重新获取用户状态。");
    draft.pages = response.pages;
    draft.check = response.check || null;
    draft.checks = response.checks || [];
    draft.loaded = true;
    renderBackground();
    if (manual) notify(response.pages.length ? `已刷新 ${response.pages.length} 篇` : "暂无知识页", false, "background-feedback");
  } catch (error) {
    if (revision === backgroundReadRevision && backgroundDraft === draft && scopeId === draft.id) {
      notify(`知识页读取失败：${errorMessage(error)}`, true, "background-feedback");
      await refreshBootstrap({ automatic: true, pollMaintenance: false }).catch(() => {});
    }
  } finally {
    draft.loading = false;
    if (revision !== backgroundReadRevision) draft.attempted = false;
    if (backgroundDraft === draft && scopeId === draft.id) renderBackground();
  }
}

function changeBackground() {
  const draft = backgroundDraft;
  if (!draft) return;
  const pageId = $("background-page").value;
  if (pageId && !draft.selected.some((page) => page.page_id === pageId)) {
    if (draft.selected.length >= 8) {
      notify("最多选择 8 篇知识页", true, "background-feedback");
      renderBackground();
      return;
    }
    draft.selected.push({ page_id: pageId });
  }
  draft.enabled = !!pageId;
  draft.page_id = draft.selected[0]?.page_id || "";
  draft.dirty = true;
  $("background-feedback").hidden = true;
  renderBackground();
}

async function saveBackground() {
  const draft = backgroundDraft;
  if (!draft) return;
  let saved = false;
  const payload = { page_ids: draft.selected.map((page) => page.page_id), enabled: draft.enabled,
    revision: draft.revision, scope_revision: draft.scope_revision, config_revision: draft.config_revision };
  await perform($("background-save"), "memory", async () => {
    const response = await bridge.apiPost(endpoint("background", draft.id), payload);
    if (backgroundDraft !== draft || scopeId !== draft.id) return;
    draft.dirty = false;
    await refreshBootstrap();
    renderBackground();
    if (backgroundDraft?.id === draft.id && response.selection?.revision === backgroundDraft.revision) {
      backgroundDraft.check = response.check || null;
      backgroundDraft.checks = response.checks || [];
      renderBackground();
    }
    saved = true;
  }, "background-feedback");
  if (saved && scopeId === draft.id) notify("已保存", false, "background-feedback");
}

function pendingScopeHelp(scope) {
  if (scope.pending_reason === "service") return "请先在插件设置中完成服务连接。";
  if (scope.pending_reason === "recovering") return "正在核对记忆库和保存任务，请等待检查完成。";
  if (scope.pending_reason === "authorization") return "请确认机器人身份，完成用户开通。";
  return "机器人尚未连接，请先在 AstrBot 中连接。";
}

function selectScope(id) {
  bootstrapReadRevision++;
  maintenanceReadRevision++;
  backgroundReadRevision++;
  backgroundDraft = null;
  scopeId = id;
  $("bank-picker").value = id;
  if (!$("maintenance-dialog").open) { maintenanceScopeId = ""; maintenanceState = null; maintenanceIdentity = ""; }
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
  try { parsed = new URL(value); } catch { throw new Error("请填写完整的面板地址，例如 https://memory.example.com。"); }
  if (!/^https?:\/\//i.test(value) || !["http:", "https:"].includes(parsed.protocol)) throw new Error("面板地址请以 http:// 或 https:// 开头。");
  if (parsed.username || parsed.password || /[@]/.test(value.split("/")[2])) throw new Error("请只填写面板地址，登录密钥在面板登录页输入。");
  if (/[?#\\\s]/.test(value)) throw new Error("请填写面板首页地址，去掉空格、反斜杠以及 ? 或 # 后面的内容。");
  return value;
}

async function copyValue(field, message, target = "feedback") {
  if (!$(field).value) throw new Error("没有可复制的内容。");
  $(field).focus();
  $(field).select();
  try { await navigator.clipboard.writeText($(field).value); }
  catch { if (!document.execCommand("copy")) throw new Error("内容已选中，请按 Ctrl+C 或长按复制。"); }
  notify(message, false, target);
}

function connectionPayload() {
  const apiUrl = $("api-url").value.trim();
  let parsed;
  try { parsed = new URL(apiUrl); } catch { throw new Error("请填写完整的 API 地址，例如 http://hindsight:8888。"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("API 地址应使用 http 或 https，密钥请填写在 API 密钥中。");
  return { api_url: apiUrl, api_key: $("api-key").value.trim() };
}

function settingsPayload() {
  return { ...connectionPayload(), mode: managedMode() ? "managed" : "existing",
    control_plane_url: controlPlaneUrl($("control-plane-url").value), config_revision: settingsRevision };
}

async function requestAuthorization(platform, user) {
  const payload = { action: "prepare", platform_instance_id: platform, user_id: user, config_revision: settingsRevision };
  const response = await bridge.apiPost("accounts/authorize", payload);
  authorizationState = { ...payload, action: "confirm", confirmation: response.confirmation };
  $("authorization-description").textContent = `用户 ${user} / 机器人 ${response.bot_id}`;
  $("authorization-dialog").showModal();
}

async function prepareAuthorization(button, platform, user) {
  await perform(button, "setup", () => requestAuthorization(platform, user));
}

async function confirmAuthorization() {
  if (!authorizationState) return;
  const pending = authorizationState;
  authorizationState = null;
  await perform($("confirm-authorization"), "authorization", async () => {
    let response;
    try { response = await bridge.apiPost("accounts/authorize", pending); }
    finally { $("authorization-dialog").close(); }
    await refreshBootstrap({ automatic: true });
    notify(response.message);
  });
}

async function checkConnection(payload, target = "connection-result") {
  const response = await bridge.apiPost("connection/check", payload);
  if (response.ok !== true) throw new Error(response.message || "连接未成功，请检查 API 地址和密钥。");
  notify(response.message || "服务连接成功。", false, target);
  return response;
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function watchModels(requestId = "", managed = false) {
  if (modelWatch) return modelWatch;
  bootstrapReadRevision++;
  locks.add("setup");
  $("setup-fields").disabled = true;
  $("model-progress").hidden = false;
  modelWatch = (async () => {
    let failures = 0;
    while (true) {
      let job;
      try {
        job = await withTimeout(managed
          ? bridge.apiPost("managed/status", { mode: "managed", installation_id: managedSetup?.installation_id })
          : bridge.apiGet("models/status"), 10000, "获取接入进度超时。");
        failures = 0;
      } catch {
        if (managed && ++failures >= 3) throw new Error("无法获取接入进度，请重新打开页面核对结果后重试。");
        $("model-progress").textContent = "正在重新获取进度…";
        await delay(5000);
        continue;
      }
      if (requestId && job.request_id !== requestId) {
        $("model-progress").textContent = "请求未确认，请点击「保存并连接」重试。";
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
        $("model-progress").textContent = job.message || "模型已验证并保存，服务已启动。";
        await refreshBootstrap({ hydrate: managed });
        notify(job.message || "模型已验证并保存，服务已启动。");
      } else {
        if (managed && job.config_revision) settingsRevision = job.config_revision;
        $("model-progress").textContent = job.status === "idle"
          ? "接入检查已中断，请重新保存并连接。"
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
    renderServiceVersion();
  });
  return modelWatch;
}

function scheduleRefresh(milliseconds = 5000) {
  clearTimeout(refreshTimer);
  if (!viewReady) return;
  refreshTimer = setTimeout(() => void autoRefresh(), milliseconds);
}

async function autoRefresh() {
  if (document.hidden || refreshRunning || !viewReady || locks.size) {
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
    if (data) { data.status = { connected: null, label: "连接状态无法确认", message: "无法获取插件状态，请检查 AstrBot 连接。" }; statusSignature = ""; renderStatus(); }
    $("update-error").hidden = false;
    $("update-error-message").textContent = "暂时无法获取最新数据，正在自动重试。";
  } finally {
    refreshRunning = false;
    scheduleRefresh(Math.min(30000, 5000 * (refreshFailures + 1)));
  }
}


function maintenanceScope() {
  return data?.scopes.find((item) => item.scope_id === (maintenanceScopeId || scopeId));
}

function renderMaintenance() {
  syncMaintenanceState();
  const scope = maintenanceScope();
  const scopes = (data?.scopes || []).filter((item) => item.summary?.maintenance || item.summary?.bank_missing || item.scope_id === maintenanceScopeId);
  $("maintenance-scope").replaceChildren(...scopes.map((item) => option(item.scope_id, scopeName(item))));
  $("maintenance-scope").value = scope?.scope_id || "";
  $("maintenance-detail").textContent = scope?.summary?.maintenance || scope?.summary?.bank_missing
    ? maintenanceDescription(scope.summary, maintenanceState) : "这个记忆库未暂停读写。";
  $("maintenance-bank-check").hidden = !scope?.summary?.bank_missing;
  $("maintenance-bank-check").disabled = locks.has("memory") || !!data?.status?.recovering || data?.status?.connected === false;
  $("maintenance-retry").hidden = !scope?.summary?.maintenance || !maintenanceState?.error;
  $("maintenance-resume").hidden = !scope?.summary?.maintenance;
  $("maintenance-resume").disabled = !scope || !!scope.summary?.bank_missing || !!maintenanceState?.bank_missing || data?.status?.connected === false || maintenanceState?.ready !== true || maintenanceState.phase !== "ready" || locks.has("memory");
  const discardable = scope?.summary?.can_discard === true;
  $("discard-records").hidden = !discardable;
  if (!discardable) $("discard-pending").checked = false;
  $("maintenance-discard").disabled = !discardable || !$("discard-pending").checked || locks.has("memory");
  const configured = (data?.config.allowed_accounts || []).some((account) => accountKey(account) === accountKey(scope || {}));
  $("maintenance-forget").hidden = configured || !scope;
  $("maintenance-forget").disabled = locks.has("memory") || !!data?.status?.recovering || !!data?.backend?.pending || !!data?.backend?.upgrade_paused;
}

function openMaintenance(id) {
  maintenanceScopeId = id || data?.scopes.find((item) => item.summary?.maintenance || item.summary?.bank_missing)?.scope_id || "";
  renderMaintenance();
  $("maintenance-feedback").hidden = true;
  $("maintenance-dialog").showModal();
  void readMaintenanceStatus();
}

async function saveSettings() {
  if (!$("settings-form").reportValidity()) return;
  const payload = settingsPayload();
  await perform($("save-settings"), "setup", async () => {
    const response = await bridge.apiPost("settings/save", payload);
    if (response.saved !== true) throw new Error(response.message || "连接未保存，请重试。");
    settingsRevision = response.config_revision;
    $("api-key").value = "";
    clearCommands();
    await refreshBootstrap({ hydrate: true });
    notify("连接与管理地址已保存。");
  });
}

async function saveAccess() {
  if (!$("settings-form").reportValidity()) return;
  await perform($("save-access"), "setup", async () => {
    const response = await bridge.apiPost("access/save", { config_revision: settingsRevision,
      control_plane_url: controlPlaneUrl($("control-plane-url").value) });
    if (!response.saved) throw new Error(response.message || "官方管理地址未保存，请重试。");
    settingsRevision = response.config_revision;
    notify(response.message, false, "connection-result");
    await refreshBootstrap({ automatic: true });
  });
}

async function saveAccounts(accounts, revision) {
  const response = await bridge.apiPost("settings/save", { allowed_accounts: accounts, config_revision: revision });
  if (response.saved !== true) throw new Error(response.message || "用户设置未保存，请重试。");
  settingsRevision = response.config_revision;
  data.config_revision = response.config_revision;
  const previous = new Set((data.config.allowed_accounts || []).map(accountKey));
  data.config.allowed_accounts = accounts.map((account) => ({ ...account }));
  const confirmed = new Set(accounts.map(accountKey));
  for (const scope of data.scopes) {
    const key = accountKey(scope);
    if (previous.has(key) !== confirmed.has(key)) {
      scope.historical = true;
      scope.availability = confirmed.has(key) ? "confirmation" : "removed";
    }
  }
  clearCommands();
  updateUserChoices();
  $("memory-fields").disabled = !scopeId || locks.has("memory");
  renderScope();
  try { await refreshBootstrap({ automatic: true }); }
  catch {
    $("update-error").hidden = false;
    $("update-error-message").textContent = "用户设置已保存，暂时无法获取最新状态，正在自动重试。";
    scheduleRefresh();
  }
}

async function submitAccount() {
  if (!$("account-form").reportValidity()) return;
  const platform = $("account-platform").value;
  const user = $("account-user").value.trim();
  if (!platform || !user || /[:\x00-\x1f\x7f]/.test(user)) throw new Error("请选择平台并填写有效的用户 ID。");
  const accounts = data.config.allowed_accounts || [];
  if (accounts.some((item) => item.platform_instance_id === platform && item.user_id === user)) throw new Error("这个用户已经添加，请在用户页确认机器人身份。");
  const revision = settingsRevision;
  await perform($("save-account"), "setup", async () => {
    selectedAccount = accountKey({ platform_instance_id: platform, user_id: user });
    await saveAccounts([...accounts, { platform_instance_id: platform, user_id: user }], revision);
    $("account-dialog").close();
    try { await requestAuthorization(platform, user); }
    catch (error) { throw new Error(`用户已添加，身份确认尚未完成：${errorMessage(error)} 请在用户页点击「确认机器人身份」。`); }
  }, "account-feedback");
}

function requestAccountRemoval() {
  const account = (data.config.allowed_accounts || []).find((item) => accountKey(item) === selectedAccount);
  if (!account) return;
  accountRemovalState = { account, accounts: data.config.allowed_accounts.map((item) => ({ ...item })), revision: settingsRevision };
  $("remove-account-user").textContent = `${account.user_id} · ${data.platforms?.find((item) => item.id === account.platform_instance_id)?.name || account.platform_instance_id}`;
  $("remove-account-feedback").hidden = true;
  $("remove-account-dialog").showModal();
}

async function confirmAccountRemoval() {
  const pending = accountRemovalState;
  if (!pending) return;
  await perform($("confirm-remove-account"), "setup", async () => {
    await saveAccounts(pending.accounts.filter((item) => accountKey(item) !== accountKey(pending.account)), pending.revision);
    accountRemovalState = null;
    $("remove-account-dialog").close();
    notify("用户已移除，已有记忆保留。");
  }, "remove-account-feedback");
}

async function prepareInstallation(button, redeploy = false) {
  await perform(button, "setup", async () => {
    const response = await bridge.apiPost("install/prepare", {
      config_revision: settingsRevision, repair: redeploy && !data?.backend?.version,
      mode: "managed",
      control_plane_url: controlPlaneUrl($("control-plane-url").value),
    });
    if (!response.command) throw new Error("未生成部署命令，请稍后重试。");
    if (response.config_revision) settingsRevision = response.config_revision;
    $("install-command").value = response.command;
    $("installation").hidden = false;
    data.has_installation = true;
    data.installation_api_url = response.api_url;
    managedSetup = { ...managedSetup, installation_id: response.installation_id, has_installation: true,
      api_url: response.api_url, providers: managedSetup?.providers || data.providers };
    if (response.api_url) $("api-url").value = response.api_url;
    if (response.control_plane_url && !$("control-plane-url").value.trim()) $("control-plane-url").value = controlPlaneUrl(response.control_plane_url);
    updateConnectionMode();
    $("generate-deployment").hidden = !redeploy;
    $("check-deployment").hidden = !redeploy;
    $("deployment-heading").textContent = redeploy ? "重新部署" : "安装配套服务";
    $("deployment-description").textContent = redeploy
      ? "在原服务器、原安装目录执行。"
      : "在 AstrBot 所在主机执行，再选择模型连接。";
    $("deployment-feedback").hidden = true;
    if (!$("deployment-dialog").open) $("deployment-dialog").showModal();
  }, "deployment-feedback");
}

async function copyCommand(field, container, message) {
  if (!$(field).value) throw new Error("请先生成命令。");
  $(field).focus();
  $(field).select();
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText($(field).value);
  } catch {
    if (!document.execCommand("copy")) throw new Error("浏览器限制了自动复制。命令已选中，请按 Ctrl+C 或长按复制。");
  }
  $(field).value = "";
  $(container).hidden = true;
  notify(message);
}

async function refreshView() {
  if (!viewReady) { window.location.reload(); return; }
  await refreshBootstrap();
  $("update-error").hidden = true;
  notify("状态已刷新。");
  scheduleRefresh();
}

async function reloadSettings() {
  clearCommands();
  await refreshBootstrap({ hydrate: true, pollMaintenance: false });
  notify("设置已重新加载。");
}

async function setMode(button, mode) {
  const id = scopeId;
  if (!modeLabels[mode]) return;
  await perform(button, "memory", async () => {
    const response = await bridge.apiPost(endpoint("action", id), { action: "mode", mode });
    await refreshBootstrap();
    notify(response.message || "记忆模式已更新。");
  });
}

async function maintenanceAction(button, action, extra = {}) {
  if (action === "maintenance_status") { await readMaintenanceStatus(); return; }
  await perform(button, "memory", async () => {
    const id = maintenanceScope()?.scope_id;
    if (!id) throw new Error("请选择记忆库。");
    maintenanceState = null;
    maintenanceCheckedAt = 0;
    renderMaintenance();
    let response;
    try {
      response = await bridge.apiPost(endpoint("action", id), { action, ...extra,
        ...(["maintenance_begin", "bank_check"].includes(action) ? { config_revision: settingsRevision } : {}) });
    } catch (error) {
      if (action === "maintenance_begin") await refreshBootstrap({ automatic: true, pollMaintenance: false }).catch(() => {});
      throw error;
    }
    if (response.config_revision) settingsRevision = response.config_revision;
    await refreshBootstrap();
    if (action === "maintenance_begin") $("discard-pending").checked = false;
    notify(response.message || "读写暂停已解除。", false, "maintenance-feedback");
  }, "maintenance-feedback");
  renderMaintenance();
}

function clearUpgrade() {
  $("upgrade-command").value = "";
  $("upgrade-installation").hidden = true;
  $("upgrade-backup-confirmed").checked = false;
}

function clearCommands() {
  $("install-command").value = "";
  $("installation").hidden = true;
  clearUninstall();
  clearUpgrade();
}

function openUpgrade() {
  if (!data?.config.managed || !data?.backend?.managed) return;
  activateTab("setup");
  if (!$("upgrade-dialog").open) $("upgrade-dialog").showModal();
}

async function prepareForget(button, id) {
  await perform(button, "memory", async () => {
    const response = await bridge.apiPost(endpoint("action", id), { action: "forget_prepare" });
    if (!response.confirmation) throw new Error("无法准备清空操作，请重试。");
    forgetState = { id, confirmation: response.confirmation };
    $("forget-user").textContent = scopeName(data.scopes.find((item) => item.scope_id === id));
    $("forget-message").textContent = "清空后暂停记忆，完成后可切回读写模式。";
    $("forget-feedback").textContent = "";
    $("forget-agree").checked = false;
    $("forget-agree").disabled = false;
    $("confirm-forget").disabled = true;
    $("forget-dialog").showModal();
  }, button === $("maintenance-forget") ? "maintenance-feedback" : "feedback");
}

$("settings-form").addEventListener("submit", (event) => { event.preventDefault(); void (managedMode() ? saveAccess() : saveSettings()).catch((error) => notify(errorMessage(error), true)); });
$("parameters-form").addEventListener("input", () => {
  if (parameterDraft) parameterDraft.dirty = true;
  $("parameters-feedback").hidden = true;
  renderParameterLimits();
});
$("parameters-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void saveParameters().catch((error) => notify(errorMessage(error), true, "parameters-feedback"));
});
$("background-recheck").addEventListener("click", () => void loadBackground({ manual: true }));
$("background-save").addEventListener("click", () => void saveBackground());
$("background-settings").addEventListener("click", () => {
  activateTab("setup");
  $("memory-background_max_tokens").focus();
});
$("check-connection").addEventListener("click", () => void perform($("check-connection"), "setup", async () => {
  await checkConnection(connectionPayload());
  await refreshBootstrap();
}));
$("add-account").addEventListener("click", addAccount);
$("account-platform").addEventListener("change", updateAccountHint);
$("account-form").addEventListener("submit", (event) => { event.preventDefault(); void submitAccount().catch((error) => notify(errorMessage(error), true, "account-feedback")); });
$("authorize-account").addEventListener("click", () => {
  const scope = data.scopes.find((item) => item.scope_id === scopeId);
  if (scope) void prepareAuthorization($("authorize-account"), scope.platform_instance_id, scope.user_id);
});
$("confirm-authorization").addEventListener("click", () => void confirmAuthorization());
$("cancel-authorization").addEventListener("click", () => $("authorization-dialog").close());
$("authorization-dialog").addEventListener("close", () => { authorizationState = null; });
$("remove-account").addEventListener("click", requestAccountRemoval);
$("cancel-remove-account").addEventListener("click", () => $("remove-account-dialog").close());
$("confirm-remove-account").addEventListener("click", () => void confirmAccountRemoval());
$("remove-account-dialog").addEventListener("close", () => { accountRemovalState = null; });
for (const radio of document.querySelectorAll('[name="connection-mode"]')) radio.addEventListener("change", () => {
  clearCommands();
  $("api-key").value = "";
  $("connection-result").hidden = true;
  $("api-url").value = radio.value === "managed" ? data.installation_api_url || "" : data.config.api_url || "";
  updateConnectionMode();
  managedReadRevision++;
  managedSetup = null;
  if (radio.value === "managed") void loadManagedSetup().catch((error) => notify(errorMessage(error), true));
  renderServiceVersion();
});
$("save-access").addEventListener("click", () => void saveAccess());
$("prepare").addEventListener("click", () => void prepareInstallation($("prepare")));
$("reinstall").addEventListener("click", () => {
  clearCommands();
  $("deployment-heading").textContent = "重新部署";
  $("deployment-description").textContent = "在原服务器、原安装目录执行。";
  $("generate-deployment").hidden = false;
  $("check-deployment").hidden = true;
  $("deployment-feedback").hidden = true;
  $("deployment-dialog").showModal();
});
$("generate-deployment").addEventListener("click", () => void prepareInstallation($("generate-deployment"), true));
$("check-deployment").addEventListener("click", () => void perform($("check-deployment"), "setup", () => checkConnection({}, "deployment-feedback"), "deployment-feedback"));
$("service-version-check").addEventListener("click", () => void perform($("service-version-check"), "setup", checkServiceVersion));
$("view-upgrade").addEventListener("click", openUpgrade);
$("go-upgrade").addEventListener("click", openUpgrade);
$("ignore-upgrade").addEventListener("click", () => void perform($("ignore-upgrade"), "setup", async () => {
  const response = await bridge.apiPost("service/upgrade/ignore", {
    config_revision: settingsRevision, connection_revision: data.config.connection_revision, target: data.backend.target,
  });
  settingsRevision = response.config_revision;
  await refreshBootstrap({ automatic: true, pollMaintenance: false });
  notify(response.message);
}));
for (const action of ["begin", "prepare", "verify", "resume", "cancel"]) $("upgrade-" + action).addEventListener("click", () => void upgradeAction(action, $("upgrade-" + action)));
$("copy-upgrade").addEventListener("click", () => void perform($("copy-upgrade"), "copy", () => copyCommand("upgrade-command", "upgrade-installation", "升级命令已复制，请在原服务器执行。")));
$("copy-command").addEventListener("click", () => void perform($("copy-command"), "copy", () => copyCommand("install-command", "installation", "部署命令已复制，请在运行 AstrBot 的服务器执行。")));
for (const button of document.querySelectorAll("[data-guide]")) button.addEventListener("click", () => {
  const dialog = $(button.dataset.guide);
  if (button.dataset.guide === "backup-dialog") {
    const managed = data?.config.managed === true;
    for (const item of dialog.querySelectorAll("[data-managed-backup]")) item.hidden = !managed;
    $("backup-existing-service").hidden = managed;
    $("backup-heading").textContent = managed ? "备份与恢复" : "Bridge 备份与恢复";
  }
  if (!dialog.open) dialog.showModal();
});
for (const button of document.querySelectorAll("[data-copy]")) button.addEventListener("click", () => void perform(
  button, "copy", () => copyValue(button.dataset.copy, "已复制。", button.dataset.feedback), button.dataset.feedback,
));
$("prepare-uninstall").addEventListener("click", () => {
  clearUninstall();
  $("delete-memory-data").checked = false;
  $("uninstall-feedback").hidden = true;
  $("uninstall-dialog").showModal();
});
$("generate-uninstall").addEventListener("click", () => void perform($("generate-uninstall"), "setup", async () => {
  if (!$("delete-memory-data").checked) throw new Error("请先确认删除范围。");
  const response = await bridge.apiPost("uninstall/prepare", { config_revision: settingsRevision, delete_memory_data: true });
  if (!response.command) throw new Error("未生成卸载命令，请重试。");
  $("uninstall-command").value = response.command;
  $("uninstallation").hidden = false;
}, "uninstall-feedback"));
$("delete-memory-data").addEventListener("change", clearUninstall);
$("copy-uninstall").addEventListener("click", () => void perform($("copy-uninstall"), "copy", () => copyCommand("uninstall-command", "uninstallation", "卸载命令已复制，请在服务器执行。")));
$("save-models").addEventListener("click", () => void perform($("save-models"), "setup", async () => {
  if (!managedSetup?.has_installation && !data?.has_installation) throw new Error("请先生成并执行配套服务安装命令。");
  if (!managedSetup?.installation_id) await loadManagedSetup();
  modelRequestId ||= globalThis.crypto?.randomUUID?.() || `model-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const selection = { config_revision: settingsRevision, mode: "managed", installation_id: managedSetup?.installation_id, request_id: modelRequestId };
  for (const kind of ["chat", "embedding", "rerank"]) {
    const select = $(`${kind}-provider`);
    if ((kind !== "rerank" && !select.value) || select.selectedOptions[0]?.disabled) throw new Error("请选择可用的记忆处理模型和向量模型。");
    selection[`${kind}_provider_id`] = select.value;
  }
  $("model-progress").hidden = false;
  $("model-progress").textContent = "正在提交模型配置…";
  let response;
  try { response = await bridge.apiPost("managed/connect", selection); }
  catch (error) {
    const status = await bridge.apiPost("managed/status", { mode: "managed", installation_id: managedSetup?.installation_id }).catch(() => null);
    if (!status) { $("model-progress").textContent = "正在确认处理结果…"; await watchModels(modelRequestId, true); return; }
    if (status.request_id !== modelRequestId && status.status !== "running") { modelRequestId = ""; throw error; }
    response = status;
  }
  await watchModels(response.request_id, true);
}));
$("refresh").addEventListener("click", () => void perform($("refresh"), "refresh", refreshView));
$("reload-settings").addEventListener("click", () => void perform($("reload-settings"), "setup", reloadSettings));
$("service-alert-setup").addEventListener("click", () => activateTab("setup", true));
$("dismiss-feedback").addEventListener("click", () => { $("feedback").hidden = true; clearTimeout(feedbackTimer); });
$("scope-picker").addEventListener("change", () => { selectedAccount = $("scope-picker").value; scopeId = ""; updateUserChoices(); selectScope(scopeId); });
$("bank-picker").addEventListener("change", () => selectScope($("bank-picker").value));
for (const button of document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => void setMode(button, button.dataset.mode));
$("background-page").addEventListener("change", changeBackground);
$("reading-mode").addEventListener("change", () => {
  if (readingDraft) readingDraft.dirty = true;
  $("reading-feedback").hidden = true;
  renderReading();
});
$("reading-save").addEventListener("click", () => void saveReading());
for (const id of ["reflect-enabled", "reflect-timeout"]) $(id).addEventListener("input", () => {
  if (reflectDraft) reflectDraft.dirty = true;
  renderReflect();
});
$("reflect-save").addEventListener("click", () => void saveReflect());
$("copy-bank").addEventListener("click", () => void perform($("copy-bank"), "copy",
  () => copyValue("scope-bank", "Bank ID 已复制。")));
$("copy-control-plane").addEventListener("click", () => void perform($("copy-control-plane"), "copy",
  () => copyValue("control-plane-url", "已复制")));
$("scope-bank").addEventListener("focus", () => $("scope-bank").select());
$("retry").addEventListener("click", () => void perform($("retry"), "memory", async () => {
  const response = await bridge.apiPost(endpoint("action"), { action: "retry" });
  await refreshBootstrap();
  notify(response.message || "已安排重试。");
}));
$("maintenance-view").addEventListener("click", () => openMaintenance(scopeId));
$("show-paused-banks").addEventListener("click", () => openMaintenance(data.scopes.find((item) => (item.summary?.maintenance || item.summary?.bank_missing)
  && !(data.config.allowed_accounts || []).some((account) => accountKey(account) === accountKey(item)))?.scope_id));
$("maintenance-scope").addEventListener("change", () => { maintenanceScopeId = $("maintenance-scope").value; renderMaintenance(); void readMaintenanceStatus(); });
$("maintenance-retry").addEventListener("click", () => void readMaintenanceStatus());
$("maintenance-resume").addEventListener("click", () => void maintenanceAction($("maintenance-resume"), "maintenance_resume"));
$("maintenance-bank-check").addEventListener("click", () => void maintenanceAction($("maintenance-bank-check"), "bank_check"));
$("maintenance-forget").addEventListener("click", () => void prepareForget($("maintenance-forget"), maintenanceScope()?.scope_id));
$("discard-pending").addEventListener("change", renderMaintenance);
$("maintenance-discard").addEventListener("click", () => void maintenanceAction($("maintenance-discard"), "maintenance_begin", { discard_pending: $("discard-pending").checked }));
$("maintenance-dialog").addEventListener("close", () => { maintenanceScopeId = ""; maintenanceIdentity = ""; maintenanceState = null; });
$("forget").addEventListener("click", () => void prepareForget($("forget"), scopeId));
$("forget-agree").addEventListener("change", () => { $("confirm-forget").disabled = !$("forget-agree").checked; });
$("cancel-forget").addEventListener("click", () => { if (!locks.has("forget")) $("forget-dialog").close(); });
$("forget-dialog").addEventListener("cancel", (event) => { if (locks.has("forget")) event.preventDefault(); });
$("forget-dialog").addEventListener("close", () => { forgetState = null; $("forget-agree").checked = false; });
$("confirm-forget").addEventListener("click", () => {
  if (!forgetState || !$("forget-agree").checked) return;
  const pending = forgetState;
  void perform($("confirm-forget"), "forget", async () => {
    maintenanceReadRevision++;
    $("forget-agree").disabled = true;
    $("cancel-forget").disabled = true;
    try {
      const response = await bridge.apiPost(endpoint("action", pending.id), { action: "forget_confirm", confirmation: pending.confirmation });
      forgetState = null;
      $("forget-dialog").close();
      await refreshBootstrap();
      notify(response.message || "正在清空旧记忆。");
    } finally {
      $("forget-agree").disabled = false;
      $("cancel-forget").disabled = false;
      if ($("forget-dialog").open) {
        forgetState = null;
        $("forget-agree").checked = false;
        $("forget-agree").disabled = true;
        $("forget-message").textContent = "操作未完成，请关闭窗口后重新发起清空。";
      }
    }
  }, "forget-feedback").finally(() => { $("confirm-forget").disabled = true; });
});
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => $(button.dataset.close).close());
$("deployment-dialog").addEventListener("close", () => { $("install-command").value = ""; $("installation").hidden = true; });
$("uninstall-dialog").addEventListener("close", clearUninstall);
$("upgrade-dialog").addEventListener("close", clearUpgrade);
document.addEventListener("visibilitychange", () => { if (document.hidden) clearCommands(); });
window.addEventListener("pagehide", () => { clearCommands(); backgroundReadRevision++; backgroundDraft = null; });

async function start() {
  if (startRunning) return;
  startRunning = true;
  $("startup").hidden = false;
  $("startup").textContent = "正在连接 AstrBot…";
  $("startup").classList.remove("error");
  $("refresh").disabled = true;
  try {
    bridge = window.AstrBotPluginView;
    if (!bridge) throw new Error("请从 AstrBot 的插件详情页打开「记忆管理」。");
    const context = await withTimeout(bridge.ready(), 10000, "连接 AstrBot 超时，请重新加载插件页面。");
    const theme = (value) => { document.documentElement.dataset.theme = (value || bridge.getContext?.())?.isDark ? "dark" : "light"; };
    theme(context); offContext?.(); offContext = bridge.onContext?.(theme);
    await refreshBootstrap({ hydrate: true, pollMaintenance: false });
    $("refresh").disabled = false;
    $("refresh").textContent = "重新获取";
    $("update-error").hidden = true;
    activateTab(data.config.configured ? "memories" : "setup");
    viewReady = true;
    renderScope();
    scheduleRefresh();
    void readMaintenanceStatus().catch((error) => notify(errorMessage(error), true));
  } catch (error) {
    $("startup").textContent = errorMessage(error);
    $("startup").classList.add("error"); $("startup").setAttribute("role", "alert");
    $("refresh").disabled = false;
    $("refresh").textContent = viewReady ? "重新获取" : "重新加载页面";
    $("update-error").hidden = false; $("update-error-message").textContent = "页面加载失败，请重试。";
  } finally { startRunning = false; }
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(refreshTimer);
  if (!document.hidden) {
    backgroundReadRevision++;
    backgroundDraft = null;
    if (data) renderScope();
    scheduleRefresh(0);
  }
});
window.addEventListener("beforeunload", () => { clearTimeout(refreshTimer); clearTimeout(maintenanceExpiryTimer); offContext?.(); }, { once: true });
void start();
