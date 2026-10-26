const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
const modeLabels = { active: "正常记忆", readonly: "只读记忆", paused: "暂停记忆" };
const modeHelp = {
  active: "读取已有记忆，并保存新的记忆。",
  readonly: "读取已有记忆，不保存新的记忆。",
  paused: "不读取记忆，也不保存新的记忆。",
};
const memoryTypes = { world: "事实记忆", experience: "对话经历", observation: "归纳记忆" };
let data = null;
let settingsRevision = "";
let scopeId = "";
let page = 1;
let searchMode = false;
let total = 0;
const pageCounts = new Map();
let listVersion = 0;
let detailVersion = 0;
let detailMemoryId = null;
let detailScopeId = "";
let forgetState = null;
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
  return error?.message || "操作未完成，请稍后重试。";
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
  }
}

function activateTab(name, focus = false) {
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
  select.replaceChildren(option("", kind === "rerank" ? "不使用重排，使用默认排序" : "请选择模型"));
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
    ? `缺少可用的${missing.join("、")}模型。请到 AstrBot「模型供应商」添加对应模型，再回到这里刷新状态。`
    : "选择 AstrBot 已配置的模型。重排模型可不选，记忆服务会使用默认排序。";
  $("prepare").disabled = missing.length > 0 || locks.has("setup");
}

function clearCommand() {
  $("installation").hidden = true;
  $("install-command").value = "";
  $("install-message").textContent = "";
}

function updateConnectionMode() {
  const managed = document.querySelector('[name="connection-mode"]:checked').value === "managed";
  $("managed-settings").hidden = !managed;
  $("connection-heading").textContent = managed ? "安装完成后连接" : "连接已有 Hindsight";
}

function addAccount(account = {}) {
  const row = el("div", "account-row");
  const platformLabel = el("label", "", "机器人 / 平台实例");
  const select = el("select");
  select.className = "account-platform";
  select.required = true;
  select.append(option("", "请选择已连接的机器人"));
  (data.platforms || []).forEach((platform) => select.append(option(platform.id, platform.name || platform.id)));
  if (account.platform_instance_id && !(data.platforms || []).some((item) => item.id === account.platform_instance_id)) {
    select.append(option(account.platform_instance_id, `${account.platform_instance_id}（实例不可用，请重新选择）`, true));
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
      select.append(option(selected, `${selected}（实例不可用，请重新选择）`, true));
    }
    select.value = selected;
  });
}

function hydrateConfig() {
  const config = data.config || {};
  settingsRevision = data.config_revision;
  $("api-url").value = config.api_url || "";
  $("api-key").value = "";
  $("api-key").placeholder = config.has_api_key ? "已保存，留空继续使用" : "填写记忆服务的 API Key";
  $("key-help").textContent = config.has_api_key ? "已保存 API Key。留空保留，填写新值则替换。" : "填写记忆服务的连接密钥。";
  let browserTimezone = "Asia/Shanghai";
  try { browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || browserTimezone; } catch { /* Use the fallback when Intl is unavailable. */ }
  $("timezone").value = config.timezone || browserTimezone;
  $("enabled").checked = config.enabled === true;
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
  };
  return Object.entries(counts).map(([key, value]) => `${labels[key] || key}：${text(value)}`).join("，") || "暂无后台任务";
}

function renderStatus() {
  const status = data.status || {};
  const connected = status.connected;
  $("status-label").textContent = status.recovering ? "正在恢复记忆服务"
    : connected === true ? "服务已连接" : status.label || (connected === false ? "服务未连接" : "连接状态未知");
  $("status-dot").className = `status-dot${!status.recovering && connected === true ? " good" : connected === false ? " bad" : ""}`;
  $("status-description").textContent = status.recovering ? "恢复完成后再使用记忆管理，可点击刷新状态查看进度。"
    : connected === true ? "已取得服务连接状态。用户记忆的处理情况见下方。"
    : connected === false ? "请在首次设置中检查 API 地址和 Key，再检查连接。"
    : "服务尚未返回明确的连接结果，请检查连接或稍后刷新。";
  $("enabled-label").textContent = data.config?.enabled ? "长期记忆已启用" : "长期记忆未启用";
  $("scope-statuses").replaceChildren();
  if (!data.scopes?.length) {
    const empty = el("div", "empty");
    empty.append(el("h3", "", "还没有配置用户"), el("p", "", "到首次设置中添加用户并保存，即可为其开启记忆。"));
    $("scope-statuses").append(empty);
  }
  (data.scopes || []).forEach((scope) => {
    const row = el("article", "scope-status");
    const heading = el("div", "scope-status-header");
    const manage = el("button", "", "管理记忆");
    manage.type = "button";
    manage.addEventListener("click", () => {
      if (locks.has("memory")) return;
      selectScope(scope.scope_id);
      activateTab("memories", true);
    });
    heading.append(el("strong", "", scopeName(scope)), el("span", "badge", modeLabels[scope.mode] || "模式未知"), manage);
    row.append(heading, el("p", "hint", countDescription(scope.summary?.counts)));
    if (scope.summary?.last_error) row.append(el("p", "hint error-text", scope.summary.last_error));
    $("scope-statuses").append(row);
  });
}

