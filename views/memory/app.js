const $ = (id) => document.getElementById(id);
let bridge = window.AstrBotPluginView;
const modeLabels = { active: "保存并自动使用记忆", readonly: "只自动使用已有记忆", paused: "暂停保存和自动使用" };
let data = null;
let settingsRevision = "";
let scopeId = "";
let modeDirty = false;
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
let bootstrapReadRevision = 0;
let refreshTimer;
let refreshRunning = false;
let refreshFailures = 0;
let modelRequestId = "";
let modelWatch = null;
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
    if (group === "setup") updateProviderHelp();
    if (group === "memory") renderScope();
  }
}

function activateTab(name, focus = false) {
  document.querySelectorAll(".help[open]").forEach((help) => { help.open = false; });
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
    : "供 Hindsight 使用：大语言模型（LLM）提取和整合记忆、生成知识页；嵌入模型（Embeddings）将文字转为向量；重排模型（Reranker）重新排序检索结果（可选）。";
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

function renderServiceVersion() {
  const backend = data?.backend;
  $("service-version").hidden = !data?.config.configured;
  $("service-version-text").textContent = backend?.version
    ? `Hindsight ${backend.version}${backend.upgrade_available ? " · 可升级到 1.2.0，当前版本仍受支持。" : backend.supported === false ? " · 此版本未经兼容验证。" : ""}`
    : "Hindsight 版本尚未确认，请恢复服务连接后重新检查。";
  const managed = data?.config.managed && backend?.managed;
  $("external-upgrade").hidden = !!data?.config.managed;
  const upgrading = backend?.pending || backend?.upgrade_paused;
  $("setup-update").hidden = !backend?.upgrade_available && !upgrading;
  $("setup-update").textContent = upgrading ? "升级待完成" : "可升级";
  $("go-upgrade").hidden = !backend?.upgrade_available && !upgrading;
  $("go-upgrade").textContent = managed ? "查看升级步骤" : "查看服务升级指引";
  $("service-upgrade").hidden = !managed || (!backend?.upgrade_available && !upgrading);
  $("reinstall").disabled = !!backend?.pending || (!!backend?.managed && !!backend?.upgrade_available);
  $("reinstall").textContent = "生成同版本修复命令（仅 1.2.0）";
  $("upgrade-begin").disabled = !backend?.upgrade_available || !!upgrading;
  $("upgrade-prepare").disabled = !backend?.upgrade_available;
  $("upgrade-abandon").hidden = backend?.version !== "1.1.0" || !upgrading;
  $("upgrade-cancel").disabled = backend?.version !== "1.1.0" || !upgrading;
  $("upgrade-verify").disabled = !backend?.pending;
  $("upgrade-resume").hidden = !backend?.resume_available;
  $("upgrade-resume").disabled = backend?.version !== "1.2.0" || !backend?.resume_available;
  if (upgrading) {
    $("save-settings").disabled = true;
    $("save-models").disabled = true;
    $("add-account").disabled = true;
  } else {
    $("save-settings").disabled = false;
    $("add-account").disabled = false;
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
      $("upgrade-installation").open = true;
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

function addAccount(account = {}) {
  const row = el("div", "account-row");
  const platformLabel = el("label", "", "平台实例");
  const select = el("select");
  select.className = "account-platform";
  select.required = true;
  select.append(option("", "请选择平台实例"));
  (data.platforms || []).forEach((platform) => select.append(option(
    platform.id, platform.name || platform.id, platform.supported === false || platform.enabled === false,
  )));
  if (account.platform_instance_id && !(data.platforms || []).some((item) => item.id === account.platform_instance_id)) {
    select.append(option(account.platform_instance_id, `${account.platform_instance_id}（实例不可用）`));
  }
  select.value = account.platform_instance_id || "";
  platformLabel.append(select);
  const userLabel = el("label");
  const caption = el("span");
  const input = el("input");
  input.type = "text";
  input.required = true;
  input.autocomplete = "off";
  input.className = "account-user";
  input.value = account.user_id || "";
  userLabel.append(caption, input);
  function updateHint() {
    const platform = (data.platforms || []).find((item) => item.id === select.value);
    caption.textContent = platform?.user_label || "用户 ID";
    input.inputMode = platform?.numeric_user_id ? "numeric" : "text";
    if (platform?.numeric_user_id) {
      input.pattern = "[0-9]+";
      input.maxLength = 20;
    } else {
      input.removeAttribute("pattern");
      input.removeAttribute("maxlength");
    }
    input.placeholder = platform?.user_hint || "填写 /sid 中的用户 UID";
  }
  select.addEventListener("change", updateHint);
  updateHint();
  const authorize = el("button", "", "确认账号授权");
  authorize.type = "button";
  authorize.addEventListener("click", () => void prepareAuthorization(
    authorize, select.value, input.value.trim(),
  ));
  const remove = el("button", "danger-quiet", "移除");
  remove.type = "button";
  remove.setAttribute("aria-label", account.user_id ? `移除用户 ${account.user_id}` : "移除此用户");
  remove.addEventListener("click", () => requestAccountRemoval(row, remove));
  row.append(platformLabel, userLabel, authorize, remove);
  $("accounts").append(row);
  return select;
}

function requestAccountRemoval(row, button) {
  if (locks.has("setup") || $("remove-account-dialog").open) return;
  const platform = row.querySelector(".account-platform").value;
  const user = row.querySelector(".account-user").value.trim();
  const name = (data.platforms || []).find((item) => item.id === platform)?.name || platform || "未选择平台";
  accountRemovalState = { row, button, platform, user };
  $("remove-account-user").textContent = `${name} · ${user || "未填写用户 ID"}`;
  $("remove-account-dialog").showModal();
}

$("cancel-remove-account").addEventListener("click", () => $("remove-account-dialog").close());
$("remove-account-dialog").addEventListener("close", () => {
  const button = accountRemovalState?.button;
  accountRemovalState = null;
  (button?.isConnected ? button : $("add-account")).focus();
});
$("confirm-remove-account").addEventListener("click", () => {
  const pending = accountRemovalState;
  if (!pending) return;
  if (
    locks.has("setup") || !$("accounts").contains(pending.row)
    || pending.row.querySelector(".account-platform").value !== pending.platform
    || pending.row.querySelector(".account-user").value.trim() !== pending.user
  ) {
    $("remove-account-dialog").close();
    notify("使用者信息已变化，请重新选择要移除的用户。", true);
    return;
  }
  pending.row.remove();
  $("remove-account-dialog").close();
  notify("已从使用者名单移除，请保存设置使更改生效。");
});

function refreshAccountPlatforms() {
  $("accounts").querySelectorAll(".account-platform").forEach((select) => {
    const selected = select.value;
    select.replaceChildren(option("", "请选择平台实例"));
    (data.platforms || []).forEach((platform) => select.append(option(
      platform.id, platform.name || platform.id, platform.supported === false || platform.enabled === false,
    )));
    if (selected && !(data.platforms || []).some((platform) => platform.id === selected)) {
      select.append(option(selected, `${selected}（实例不可用）`));
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
  $("save-settings").textContent = "保存连接和使用者名单";
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
  return `${scope.user_id} · ${platform?.name || scope.platform_instance_id}${scope.historical ? ` · 历史机器人 ${scope.bot_id}` : ""}`;
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
  $("status-label").textContent = status.recovering ? "正在核对记忆数据"
    : connected === true ? "服务已连接" : status.label || (connected === false ? "服务未连接" : "连接状态未知");
  $("status-dot").className = `status-dot${!status.recovering && connected === true ? " good" : connected === false ? " bad" : ""}`;
  $("status-description").textContent = status.recovering ? "正在核对记忆库归属、保存任务和清空记录，完成后按原记忆模式运行。"
    : connected === false ? (status.message || (data.config.managed
      ? "请检查配套服务是否运行；服务恢复后会自动连接。"
      : "请检查服务地址、密钥与服务状态，可点击「测试服务连接」查看原因。"))
    : "";
  if (data.config.managed && data.managed_service?.installed && !status.recovering) {
    if (!data.managed_service.ready) {
      $("status-label").textContent = "服务已安装，请选择模型并保存";
      $("status-description").textContent = "";
    } else if (!data.config.configured) {
      $("status-label").textContent = "模型已保存，请添加使用者并保存连接和名单";
    }
  }
  $("status-description").hidden = !$("status-description").textContent;
  const attention = !!status.recovering || !data.config.configured || connected !== true;
  $("service-alert").hidden = !attention;
  $("service-alert-message").textContent = attention
    ? [$("status-label").textContent, $("status-description").textContent].filter(Boolean).join("：")
    : "";
}

function recallDescription(recall) {
  if (!recall) return "最近记忆检索：尚未检索";
  const labels = { success: "成功", empty: "无相关结果", error: "失败", skipped: "已跳过" };
  const label = recall.outcome === "error" && recall.code?.includes("timeout") ? "超时" : labels[recall.outcome];
  let result = `最近记忆检索：${label} · ${new Date(recall.last_at * 1000).toLocaleTimeString()} · ${(recall.elapsed_ms / 1000).toFixed(2)} 秒`;
  if (recall.consecutive_failures) result += ` · 连续失败 ${recall.consecutive_failures} 次`;
  if (recall.error_message) result += `。${recall.error_message}`;
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
    if (maintenanceIdentity) {
      maintenanceReadRevision++;
      maintenanceState = { ready: false, error: errorMessage(error) };
      renderScope();
    }
    throw error;
  }
  if (revision !== bootstrapReadRevision) return;
  if (settingsRevision && next.config_revision !== settingsRevision) clearUpgrade();
  data = next;
  renderServiceVersion();
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
  $("platform-help").textContent = data.platforms?.some((platform) => platform.supported !== false)
    ? "" : "未找到可用的平台实例。请先在 AstrBot 中添加受支持的机器人。";
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
  renderServiceVersion();
  $("settings-conflict").hidden = !settingsRevision || data.config_revision === settingsRevision;
  $("startup").hidden = true;
  if (data.model_job?.status === "running" && !modelWatch) void watchModels();
}

function maintenanceDescription(summary = {}, state = null) {
  if (summary.bank_missing || state?.bank_missing) return "找不到这个用户的记忆库，记忆功能已暂停。请恢复原记忆库，或通过「清空此用户的记忆」重新开始。";
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
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const identity = maintenanceSnapshot(scope);
  if (identity !== maintenanceIdentity) {
    maintenanceReadRevision++;
    maintenanceIdentity = identity;
    maintenanceState = null;
    maintenanceCheckedAt = 0;
    $("discard-pending").checked = false;
    $("discard-records").open = false;
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
    renderScope();
  }, Math.max(0, 30000 - (Date.now() - checkedAt)));
}

async function readMaintenanceStatus() {
  syncMaintenanceState();
  const revision = ++maintenanceReadRevision;
  const identity = maintenanceIdentity;
  const id = scopeId;
  if (!identity) return;
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
  if (revision !== maintenanceReadRevision || identity !== maintenanceIdentity || id !== scopeId) return;
  maintenanceState = state;
  maintenanceCheckedAt = Date.now();
  renderScope();
  scheduleMaintenanceExpiry();
}

function renderScope() {
  syncMaintenanceState();
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const summary = scope?.summary || {};
  if (!modeDirty) $("scope-mode").value = scope?.mode || "";
  $("scope-mode-field").hidden = !!scope?.historical;
  $("save-mode").hidden = !!scope?.historical;
  $("historical-mode").hidden = !scope?.historical;
  $("historical-mode").textContent = scope?.historical ? `机器人 ${scope.bot_id} · 原记忆模式：${modeLabels[scope.mode] || scope.mode}` : "";
  for (const id of ["scope-mode", "save-mode", "retry", "maintenance-begin", "maintenance-retry", "maintenance-resume", "discard-pending", "maintenance-discard", "forget"]) $(id).disabled = !scope || scope.pending === true;
  for (const id of ["scope-mode", "save-mode", "retry"]) $(id).disabled ||= scope?.historical === true;
  const maintenanceActive = !!summary.maintenance;
  $("scope-mode").querySelector('[value="active"]').disabled = maintenanceActive || !!summary.bank_missing;
  $("scope-mode").querySelector('[value="readonly"]').disabled = maintenanceActive || !!summary.bank_missing;
  $("maintenance-begin").hidden = maintenanceActive;
  $("maintenance-resume").hidden = !maintenanceActive;
  $("maintenance-retry").hidden = !maintenanceActive || !maintenanceState?.error;
  $("maintenance-resume").disabled ||= !maintenanceActive || !!summary.bank_missing || !!maintenanceState?.bank_missing || data?.status?.connected === false || maintenanceState?.ready !== true || maintenanceState.phase !== "ready";
  $("retry").disabled ||= maintenanceActive || !!summary.bank_missing;
  $("retry").hidden = $("retry").disabled || !(summary.counts?.failed || summary.counts?.dead);
  const discardable = summary.can_discard === true;
  $("discard-records").hidden = !discardable;
  if (!discardable) {
    $("discard-pending").checked = false;
    $("discard-records").open = false;
  }
  $("maintenance-discard").disabled ||= !$("discard-pending").checked;
  $("pause-impact").hidden = !scope || !!scope.pending;
  $("scope-error").hidden = !summary.error_message;
  $("scope-error").textContent = text(summary.error_message);
  $("control-plane-address").value = data?.config.control_plane_url || "";
  $("control-plane-missing").hidden = !!data?.config.control_plane_url;
  const keyCommand = data?.config.managed ? data.control_plane_key_command || "" : "";
  $("control-plane-key-command").hidden = !keyCommand;
  $("control-plane-key-command").value = keyCommand;
  $("control-plane-key-help").textContent = keyCommand
    ? "用安装服务时的账号登录服务器，在终端执行下方命令。输出的内容就是面板登录密钥；自定义过安装目录时，请替换命令中的路径。"
    : "连接已有 Hindsight 时，请向服务管理员获取网页登录密钥。";
  $("scope-bank").value = scope?.bank || "";
  $("queue-summary").textContent = scope?.pending ? pendingScopeHelp(scope) : Object.keys(summary.counts || {}).length ? countDescription(summary.counts) : "";
  $("queue-summary").hidden = !$("queue-summary").textContent;
  $("scope-recall-status").textContent = scope?.pending || !summary.recall ? "" : recallDescription(summary.recall);
  $("scope-recall-status").hidden = !$("scope-recall-status").textContent;
  $("maintenance-state").textContent = maintenanceDescription(summary, maintenanceState);
  $("maintenance-state").hidden = !$("maintenance-state").textContent;
  renderBackground();
}

function backgroundSnapshot(scope) {
  return JSON.stringify([
    scope?.scope_id, scope?.bank, scope?.background?.scope_revision, scope?.background?.revision,
    data?.config_revision,
    !!scope?.historical, !!scope?.pending, !!scope?.summary?.maintenance, !!scope?.summary?.bank_missing,
  ]);
}

function renderBackground() {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  const saved = scope?.background || { revision: 0, enabled: false, page_id: "" };
  const snapshot = backgroundSnapshot(scope);
  if (!backgroundDraft || backgroundDraft.snapshot !== snapshot) {
    backgroundReadRevision++;
    backgroundDraft = {
      snapshot, id: scopeId, page_id: saved.page_id || "", enabled: saved.enabled === true,
      revision: saved.revision, scope_revision: saved.scope_revision,
      config_revision: data?.config_revision, pages: [], loaded: false,
    };
    $("background-feedback").hidden = true;
    $("background-feedback").textContent = "";
  }
  const unavailable = !scope || scope.pending || scope.historical || data?.status?.recovering || saved.error;
  const blocked = unavailable || scope.summary?.maintenance || scope.summary?.bank_missing;
  for (const id of ["background-page", "background-enabled", "background-load", "background-save"]) $(id).disabled = !!blocked || locks.has("memory");
  $("background-load").disabled ||= backgroundDraft.loading === true;
  $("background-close").disabled = !!unavailable || !saved.enabled || locks.has("memory");
  const select = $("background-page");
  const options = [option("", "不选择知识页（检索按记忆模式运行）")];
  backgroundDraft.pages.forEach((page) => options.push(option(page.page_id, page.path)));
  if (backgroundDraft.page_id && !backgroundDraft.pages.some((page) => page.page_id === backgroundDraft.page_id)) {
    options.push(option(backgroundDraft.page_id, `${backgroundDraft.page_id}（已选；请获取列表确认）`));
  }
  select.replaceChildren(...options);
  select.value = backgroundDraft.page_id;
  $("background-enabled").checked = backgroundDraft.enabled;
  let status = !saved.page_id ? "" : saved.enabled ? "聊天时使用所选知识页已开启。" : "聊天时使用已关闭。";
  if (unavailable) status = saved.error || "请连接当前机器人并确认授权后选择知识页。";
  else if (blocked) status = "保存和查询暂停或数据恢复检查期间不读取知识页；可以停止聊天时使用知识页。";
  else if (scope.mode === "paused" && saved.enabled) status = "已设置聊天时使用知识页，但「暂停保存和自动使用」模式生效，聊天时不提供知识页参考。";
  else if (saved.status?.message) status += ` ${saved.status.message}`;
  else if (saved.status?.used) status += " 最近一次聊天已提供所选知识页参考。内容是否过期尚未确认，请在官方面板核对。";
  else if (backgroundDraft.loaded && !backgroundDraft.pages.length) status = "此记忆库还没有知识页；请按问号中的步骤创建，再获取列表。";
  $("background-status").textContent = status;
  $("background-status").hidden = !status;
}

async function loadBackground() {
  const draft = backgroundDraft;
  if (!draft || $("background-load").disabled) return;
  const revision = ++backgroundReadRevision;
  draft.loading = true;
  $("background-load").disabled = true;
  try {
    const response = await withTimeout(
      bridge.apiGet(endpoint("background", draft.id)), 8500, "获取知识页列表超时，请检查服务后重试。",
    );
    if (revision !== backgroundReadRevision || backgroundDraft !== draft || scopeId !== draft.id) return;
    if (!Array.isArray(response.pages)) throw new Error("知识页列表无法读取，请检查服务后重试。");
    if (response.selection.revision !== draft.revision || response.scope_revision !== draft.scope_revision) {
      throw new Error("知识页选择或使用开关已变化，请重新获取插件状态后再选择。");
    }
    draft.pages = response.pages;
    draft.loaded = true;
    draft.config_revision = response.config_revision;
    renderBackground();
  } catch (error) {
    if (revision === backgroundReadRevision && backgroundDraft === draft && scopeId === draft.id) {
      notify(errorMessage(error), true, "background-feedback");
    }
  } finally {
    draft.loading = false;
    if (backgroundDraft === draft && scopeId === draft.id) renderBackground();
  }
}

async function saveBackground(close = false) {
  const draft = backgroundDraft;
  if (!draft) return;
  const saved = data.scopes.find((scope) => scope.scope_id === draft.id)?.background;
  const payload = {
    page_id: close ? saved?.page_id || "" : draft.page_id,
    enabled: close ? false : draft.enabled,
    revision: draft.revision, scope_revision: draft.scope_revision, config_revision: draft.config_revision,
  };
  await perform($(close ? "background-close" : "background-save"), "memory", async () => {
    const response = await bridge.apiPost(endpoint("background", draft.id), payload);
    if (backgroundDraft !== draft || scopeId !== draft.id) return;
    await refreshBootstrap();
    notify(response.message || "知识页选择和聊天使用开关已保存。", false, "background-feedback");
  }, "background-feedback");
}

function pendingScopeHelp(scope) {
  if (scope.pending_reason === "service") return "请先在「设置」中完成服务配置。";
  if (scope.pending_reason === "recovering") return "正在核对记忆库归属、保存任务和清空记录，请等待检查完成。";
  if (scope.pending_reason === "authorization") return "请到「设置」点击「确认账号授权」，核对当前机器人身份。";
  return "机器人尚未连接，请先在 AstrBot 中连接。";
}

function selectScope(id) {
  bootstrapReadRevision++;
  maintenanceReadRevision++;
  backgroundReadRevision++;
  backgroundDraft = null;
  scopeId = id;
  $("scope-picker").value = id;
  modeDirty = false;
  maintenanceState = null;
  maintenanceIdentity = "";
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
    const contract = (data.platforms || []).find((item) => item.id === platform.value);
    const previous = (data.config.allowed_accounts || []).some((item) => item.platform_instance_id === platform.value && item.user_id === userId);
    if (!platform.value || (platform.selectedOptions[0]?.disabled && !previous)) throw new Error("请为每个用户选择可用的平台实例。");
    if (!userId || /[:\x00-\x1f\x7f]/.test(userId)) throw new Error("请填写有效的用户 ID。");
    if (contract?.numeric_user_id && !/^[0-9]{1,20}$/.test(userId)) throw new Error(`${contract.user_label}只能填写数字。`);
    const identity = JSON.stringify([platform.value, userId]);
    if (seen.has(identity)) throw new Error(`用户 ${userId} 已经添加，请移除重复行。`);
    seen.add(identity);
    payload.allowed_accounts.push({ platform_instance_id: platform.value, user_id: userId });
  }
  return payload;
}

async function prepareAuthorization(button, platform, user) {
  await perform(button, "setup", async () => {
    const payload = { action: "prepare", platform_instance_id: platform, user_id: user, config_revision: settingsRevision };
    const response = await bridge.apiPost("accounts/authorize", payload);
    authorizationState = { ...payload, action: "confirm", confirmation: response.confirmation };
    $("authorization-description").textContent = `${response.message} 授权后仅接收新消息。原记忆模式和保存、查询的暂停状态保持不变。`;
    $("authorization-dialog").showModal();
  });
}

$("cancel-authorization").addEventListener("click", () => $("authorization-dialog").close());
$("authorization-dialog").addEventListener("close", () => { authorizationState = null; });
async function confirmAuthorization() {
  if (!authorizationState) return;
  const pending = authorizationState;
  authorizationState = null;
  await perform($("confirm-authorization"), "authorization", async () => {
    let response;
    try {
      response = await bridge.apiPost("accounts/authorize", pending);
    } finally {
      $("authorization-dialog").close();
    }
    await refreshBootstrap({ automatic: true });
    notify(response.message);
  });
}
$("confirm-authorization").addEventListener("click", () => void confirmAuthorization().catch(() => {}));

async function checkConnection(payload) {
  const response = await bridge.apiPost("connection/check", payload);
  if (response.ok !== true) throw new Error(response.message || "连接未成功，请检查 API 地址和 Key。");
  notify(response.message || "记忆服务连接成功。", false, "connection-result");
  return response;
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function watchModels(requestId = "") {
  if (modelWatch) return modelWatch;
  bootstrapReadRevision++;
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
        $("model-progress").textContent = "模型已验证并保存，服务已启动。";
        await refreshBootstrap();
        notify("模型已验证并保存，服务已启动。");
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
    ? "连接其他服务会使用独立记忆。旧记忆保存在原服务；同一服务搬迁请按备份与恢复指南操作。"
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
      repair: button === $("reinstall") && !data?.backend?.version,
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
    notify(button === $("reinstall")
      ? "同版本修复命令已生成。请在原服务器、原安装路径执行，保留原模型和记忆数据。"
      : "安装命令已生成。执行后在下方选择模型并保存。");
  });
}
$("prepare").addEventListener("click", () => void prepareInstallation($("prepare")));
$("reinstall").addEventListener("click", () => void prepareInstallation($("reinstall")));
$("go-upgrade").addEventListener("click", () => {
  activateTab("setup", true);
  if (!data?.config.managed) $("external-upgrade").open = true;
  $(data?.config.managed ? "service-upgrade" : "external-upgrade").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("service-version-check").addEventListener("click", () => void perform($("service-version-check"), "setup", async () => {
  const revision = bootstrapReadRevision;
  const backend = await bridge.apiGet("service/status");
  if (revision !== bootstrapReadRevision) return;
  data.backend = backend;
  renderServiceVersion();
}));
for (const action of ["begin", "prepare", "verify", "resume", "cancel"]) {
  $("upgrade-" + action).addEventListener("click", () => void upgradeAction(action, $("upgrade-" + action)));
}
$("copy-upgrade").addEventListener("click", () => void perform($("copy-upgrade"), "copy", async () => {
  const field = $("upgrade-command");
  if (!field.value) throw new Error("请先生成升级命令。");
  field.focus();
  field.select();
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(field.value);
  } catch {
    if (!document.execCommand("copy")) throw new Error("浏览器限制了自动复制。命令已选中，请按 Ctrl+C，或长按选择复制。");
  }
  notify("升级命令已复制，请在原服务器终端执行。");
}));
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
async function refreshView() {
  if (!viewReady) {
    window.location.reload();
    return;
  }
  await refreshBootstrap();
  notify("状态已刷新。");
  $("update-error").hidden = true;
  scheduleRefresh();
}
$("refresh").addEventListener("click", () => void perform($("refresh"), "refresh", refreshView));
async function reloadSettings() {
  clearCommands();
  await refreshBootstrap({ hydrate: true, pollMaintenance: false });
  notify("设置已重新加载，未保存的更改已替换。请重新生成需要的命令。");
}
$("reload-settings").addEventListener("click", () => void perform($("reload-settings"), "setup", reloadSettings));
$("go-setup").addEventListener("click", () => activateTab("setup", true));
$("service-alert-setup").addEventListener("click", () => activateTab("setup", true));
$("dismiss-feedback").addEventListener("click", () => { $("feedback").hidden = true; clearTimeout(feedbackTimer); });
$("scope-picker").addEventListener("change", () => selectScope($("scope-picker").value));
$("scope-mode").addEventListener("change", () => { modeDirty = true; });
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
  if (action === "maintenance_status") {
    await readMaintenanceStatus();
    return;
  }
  await perform(button, "memory", async () => {
    const id = scopeId;
    maintenanceState = null;
    maintenanceCheckedAt = 0;
    renderScope();
    let response;
    try {
      response = await bridge.apiPost(endpoint("action", id), {
          action, ...extra, ...(action === "maintenance_begin" ? { config_revision: settingsRevision } : {}),
        });
    } catch (error) {
      if (action === "maintenance_begin") {
        await refreshBootstrap({ automatic: true, pollMaintenance: false }).catch(() => {});
      }
      throw error;
    }
    if (response.config_revision && response.config_revision !== settingsRevision) {
      clearUpgrade();
      settingsRevision = response.config_revision;
    }
    await refreshBootstrap();
    if (action === "maintenance_begin") $("discard-pending").checked = false;
    notify(action === "maintenance_begin" ? "已暂停保存和查询。" : response.message || "已恢复原记忆模式。");
  });
}
$("maintenance-begin").addEventListener("click", () => void maintenanceAction($("maintenance-begin"), "maintenance_begin", { discard_pending: false }));
$("maintenance-retry").addEventListener("click", () => void readMaintenanceStatus());
$("discard-pending").addEventListener("change", () => { $("maintenance-discard").disabled = !$("discard-pending").checked; });
$("maintenance-discard").addEventListener("click", () => void maintenanceAction($("maintenance-discard"), "maintenance_begin", { discard_pending: $("discard-pending").checked }));
$("maintenance-resume").addEventListener("click", () => void maintenanceAction($("maintenance-resume"), "maintenance_resume"));
$("background-load").addEventListener("click", () => void loadBackground());
$("background-save").addEventListener("click", () => void saveBackground());
$("background-close").addEventListener("click", () => void saveBackground(true));
$("background-page").addEventListener("change", () => { if (backgroundDraft) backgroundDraft.page_id = $("background-page").value; });
$("background-enabled").addEventListener("change", () => { if (backgroundDraft) backgroundDraft.enabled = $("background-enabled").checked; });

for (const id of ["control-plane-address", "scope-bank", "control-plane-key-command"]) $(id).addEventListener("focus", () => $(id).select());
for (const [details, field] of [["installation", "install-command"], ["uninstallation", "uninstall-command"]]) {
  $(details).addEventListener("toggle", () => { if (!$(details).open) $(field).value = ""; });
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearCommands();
});
window.addEventListener("pagehide", clearCommands);
window.addEventListener("pagehide", () => { backgroundReadRevision++; backgroundDraft = null; });
document.addEventListener("click", (event) => {
  document.querySelectorAll(".help[open]").forEach((help) => {
    if (!help.contains(event.target)) help.open = false;
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  document.querySelectorAll(".help[open]").forEach((help) => {
    help.open = false;
    help.querySelector("summary").focus();
  });
});
function clearCommands() {
  $("installation").open = false;
  $("uninstallation").open = false;
  $("install-command").value = "";
  $("uninstall-command").value = "";
  clearUpgrade();
}

function clearUpgrade() {
  $("upgrade-command").value = "";
  $("upgrade-installation").hidden = true;
  $("upgrade-backup-confirmed").checked = false;
}

$("forget").addEventListener("click", () => void perform($("forget"), "memory", async () => {
  const id = scopeId;
  const response = await bridge.apiPost(endpoint("action", id), { action: "forget_prepare" });
  if (!response.confirmation) throw new Error("无法准备清空操作，请重试。");
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
    maintenanceReadRevision++;
    $("forget-agree").disabled = true;
    $("cancel-forget").disabled = true;
    try {
      const response = await bridge.apiPost(endpoint("action", pending.id), { action: "forget_confirm", confirmation: pending.confirmation });
      forgetState = null;
      $("forget-dialog").close();
      await refreshBootstrap();
      notify(response.message || "正在清空旧记忆，记忆功能已暂停。清理完成后，可切换为「保存并自动使用记忆」重新开始。");
    } finally {
      $("forget-agree").disabled = false;
      $("cancel-forget").disabled = false;
      // A failed confirmation may be expired. Require a fresh prepare instead of reusing it.
      if ($("forget-dialog").open) {
        forgetState = null;
        $("forget-agree").checked = false;
        $("forget-agree").disabled = true;
        $("forget-message").textContent = "操作未完成。请关闭此窗口，重新点击「清空此用户的记忆」。";
      }
    }
  }, "forget-feedback").finally(() => { $("confirm-forget").disabled = true; });
});

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
    theme(context);
    offContext?.();
    offContext = bridge.onContext?.(theme);
    await refreshBootstrap({ hydrate: true, pollMaintenance: false });
    $("refresh").disabled = false;
    $("refresh").textContent = "重新获取";
    $("update-error").hidden = true;
    activateTab(data.config.configured ? "memories" : "setup");
    viewReady = true;
    scheduleRefresh();
    void readMaintenanceStatus().catch((error) => notify(errorMessage(error), true));
  } catch (error) {
    $("startup").textContent = errorMessage(error);
    $("startup").classList.add("error");
    $("startup").setAttribute("role", "alert");
    $("refresh").disabled = false;
    $("refresh").textContent = viewReady ? "重新获取" : "重新加载页面";
    $("update-error").hidden = false;
    $("update-error-message").textContent = "页面加载失败，请重试。";
  } finally {
    startRunning = false;
  }
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(refreshTimer);
  if (!document.hidden) {
    if (data) renderScope();
    scheduleRefresh(0);
  }
});
window.addEventListener("beforeunload", () => { clearTimeout(refreshTimer); clearTimeout(maintenanceExpiryTimer); offContext?.(); }, { once: true });
void start();