async function refreshBootstrap({ hydrate = false } = {}) {
  const next = await bridge.apiGet("bootstrap");
  if (!next || !next.config || !Array.isArray(next.scopes)) throw new Error("设置数据不完整，请刷新页面重试。");
  data = next;
  if (hydrate) hydrateConfig();
  else {
    for (const kind of ["chat", "embedding", "rerank"]) providerSelect(kind, $(`${kind}-provider`).value);
    refreshAccountPlatforms();
    updateProviderHelp();
  }
  $("platform-help").textContent = data.platforms?.length ? "只对这里列出的用户启用记忆。请先移除不需要的空白行，再保存。" : "未找到平台实例。请先在 AstrBot 中连接 QQ 机器人，再刷新此页。";
  renderStatus();
  const previous = scopeId;
  const scopes = data.scopes || [];
  scopeId = scopes.some((scope) => scope.scope_id === previous) ? previous : (scopes[0]?.scope_id || "");
  $("scope-picker").replaceChildren();
  scopes.forEach((scope) => $("scope-picker").append(option(scope.scope_id, scopeName(scope))));
  if (!scopes.length) $("scope-picker").append(option("", "暂无用户"));
  $("scope-picker").value = scopeId;
  $("memory-fields").disabled = !scopeId || locks.has("memory");
  $("setup-fields").disabled = locks.has("setup");
  $("no-scopes").hidden = scopes.length > 0;
  renderScope();
  if (previous !== scopeId) {
    pageCounts.clear();
    page = 1;
    searchMode = false;
    $("query").value = "";
    if (scopeId) await loadMemories();
    else {
      listVersion++;
      $("memory-list").replaceChildren();
      $("list-summary").textContent = "暂无用户。";
      $("pagination").hidden = true;
    }
  }
  $("startup").hidden = true;
}

function renderScope() {
  const scope = data?.scopes.find((item) => item.scope_id === scopeId);
  $("scope-mode").value = scope?.mode || "";
  $("mode-help").textContent = modeHelp[scope?.mode] || "记忆模式未知，请刷新状态。";
  $("scope-error").hidden = !scope?.summary?.last_error;
  $("scope-error").textContent = text(scope?.summary?.last_error);
}

function selectScope(id) {
  scopeId = id;
  $("scope-picker").value = id;
  page = 1;
  searchMode = false;
  pageCounts.clear();
  $("query").value = "";
  renderScope();
  void perform(null, "memory", loadMemories);
}

function endpoint(suffix, id = scopeId) {
  if (!id) throw new Error("请先选择用户。");
  return `scopes/${encodeURIComponent(id)}/${suffix}`;
}

function renderMemories(items, searching) {
  const list = $("memory-list");
  list.replaceChildren();
  if (!items.length) {
    const empty = el("div", "empty");
    empty.append(el("h3", "", searching ? "没有找到相关记忆" : "还没有保存的记忆"), el("p", "", searching ? "换一个说法，或点击「查看全部」浏览。" : "启用记忆后正常聊天，保存的内容会显示在这里。"));
    list.append(empty);
  }
  items.forEach((item) => {
    const card = el("article", "memory-card");
    const meta = el("div", "memory-meta");
    meta.append(el("span", "badge", memoryTypes[item.type] || text(item.type) || "记忆"));
    if (item.date) meta.append(el("span", "", item.date));
    const detail = el("button", "", "查看详情 / 更正");
    detail.type = "button";
    detail.disabled = item.id == null;
    detail.addEventListener("click", () => void openDetail(item.id, scopeId));
    card.append(meta, el("p", "", item.text || "此条记忆没有文本内容。"), detail);
    list.append(card);
  });
}

async function loadMemories(requestedPage = page) {
  const version = ++listVersion;
  const id = scopeId;
  if (!id) return;
  const searching = searchMode;
  $("memory-list").setAttribute("aria-busy", "true");
  $("list-summary").textContent = "正在读取记忆…";
  $("memory-list").replaceChildren();
  $("pagination").hidden = true;
  try {
    const response = searching
      ? await bridge.apiPost(endpoint("search", id), { query: $("query").value.trim() })
      : await bridge.apiGet(endpoint("memories", id), { page: requestedPage, query: "" });
    if (version !== listVersion || id !== scopeId) return;
    const items = (searching ? response.results : response.items) || [];
    if (!Array.isArray(items)) throw new Error("记忆列表格式不正确，请稍后刷新。");
    total = searching ? items.length : Number(response.total);
    page = searching ? 1 : Number(response.page) || requestedPage;
    if (!searching) pageCounts.set(page, items.length);
    renderMemories(items, searching);
    $("list-summary").textContent = searching ? `找到 ${items.length} 条相关记忆` : Number.isFinite(total) ? `共 ${total} 条记忆` : "全部记忆";
    $("pagination").hidden = searching || !items.length;
    $("page-number").textContent = `第 ${page} 页`;
    $("previous").disabled = page <= 1;
    // Count visited pages rather than assume an undocumented page size.
    const visitedCount = [...pageCounts.entries()].filter(([number]) => number <= page).reduce((sum, [, count]) => sum + count, 0);
    $("next").disabled = !Number.isFinite(total) || !items.length || visitedCount >= total;
    if (!searching && !items.length && page > 1) {
      $("list-summary").textContent = "已经到最后一页，点击上一页返回。";
      $("pagination").hidden = false;
      $("next").disabled = true;
    }
  } catch (error) {
    if (version === listVersion) $("list-summary").textContent = "记忆读取失败，请点击「查看全部」重试。";
    throw error;
  } finally {
    if (version === listVersion) $("memory-list").setAttribute("aria-busy", "false");
  }
}

function renderDetail(record, corrections = []) {
  const content = $("detail-content");
  content.replaceChildren();
  if (typeof record !== "object" || !record) {
    content.append(el("p", "", record || "未返回记忆内容。"));
  } else {
    const labels = { id: "记忆编号", text: "记忆内容", type: "类型", date: "时间", context: "相关语境", document_id: "来源文档" };
    const description = el("dl");
    Object.entries(record).forEach(([key, value]) => {
      if (value == null || value === "") return;
      const row = el("div", "detail-field");
      row.append(el("dt", "", labels[key] || key), el("dd", "", key === "type" ? memoryTypes[value] || value : value));
      description.append(row);
    });
    content.append(description);
  }
  $("corrections-section").hidden = !corrections.length;
  $("corrections").replaceChildren(...corrections.map((correction) => el("p", "", typeof correction === "object" ? correction.text || correction : correction)));
}

async function openDetail(memoryId, id) {
  const version = ++detailVersion;
  detailMemoryId = memoryId;
  detailScopeId = id;
  $("detail-content").textContent = "正在读取详情…";
  $("corrections-section").hidden = true;
  $("correction-text").value = "";
  $("detail-feedback").textContent = "";
  $("correction-fields").disabled = true;
  $("detail-dialog").showModal();
  try {
    const response = await bridge.apiGet(endpoint("detail", id), { memory_id: memoryId });
    if (version !== detailVersion || !$("detail-dialog").open) return;
    renderDetail(response.record, response.corrections || []);
    $("correction-fields").disabled = false;
  } catch (error) {
    if (version === detailVersion) {
      $("detail-content").textContent = "详情读取失败。关闭后可以重新打开。";
      notify(errorMessage(error), true, "detail-feedback");
    }
  }
}

function connectionPayload() {
  const apiUrl = $("api-url").value.trim();
  let parsed;
  try { parsed = new URL(apiUrl); } catch { throw new Error("请填写完整的 API 地址，例如 http://hindsight:8888。"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("API 地址应使用 http 或 https，密钥请填写在 API Key 中。");
  return { api_url: apiUrl, api_key: $("api-key").value.trim() };
}

function settingsPayload(enabled) {
  const payload = {
    ...connectionPayload(), enabled, timezone: $("timezone").value.trim(),
    allowed_accounts: [], config_revision: settingsRevision,
  };
  if (!payload.timezone) throw new Error("请填写时区，例如 Asia/Shanghai。");
  try { new Intl.DateTimeFormat("zh-CN", { timeZone: payload.timezone }); } catch { throw new Error("时区无法识别，请填写例如 Asia/Shanghai。"); }
  const seen = new Set();
  for (const row of $("accounts").children) {
    const platform = row.querySelector(".account-platform");
    const user = row.querySelector(".account-user");
    const userId = user.value.trim();
    if (!platform.value || platform.selectedOptions[0]?.disabled) throw new Error("请为每个用户选择可用的机器人平台实例。");
    if (!/^[0-9]+$/.test(userId)) throw new Error("用户 QQ 号只能填写数字。");
    const identity = JSON.stringify([platform.value, userId]);
    if (seen.has(identity)) throw new Error(`用户 ${userId} 已经添加，请移除重复行。`);
    seen.add(identity);
    payload.allowed_accounts.push({ platform_instance_id: platform.value, user_id: userId });
  }
  if (enabled && !payload.allowed_accounts.length) throw new Error("请至少添加一个用户，再启用长期记忆。");
  return payload;
}

async function checkConnection(payload) {
  const response = await bridge.apiPost("connection/check", payload);
  if (response.ok !== true) throw new Error(response.message || "连接未成功，请检查 API 地址和 Key。");
  notify(response.message || "记忆服务连接成功。", false, "connection-result");
  return response;
}

async function saveSettings(forceEnable) {
  // Validate before disabling fields; browser constraint validation ignores disabled controls.
  if (!$("settings-form").reportValidity()) return;
  const payload = settingsPayload(forceEnable || $("enabled").checked);
  await perform(forceEnable ? $("connect-enable") : $("save-settings"), "setup", async () => {
    if (forceEnable) await checkConnection({ api_url: payload.api_url, api_key: payload.api_key });
    const response = await bridge.apiPost("settings/save", payload);
    if (response.saved !== true) throw new Error(response.message || "设置未保存，请重试。");
    $("api-key").value = "";
    clearCommand();
    await refreshBootstrap({ hydrate: true });
    notify(response.message || (forceEnable ? "已连接并启用记忆，正常聊天即可。" : "设置已保存。"));
  });
}

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void saveSettings(false).catch((error) => notify(errorMessage(error), true));
});
$("connect-enable").addEventListener("click", () => void saveSettings(true).catch((error) => notify(errorMessage(error), true)));
$("check-connection").addEventListener("click", () => void perform($("check-connection"), "setup", async () => {
  await checkConnection(connectionPayload());
}));
$("api-url").addEventListener("input", () => {
  const changed = data.config.api_url
    && $("api-url").value.trim().replace(/\/+$/, "") !== data.config.api_url.replace(/\/+$/, "");
  $("connection-result").textContent = changed
    ? "连接其他服务会使用独立记忆。旧记忆保存在原服务；同一服务搬迁请按数据维护指南操作。"
    : "地址已修改，请重新检查连接。";
});
$("api-key").addEventListener("input", () => { $("connection-result").textContent = "密钥已修改，请重新检查连接。"; });
$("add-account").addEventListener("click", () => addAccount().focus());
document.querySelectorAll('[name="connection-mode"]').forEach((radio) => radio.addEventListener("change", () => {
  clearCommand();
  updateConnectionMode();
}));
for (const kind of ["chat", "embedding", "rerank"]) $(`${kind}-provider`).addEventListener("change", clearCommand);
$("prepare").addEventListener("click", () => void perform($("prepare"), "setup", async () => {
  clearCommand();
  const selection = { config_revision: settingsRevision };
  for (const kind of ["chat", "embedding", "rerank"]) {
    const select = $(`${kind}-provider`);
    if ((kind !== "rerank" && !select.value) || select.selectedOptions[0]?.disabled) throw new Error("请选择可用的聊天模型和嵌入模型，重排模型可以不选。");
    selection[`${kind}_provider_id`] = select.value;
  }
  const response = await bridge.apiPost("install/prepare", selection);
  if (!response.command) throw new Error("未生成安装命令，请稍后重试。");
  if (response.config_revision) settingsRevision = response.config_revision;
  $("install-command").value = response.command;
  $("install-message").textContent = response.message || "";
  $("installation").hidden = false;
  if (response.api_url) $("api-url").value = response.api_url;
  $("install-command").focus();
  notify("安装命令已生成。安装完成后，点击「连接并启用」。");
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
  notify("安装命令已复制。");
}));
$("refresh").addEventListener("click", () => void perform($("refresh"), "refresh", async () => {
  await refreshBootstrap({ hydrate: !data });
  if (scopeId && !locks.has("memory")) await perform(null, "memory", loadMemories);
  notify("状态已刷新。");
}));
$("go-setup").addEventListener("click", () => activateTab("setup", true));
$("dismiss-feedback").addEventListener("click", () => { $("feedback").hidden = true; clearTimeout(feedbackTimer); });
$("scope-picker").addEventListener("change", () => selectScope($("scope-picker").value));
$("scope-mode").addEventListener("change", () => { $("mode-help").textContent = modeHelp[$("scope-mode").value]; });
$("save-mode").addEventListener("click", () => void perform($("save-mode"), "memory", async () => {
  const mode = $("scope-mode").value;
  if (!modeLabels[mode]) throw new Error("请选择记忆模式。");
  const response = await bridge.apiPost(endpoint("action"), { action: "mode", mode });
  await refreshBootstrap();
  notify(response.message || `已切换为${modeLabels[mode]}。`);
}));
$("retry").addEventListener("click", () => void perform($("retry"), "memory", async () => {
  const response = await bridge.apiPost(endpoint("action"), { action: "retry" });
  await refreshBootstrap();
  notify(response.message || "已提交重试，请稍后刷新查看结果。");
}));
$("export").addEventListener("click", () => void perform($("export"), "memory", async () => {
  await bridge.download(endpoint("export"));
  notify("记忆 JSON 已交给浏览器下载。");
}));
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!$("query").value.trim()) { notify("请先输入要搜索的内容。", true); $("query").focus(); return; }
  searchMode = true;
  page = 1;
  void perform($("search"), "memory", loadMemories);
});
$("show-all").addEventListener("click", () => {
  searchMode = false;
  page = 1;
  pageCounts.clear();
  $("query").value = "";
  void perform($("show-all"), "memory", loadMemories);
});
for (const [id, delta] of [["previous", -1], ["next", 1]]) $(id).addEventListener("click", () => {
  void perform(null, "memory", () => loadMemories(Math.max(1, page + delta)));
});
$("close-detail").addEventListener("click", () => { if (!locks.has("correction")) $("detail-dialog").close(); });
$("detail-dialog").addEventListener("cancel", (event) => { if (locks.has("correction")) event.preventDefault(); });
$("detail-dialog").addEventListener("close", () => { detailVersion++; detailMemoryId = null; $("correction-text").value = ""; });
$("correction-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const correction = $("correction-text").value.trim();
  if (!correction) { notify("请填写准确的信息。", true, "detail-feedback"); return; }
  const id = detailScopeId;
  const memoryId = detailMemoryId;
  if (memoryId == null) return;
  void perform($("save-correction"), "correction", async () => {
    const response = await bridge.apiPost(endpoint("action", id), { action: "correct", memory_id: memoryId, text: correction });
    $("correction-text").value = "";
    notify(response.message || "更正已保存。", false, "detail-feedback");
    const detail = await bridge.apiGet(endpoint("detail", id), { memory_id: memoryId });
    renderDetail(detail.record, detail.corrections || []);
    await refreshBootstrap();
    if (scopeId === id) await loadMemories();
  }, "detail-feedback");
});
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
      page = 1;
      pageCounts.clear();
      searchMode = false;
      $("query").value = "";
      await refreshBootstrap();
      if (scopeId) await loadMemories();
      notify(response.message || "此用户的全部记忆已清空。");
    } finally {
      $("forget-agree").disabled = false;
      $("cancel-forget").disabled = false;
      // A failed confirmation may be expired. Require a fresh prepare instead of reusing it.
      if ($("forget-dialog").open) {
        forgetState = null;
        $("forget-agree").checked = false;
        $("forget-agree").disabled = true;
        $("forget-message").textContent = "操作未完成。请关闭此窗口，重新点击「完整遗忘此用户」获取确认信息。";
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
    window.addEventListener("beforeunload", () => { off?.(); clearCommand(); }, { once: true });
    await refreshBootstrap({ hydrate: true });
    $("refresh").disabled = false;
    if (data.config.enabled && data.config.api_url) activateTab("status");
  } catch (error) {
    $("startup").textContent = errorMessage(error);
    $("startup").classList.add("error");
    $("startup").setAttribute("role", "alert");
    $("refresh").disabled = !bridge;
  }
}
void start();
