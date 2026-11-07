"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../views/memory/app.js"), "utf8");
function declaration(name) {
  const begin = source.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.notEqual(begin, -1);
  return source.slice(begin, source.indexOf("\n}\n", begin) + 3);
}

function harness() {
  const nodes = new Map();
  function node() {
    return {
      children: [], textContent: "", hidden: false, disabled: false, value: "", dataset: {}, listeners: {},
      classList: { add() {}, remove() {}, toggle() {} },
      get options() { return this.children; },
      querySelector() { return node(); }, closest() { return null; },
      append(...children) { this.children.push(...children); },
      prepend(...children) { this.children.unshift(...children); },
      replaceChildren(...children) { this.children = children; },
      attributes: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this[name === "maxlength" ? "maxLength" : name]; },
      showModal() { this.open = true; }, close() { this.open = false; },
      focus() {}, select() {}, reset() {}, reportValidity() { return true; },
    };
  }
  const context = vm.createContext({
    console, Promise, Date, Error, URL,
    setTimeout: (callback, milliseconds) => milliseconds > 20000 ? context.expiryTimer(callback, milliseconds) : setTimeout(callback, 5),
    clearTimeout: (timer) => { if (timer?.callback) timer.cancelled = true; else clearTimeout(timer); },
    expiryTimers: [],
    expiryTimer(callback, milliseconds) {
      const timer = { callback, milliseconds, cancelled: false };
      this.expiryTimers.push(timer);
      return timer;
    },
    $: (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    el: (tag, kind, content) => Object.assign(node(), { textContent: content, className: kind || "" }),
    window: { AstrBotPluginView: null, location: { reload() { context.reloads++; } } },
    document: { documentElement: { dataset: {} }, querySelector: (selector) => selector.startsWith("[data-mode=")
      ? context.$("mode-" + selector.match(/"([^"]+)"/)[1]) : ({ value: "existing" }) },
    data: null, bridge: null, startRunning: false, viewReady: false, offContext: undefined, reloads: 0,
    locks: new Set(), scopeId: "", selectedAccount: "", maintenanceScopeId: "", modeDirty: false, maintenanceState: null,
    maintenanceIdentity: "", maintenanceCheckedAt: 0,
    maintenanceExpiryTimer: undefined,
    authorizationState: null, settingsRevision: "", parameterDraft: null,
    parameterKeys: ["background_max_tokens", "recall_max_tokens", "context_max_chars",
      "recall_timeout", "background_timeout", "recall_query_tokens", "query_tokenizer",
      "queue_max_tasks", "queue_max_mib", "command_prefixes"],
    renderParameters() {},
    maintenanceReadRevision: 0, bootstrapReadRevision: 0, statusSignature: "",
    versionCheckRevision: 0, versionCheck: null,
    managedSetup: null, managedReadRevision: 0,
    backgroundReadRevision: 0, backgroundDraft: null, readingDraft: null, reflectDraft: null,
    renderBackground() {}, renderReading() {}, renderReflect() {}, renderScope() {},
    clearCommands() {},
    renderServiceVersion() {},
    clearUpgrade() {},
    modelWatch: null, feedbackTimer: null,
    hydrateConfig() {}, providerSelect() {}, refreshAccountPlatforms() {}, updateProviderHelp() {},
    readMaintenanceStatus: async () => {},
    option: (value, label, disabled = false) => Object.assign(node(), { value, textContent: label, disabled }),
    activateTab() {}, scheduleRefresh() {}, modeLabels: { active: "读写记忆", readonly: "只读记忆", paused: "停用记忆" },
    scopeName: () => "user", countDescription: () => "queue", maintenanceDescription: () => "maintenance",
    refreshBootstrap: async () => {
      context.data = { config: { configured: true } };
      context.$("startup").hidden = true;
    },
  });
  for (const name of ["withTimeout", "errorMessage", "managedMode", "versionCheckIdentity", "maintenanceSnapshot", "syncMaintenanceState", "scheduleMaintenanceExpiry", "start", "refreshView", "notify", "recallDescription", "renderStatus", "updateConnectionMode", "accountKey", "accountScopes", "updateUserChoices", "maintenanceScope", "renderMaintenance"]) {
    vm.runInContext(declaration(name), context);
  }
  return context;
}

function parameterHarness() {
  const context = harness();
  for (const name of ["text", "renderParameterLimits", "renderParameters", "parameterPayload", "perform", "saveParameters"]) {
    vm.runInContext(declaration(name), context);
  }
  const values = { background_max_tokens: 400, recall_max_tokens: 1200, context_max_chars: 4000,
    recall_timeout: 10, background_timeout: 1, recall_query_tokens: 380, query_tokenizer: "o200k_base",
    queue_max_tasks: 10000, queue_max_mib: 64, command_prefixes: [] };
  context.data = { config_revision: "a", config: {}, parameters: {
    values, effective: values, running: true, deployment: { service_id: "service", agent_id: "default" },
  } };
  context.settingsRevision = "a";
  context.refreshBootstrap = async () => { context.renderParameters(); };
  context.renderParameters();
  return context;
}

test("memory settings polling retains drafts and exposes conflicts without changing saved values", () => {
  const context = parameterHarness();
  context.$("memory-background_max_tokens").value = "4096";
  context.parameterDraft.dirty = true;
  context.renderParameters();
  assert.equal(context.$("memory-background_max_tokens").value, "4096");
  assert.equal(context.$("parameters-conflict").hidden, true);
  context.data.config_revision = "b";
  context.data.parameters.values = { ...context.data.parameters.values, background_max_tokens: 2000 };
  context.renderParameters();
  assert.equal(context.$("memory-background_max_tokens").value, "4096");
  assert.equal(context.parameterDraft.revision, "a");
  assert.equal(context.$("parameters-conflict").hidden, false);
  context.renderParameters({ discard: true });
  assert.equal(context.$("memory-background_max_tokens").value, "2000");
  assert.equal(context.parameterDraft.revision, "b");
});

test("memory parameter saves use an independent payload and acknowledge the returned runtime values", async () => {
  const context = parameterHarness();
  context.$("api-url").value = "http://unsaved-connection";
  context.$("memory-background_max_tokens").value = "4096";
  context.$("memory-command_prefixes").value = "!\n#\n";
  context.parameterDraft.dirty = true;
  context.bridge = { apiPost: async (route, payload) => {
    assert.equal(route, "parameters/save");
    assert.deepEqual(Object.keys(payload).sort(), ["config_revision", "parameters"]);
    assert.equal(payload.config_revision, "a");
    assert.equal(payload.parameters.background_max_tokens, 4096);
    assert.equal(JSON.stringify(payload.parameters.command_prefixes), '["!","#"]');
    return { saved: true, config_revision: "b", message: "已保存并生效",
      parameters: { ...context.data.parameters, values: payload.parameters, effective: payload.parameters } };
  } };
  await context.saveParameters();
  assert.equal(context.$("api-url").value, "http://unsaved-connection");
  assert.equal(context.settingsRevision, "b");
  assert.equal(context.parameterDraft.revision, "b");
  assert.equal(context.parameterDraft.dirty, false);
  assert.equal(context.data.parameters.effective.background_max_tokens, 4096);
  assert.match(context.$("parameters-feedback").textContent, /已保存/);
  assert.equal(context.locks.size, 0);
});

test("failed parameter saves retain input and revision and reject invalid numeric serialization", async () => {
  const context = parameterHarness();
  context.$("memory-context_max_chars").value = "24000";
  context.parameterDraft.dirty = true;
  context.bridge = { apiPost: async () => { throw new Error("配置已被更新"); } };
  await context.saveParameters();
  assert.equal(context.$("memory-context_max_chars").value, "24000");
  assert.equal(context.parameterDraft.revision, "a");
  assert.equal(context.parameterDraft.dirty, true);
  assert.match(context.$("parameters-feedback").textContent, /配置已被更新/);
  for (const raw of ["", "1.2", "NaN", "9007199254740992"]) {
    context.$("memory-queue_max_tasks").value = raw;
    assert.throws(() => context.parameterPayload(), /有效数值/);
  }
});

test("a late parameter save response cannot overwrite newer bootstrap values", async () => {
  const context = parameterHarness();
  context.parameterDraft.dirty = true;
  context.$("memory-background_max_tokens").value = "4096";
  let finish;
  context.bridge = { apiPost: () => new Promise((resolve) => { finish = resolve; }) };
  const saving = context.saveParameters();
  context.data.config_revision = "newer";
  context.data.parameters.values = { ...context.data.parameters.values, background_max_tokens: 3000 };
  finish({ saved: true, config_revision: "saved", parameters: { ...context.data.parameters,
    values: { ...context.data.parameters.values, background_max_tokens: 4096 } } });
  await saving;
  assert.equal(context.data.config_revision, "newer");
  assert.equal(context.data.parameters.values.background_max_tokens, 3000);
  assert.equal(context.$("memory-background_max_tokens").value, "4096");
  assert.equal(context.parameterDraft.dirty, true);
  assert.match(context.$("parameters-feedback").textContent, /设置已更新/);
});

test("manual version feedback survives polling and rejects results from a changed connection", async () => {
  const context = harness();
  for (const name of ["renderServiceVersion", "checkServiceVersion"]) vm.runInContext(declaration(name), context);
  context.data = { config: { configured: true, connection_revision: "a", managed: true }, backend: {} };
  let finish;
  context.bridge = { apiGet: () => new Promise((resolve) => { finish = resolve; }) };
  const checking = context.checkServiceVersion();
  context.bootstrapReadRevision++;
  context.renderServiceVersion();
  assert.match(context.$("version-check-result").textContent, /正在检查/);
  finish({ version: "1.2.0", target: "1.2.0", managed: true });
  await checking;
  assert.match(context.$("version-check-result").textContent, /已是插件支持的配套版本 1.2.0/);
  const older = context.checkServiceVersion();
  context.data.config.connection_revision = "b";
  context.renderServiceVersion();
  finish({ version: "1.1.0", managed: true, target: "1.2.0", upgrade_available: true });
  await older;
  assert.equal(context.$("version-check-result").hidden, true);
  assert.equal(context.data.backend.version, "1.2.0");
});

test("manual version timeout is visible and higher versions never suggest downgrading", async () => {
  const context = harness();
  for (const name of ["renderServiceVersion", "checkServiceVersion"]) vm.runInContext(declaration(name), context);
  context.data = { config: { configured: false }, backend: {} };
  context.bridge = { apiGet: () => new Promise(() => {}) };
  await context.checkServiceVersion();
  assert.match(context.$("version-check-result").textContent, /检查版本超时/);
  assert.equal(context.$("service-version").hidden, false);
  context.bridge.apiGet = async () => ({ version: "1.3.0", target: "1.2.0", managed: true });
  await context.checkServiceVersion();
  assert.match(context.$("version-check-result").textContent, /不执行降级/);
});

test("managed connection fields are readonly and saving the browser address cannot submit credentials", async () => {
  const context = harness();
  context.document.querySelector = () => ({ value: "managed" });
  context.data = { config: { configured: true }, has_installation: true };
  context.updateConnectionMode();
  assert.equal(context.$("api-url").readOnly, true);
  assert.equal(context.$("api-key-field").hidden, true);
  assert.equal(context.$("save-settings").hidden, true);
  assert.equal(context.$("save-access").hidden, false);
  for (const name of ["perform", "controlPlaneUrl", "saveAccess"]) vm.runInContext(declaration(name), context);
  context.$("control-plane-url").value = "https://memory.example.com";
  context.$("api-url").value = "http://owned-service:8888";
  context.$("api-key").value = "must-not-submit";
  context.bridge = { apiPost: async (route, payload) => {
    assert.equal(route, "access/save");
    assert.deepEqual(Object.keys(payload).sort(), ["config_revision", "control_plane_url"]);
    return { saved: true, config_revision: "saved", message: "管理地址已保存" };
  } };
  await context.saveAccess();
  assert.equal(context.settingsRevision, "saved");
});

test("version notices show upgrades only for the connected managed service", () => {
  const context = harness();
  vm.runInContext(declaration("renderServiceVersion"), context);
  context.data = { config: { configured: true, managed: true }, backend: {
    version: "1.1.0", target: "1.2.0", supported: true, managed: true, upgrade_available: true, pending: false,
  } };
  context.renderServiceVersion();
  assert.equal(context.$("service-upgrade").hidden, false);
  assert.equal(context.$("reinstall").disabled, true);
  assert.equal(context.$("upgrade-verify").disabled, true);
  context.data.config.managed = false;
  context.data.backend.managed = false;
  context.renderServiceVersion();
  assert.equal(context.$("service-upgrade").hidden, true);
  assert.equal(context.$("go-upgrade").hidden, true);
  assert.equal(context.$("upgrade-notice").hidden, true);
  context.data.backend.pending = true;
  context.data.backend.upgrade_paused = true;
  context.renderServiceVersion();
  assert.equal(context.$("upgrade-notice").hidden, true);
  assert.equal(context.$("save-settings").disabled, false);
  context.data.backend = { version: null, upgrade_available: false };
  context.renderServiceVersion();
  assert.equal(context.$("go-upgrade").hidden, true);
  assert.equal(context.$("upgrade-prepare").disabled, true);
  context.data = { config: { configured: true, managed: true }, backend: {
    version: "1.2.0", target: "1.2.0", managed: true, upgrade_available: false, pending: false,
    upgrade_paused: true, resume_available: true,
  } };
  context.renderServiceVersion();
  assert.equal(context.$("service-upgrade").hidden, false);
  assert.equal(context.$("upgrade-resume").hidden, false);
  assert.equal(context.$("upgrade-resume").disabled, false);
  assert.equal(context.$("upgrade-abandon").hidden, true);
  assert.equal(context.$("upgrade-cancel").disabled, true);
});

test("opening an upgrade cannot expose service management for an existing connection", () => {
  const context = harness();
  vm.runInContext(declaration("openUpgrade"), context);
  context.data = { config: { managed: false }, backend: { managed: true, upgrade_available: true } };
  context.openUpgrade();
  assert.notEqual(context.$("upgrade-dialog").open, true);
  context.data.config.managed = true;
  context.openUpgrade();
  assert.equal(context.$("upgrade-dialog").open, true);
});

test("a model task outside the connected service releases polling and setup controls", async () => {
  const context = harness();
  vm.runInContext(declaration("watchModels"), context);
  context.data = { config: { managed: false } };
  context.modelRequestId = "previous";
  context.bridge = { apiGet: async () => ({ status: "idle" }) };
  await context.watchModels("previous");
  assert.equal(context.modelWatch, null);
  assert.equal(context.modelRequestId, "");
  assert.equal(context.locks.has("setup"), false);
  assert.equal(context.$("setup-fields").disabled, false);
  assert.match(context.$("model-progress").textContent, /请求未确认/);
});

test("upgrade controls require backup confirmation and preserve failure state", async () => {
  const context = harness();
  vm.runInContext(declaration("upgradeAction"), context);
  let calls = 0;
  context.perform = async (button, group, work) => { await work(); };
  context.bridge = { apiPost: async () => { calls++; throw new Error("maintenance pending"); } };
  await assert.rejects(context.upgradeAction("prepare", {}));
  assert.equal(calls, 0);
  context.$("upgrade-backup-confirmed").checked = true;
  context.$("upgrade-command").value = "original";
  await assert.rejects(context.upgradeAction("prepare", {}));
  assert.equal(context.$("upgrade-command").value, "original");
  assert.equal(calls, 1);
});

test("leaving or rebinding upgrade preparation clears secret commands and backup agreement", () => {
  const context = harness();
  vm.runInContext(declaration("clearUpgrade"), context);
  context.$("upgrade-command").value = "private-command";
  context.$("upgrade-backup-confirmed").checked = true;
  context.clearUpgrade();
  assert.equal(context.$("upgrade-command").value, "");
  assert.equal(context.$("upgrade-installation").hidden, true);
  assert.equal(context.$("upgrade-backup-confirmed").checked, false);
});

test("missing SDK and singleton pending handshake require reloading the plugin frame", async () => {
  const context = harness();
  await context.start();
  assert.match(context.$("startup").textContent, /插件详情页/);
  assert.equal(context.$("refresh").disabled, false);
  const pendingReady = new Promise(() => {});
  let readyCalls = 0;
  context.window.AstrBotPluginView = { ready: () => { readyCalls++; return pendingReady; } };
  await context.start();
  assert.match(context.$("startup").textContent, /连接 AstrBot 超时/);
  assert.equal(context.startRunning, false);
  assert.equal(context.data, null);
  assert.equal(context.$("refresh").textContent, "重新加载页面");
  await context.refreshView();
  assert.equal(context.reloads, 1);
  assert.equal(readyCalls, 1);
  const reloaded = harness();
  reloaded.window.AstrBotPluginView = { ready: async () => ({ isDark: false }) };
  await reloaded.start();
  assert.equal(reloaded.$("update-error").hidden, true);
  assert.equal(reloaded.$("startup").hidden, true);
  assert.equal(reloaded.$("refresh").disabled, false);
});

test("account form uses platform hints and preserves opaque identifiers as strings", () => {
  const context = harness();
  vm.runInContext(declaration("updateAccountHint"), context);
  context.data = { platforms: [
    { id: "onebot", numeric_user_id: true, user_label: "用户 QQ 号" },
    { id: "official", numeric_user_id: false, user_label: "用户 OpenID", user_hint: "OpenID，不是 QQ 号" },
  ] };
  context.$("account-platform").value = "onebot";
  const input = context.$("account-user");
  input.value = "9007199254740993";
  context.updateAccountHint();
  assert.equal(input.value, "9007199254740993");
  assert.equal(input.inputMode, "numeric");
  assert.equal(input.pattern, "[0-9]+");
  context.$("account-platform").value = "official";
  input.value = "AbC_opaque-id";
  context.updateAccountHint();
  assert.equal(context.$("account-user-label").textContent, "用户 OpenID");
  assert.equal(input.placeholder, "OpenID，不是 QQ 号");
  assert.equal(input.pattern, undefined);
  assert.equal(input.value, "AbC_opaque-id");
});

test("historical scope disables modes and retry while preserving maintenance and deletion", () => {
  const context = harness();
  vm.runInContext(declaration("text"), context);
  vm.runInContext(declaration("renderScope"), context);
  context.data = { config: {}, scopes: [{
    scope_id: "old", historical: true, bot_id: "990001", mode: "paused", bank: "old-bank",
    summary: { counts: {}, maintenance: null },
  }] };
  context.scopeId = "old";
  context.renderScope();
  for (const id of ["mode-active", "mode-readonly", "mode-paused", "retry"]) assert.equal(context.$(id).disabled, true);
  for (const id of ["forget"]) assert.equal(context.$(id).disabled, false);
  assert.equal(context.$("mode-control").hidden, true);
  assert.match(context.$("historical-mode").textContent, /停用记忆/);
});

function installBootstrapChain(context) {
  for (const name of ["text", "pendingScopeHelp", "maintenanceDescription", "renderScope", "endpoint", "readMaintenanceStatus", "selectScope", "refreshBootstrap", "perform", "maintenanceAction"]) {
    vm.runInContext(declaration(name), context);
  }
}

function maintenanceBootstrap() {
  return {
    config: { configured: true, managed: false, connection_revision: "connection-1", allowed_accounts: [{platform_instance_id: "qq", user_id: "123"}] }, platforms: [], providers: {},
    status: { connected: true }, scopes: [{
      scope_id: "one", platform_instance_id: "qq", user_id: "123", mode: "active", bank: "bank", owner_id: "owner-1", generation: 1, revision: 2,
      summary: { counts: {}, maintenance: { connection_id: "service-1", started_at: 123 } },
    }],
  };
}

test("late bootstrap cannot restore an older bank or reenable historical controls", async () => {
  const context = harness();
  installBootstrapChain(context);
  let older;
  context.bridge = { apiGet: () => new Promise((resolve) => { older = resolve; }) };
  const first = context.refreshBootstrap({ automatic: true });
  const current = maintenanceBootstrap();
  current.scopes[0].bank = "new-bank";
  current.scopes[0].historical = true;
  current.scopes[0].availability = "historical";
  current.scopes[0].summary.maintenance = null;
  context.bridge.apiGet = async () => current;
  await context.refreshBootstrap({ automatic: true });
  older(maintenanceBootstrap());
  await first;
  assert.equal(context.$("scope-bank").value, "new-bank");
  assert.equal(context.$("mode-active").disabled, true);
});

test("starting an action invalidates an outstanding automatic bootstrap", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.data = maintenanceBootstrap();
  let older;
  context.bridge = { apiGet: () => new Promise((resolve) => { older = resolve; }) };
  const first = context.refreshBootstrap({ automatic: true });
  await context.perform(null, "memory", async () => {
    context.data.scopes[0].historical = true;
    context.data.scopes[0].bank = "after-action";
  });
  older(maintenanceBootstrap());
  await first;
  assert.equal(context.data.scopes[0].bank, "after-action");
  assert.equal(context.$("mode-active").disabled, true);
});

test("failed authorization drops the consumed token and allows preparing a fresh confirmation", async () => {
  const context = harness();
  for (const name of ["perform", "requestAuthorization", "prepareAuthorization", "confirmAuthorization"]) {
    vm.runInContext(declaration(name), context);
  }
  const calls = [];
  let prepared = 0;
  context.bridge = {
    apiPost: async (endpoint, payload) => {
      calls.push(payload);
      if (payload.action === "prepare") return { confirmation: `token-${++prepared}`, message: "确认机器人" };
      if (payload.confirmation === "token-1") throw new Error("授权确认已失效，请重新确认当前机器人身份。");
      return { message: "账号已授权" };
    },
  };
  await context.prepareAuthorization(null, "onebot", "2001");
  assert.equal(context.$("authorization-dialog").open, true);
  await context.confirmAuthorization();
  assert.equal(context.authorizationState, null);
  assert.equal(context.$("authorization-dialog").open, false);
  assert.match(context.$("feedback-message").textContent, /授权确认已失效/);
  assert.equal(context.$("feedback").hidden, false);
  await context.confirmAuthorization();
  assert.equal(calls.length, 2);
  await context.prepareAuthorization(null, "onebot", "2001");
  await context.confirmAuthorization();
  assert.equal(calls[3].confirmation, "token-2");
  assert.equal(context.authorizationState, null);
  assert.equal(context.$("authorization-dialog").open, false);
});

test("actual startup completes while optional maintenance waits, times out and ignores late success", async () => {
  const context = harness();
  installBootstrapChain(context);
  let resolveMaintenance;
  const sdk = {
    ready: async () => ({}),
    apiGet: (endpoint) => endpoint === "bootstrap" ? Promise.resolve(maintenanceBootstrap())
      : new Promise((resolve) => { resolveMaintenance = resolve; }),
  };
  context.window.AstrBotPluginView = sdk;
  await context.start();
  assert.equal(context.startRunning, false);
  assert.equal(context.viewReady, true);
  assert.equal(context.$("startup").hidden, true);
  assert.equal(context.$("refresh").disabled, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
  await new Promise((done) => setTimeout(done, 15));
  assert.match(context.maintenanceState.error, /获取暂停进度超时/);
  resolveMaintenance({ maintenance: { ready: true, phase: "ready" } });
  await new Promise((done) => setImmediate(done));
  assert.equal(context.maintenanceState.ready, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
  await context.refreshView();
  assert.equal(context.viewReady, true);
  assert.equal(context.maintenanceState.ready, false);
});

test("partial first-screen hydration still offers a full frame reload", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.hydrateConfig = () => { throw new Error("failed hydration"); };
  context.window.AstrBotPluginView = {
    ready: async () => ({}), apiGet: async () => maintenanceBootstrap(),
  };
  await context.start();
  assert.notEqual(context.data, null);
  assert.equal(context.viewReady, false);
  assert.equal(context.$("refresh").textContent, "重新加载页面");
  await context.refreshView();
  assert.equal(context.reloads, 1);
});

test("switching A to B to A discards old success after a newer read fails", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.setTimeout = (callback, milliseconds) => milliseconds > 20000 ? context.expiryTimer(callback, milliseconds) : setTimeout(callback, 1000);
  context.data = maintenanceBootstrap();
  context.data.scopes.push({ scope_id: "two", mode: "active", summary: { counts: {} } });
  context.scopeId = "one";
  let resolveOld;
  let reads = 0;
  context.bridge = { apiGet: () => ++reads === 1
    ? new Promise((resolve) => { resolveOld = resolve; })
    : Promise.reject(new Error("new read failed")) };
  const old = context.readMaintenanceStatus();
  context.selectScope("two");
  context.selectScope("one");
  await new Promise((done) => setImmediate(done));
  assert.equal(context.maintenanceState.ready, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
  resolveOld({ maintenance: { ready: true, phase: "ready" } });
  await old;
  assert.match(context.maintenanceState.error, /new read failed/);
  assert.equal(context.$("maintenance-resume").disabled, true);
});

test("starting a maintenance action invalidates an outstanding status read", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.setTimeout = (callback, milliseconds) => milliseconds > 20000 ? context.expiryTimer(callback, milliseconds) : setTimeout(callback, 1000);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  let resolveOld, rejectAction;
  context.bridge = {
    apiGet: () => new Promise((resolve) => { resolveOld = resolve; }),
    apiPost: () => new Promise((_, reject) => { rejectAction = reject; }),
  };
  const old = context.readMaintenanceStatus();
  const action = context.maintenanceAction(context.$("maintenance-begin"), "maintenance_begin");
  resolveOld({ maintenance: { ready: true, phase: "ready" } });
  await old;
  assert.equal(context.maintenanceState, null);
  assert.equal(context.$("maintenance-resume").disabled, true);
  rejectAction(new Error("action failed"));
  await action;
  assert.equal(context.$("memory-fields").disabled, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
});

test("manual maintenance status also times out and releases its controls", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  context.bridge = { apiGet: () => new Promise(() => {}) };
  await context.maintenanceAction(context.$("maintenance-retry"), "maintenance_status");
  assert.equal(context.$("memory-fields").disabled, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /获取暂停进度超时/);
  assert.equal(context.$("maintenance-retry").hidden, false);
});

test("same-scope polling retains confirmed ready until an actual failure, tasks or expiry", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.setTimeout = (callback, milliseconds) => milliseconds > 20000 ? context.expiryTimer(callback, milliseconds) : setTimeout(callback, 1000);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  context.bridge = { apiGet: async () => ({ maintenance: { ready: true, phase: "ready" } }) };
  await context.readMaintenanceStatus();
  let finish;
  context.bridge.apiGet = (path) => path === "bootstrap" ? Promise.resolve(maintenanceBootstrap())
    : new Promise((resolve) => { finish = resolve; });
  const poll = context.refreshBootstrap({ automatic: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(context.$("maintenance-resume").disabled, false);
  assert.equal(context.$("maintenance-detail").textContent, "可以修改记忆");
  finish({ maintenance: { ready: false, phase: "draining", remote_active: 2 } });
  await poll;
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /2 个任务/);
  context.bridge.apiGet = async () => ({ maintenance: { ready: true, phase: "ready" } });
  await context.readMaintenanceStatus();
  context.bridge.apiGet = async () => { throw new Error("connection refused"); };
  await context.readMaintenanceStatus();
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /connection refused/);
  context.bridge.apiGet = async () => ({ maintenance: { ready: true, phase: "ready" } });
  await context.readMaintenanceStatus();
  context.bridge.apiGet = async () => ({ maintenance: {
    ready: true, phase: "ready", bank: "bank", connection_id: "service-1", started_at: 999,
  } });
  await context.readMaintenanceStatus();
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /暂停范围已变化/);
  context.bridge.apiGet = async () => ({ maintenance: { ready: true, phase: "ready" } });
  await context.readMaintenanceStatus();
  context.maintenanceCheckedAt -= 30001;
  context.renderScope();
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /过期/);
  context.bridge.apiGet = async () => ({ maintenance: { ready: true, phase: "ready" } });
  await context.readMaintenanceStatus();
  context.bridge.apiGet = async () => ({});
  await assert.rejects(context.refreshBootstrap({ automatic: true }), /设置数据不完整/);
  assert.equal(context.$("maintenance-resume").disabled, true);
});

test("confirmed maintenance is bound to owner generation bank connection and pause boundary", async () => {
  const mutations = [
    (state) => { state.scopes[0].owner_id = "other-owner"; },
    (state) => { state.scopes[0].generation++; },
    (state) => { state.scopes[0].revision++; },
    (state) => { state.scopes[0].bank = "other-bank"; },
    (state) => { state.config.connection_revision = "other-key"; },
    (state) => { state.scopes[0].summary.maintenance.started_at++; },
    (state) => { state.scopes[0].summary.bank_missing = true; },
    (state) => { state.scopes[0].summary.maintenance = null; },
  ];
  for (const mutate of mutations) {
    const context = harness();
    installBootstrapChain(context);
    context.data = maintenanceBootstrap();
    context.scopeId = "one";
    context.bridge = { apiGet: async () => ({ maintenance: { ready: true, phase: "ready" } }) };
    await context.readMaintenanceStatus();
    assert.equal(context.$("maintenance-resume").disabled, false);
    mutate(context.data);
    context.renderScope();
    assert.equal(context.$("maintenance-resume").disabled, true);
    assert.equal(context.maintenanceState, null);
  }
  const context = harness();
  installBootstrapChain(context);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  context.bridge = { apiGet: async () => ({ maintenance: { ready: true, phase: "ready" } }) };
  await context.readMaintenanceStatus();
  context.data.config_revision = "unrelated-settings";
  context.data.scopes.push({ scope_id: "other", revision: 90, summary: {} });
  context.renderScope();
  assert.equal(context.$("maintenance-resume").disabled, false);
});

test("ready expires without polling under setup locks and old timers cannot expire new results", async () => {
  const context = harness();
  installBootstrapChain(context);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  context.bridge = { apiGet: async () => ({ maintenance: { ready: true, phase: "ready" } }) };
  await context.readMaintenanceStatus();
  const first = context.expiryTimers.at(-1);
  assert.ok(first.milliseconds > 29000 && first.milliseconds <= 30000);
  context.locks.add("setup");
  first.callback();
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("maintenance-detail").textContent, /过期/);
  await context.readMaintenanceStatus();
  const second = context.expiryTimers.at(-1);
  first.callback();
  assert.equal(context.$("maintenance-resume").disabled, false);
  context.data.scopes[0].summary.maintenance.started_at++;
  await context.readMaintenanceStatus();
  second.callback();
  assert.equal(context.$("maintenance-resume").disabled, false);
  context.selectScope("");
  context.expiryTimers.at(-1).callback();
  assert.equal(context.maintenanceState, null);
});

test("discard visibility uses backend capability and ordinary pause never discards", () => {
  const context = harness();
  installBootstrapChain(context);
  context.data = maintenanceBootstrap();
  context.scopeId = "one";
  const summary = context.data.scopes[0].summary;
  summary.counts = { reconcile_required: 1 };
  summary.can_discard = true;
  context.renderScope();
  assert.equal(context.$("discard-records").hidden, false);
  summary.can_discard = false;
  summary.counts = { unknown: 1 };
  context.renderScope();
  assert.equal(context.$("discard-records").hidden, true);
  const calls = [];
  context.maintenanceAction = (button, action, extra) => { calls.push(extra.discard_pending); };
  vm.runInContext(source.split("\n").find((line) => line.startsWith('$("maintenance-discard").addEventListener')), context);
  context.$("discard-pending").checked = true;
  context.$("maintenance-discard").listeners.click();
  assert.deepEqual(calls, [true]);
  assert.equal(source.includes('$("maintenance-begin").addEventListener'), false);
});

test("a late bootstrap response cannot hydrate the page after timeout", async () => {
  const context = harness();
  let resolve;
  context.bridge = { apiGet: () => new Promise((done) => { resolve = done; }) };
  vm.runInContext(declaration("refreshBootstrap"), context);
  await assert.rejects(context.refreshBootstrap({ hydrate: true }), /获取插件状态超时/);
  resolve({ config: {}, scopes: [] });
  await new Promise((done) => setImmediate(done));
  assert.equal(context.data, null);
});

test("successful work clears its timeout", async () => {
  const context = harness();
  let timers = 0;
  context.setTimeout = () => { timers++; return "timer"; };
  context.clearTimeout = () => { timers--; };
  assert.equal(await context.withTimeout(Promise.resolve("done"), 100, "timeout"), "done");
  assert.equal(timers, 0);
});

test("connected service does not hide the user's recall failure", () => {
  const context = harness();
  vm.runInContext(declaration("text"), context);
  vm.runInContext(declaration("renderScope"), context);
  context.data = {
    config: { managed: false }, status: { connected: true, label: "recall_timeout" },
    scopes: [{ scope_id: "one", mode: "active", summary: { counts: {}, recall: {
      outcome: "error", code: "recall_timeout", elapsed_ms: 10000,
      last_at: 1700000000, consecutive_failures: 3, error_message: "可增加召回等待时间",
    } } }],
  };
  context.scopeId = "one";
  context.renderStatus();
  context.renderScope();
  assert.equal(context.$("status-label").textContent, "已连接");
  const text = context.$("scope-recall-status").textContent;
  assert.match(text, /超时/);
  assert.match(text, /增加召回等待时间/);
});

test("connection errors retain specific guidance and existing services hide models", () => {
  const context = harness();
  context.data = { config: { managed: false }, status: { connected: false, message: "连接密钥无效，请检查 API Key" }, scopes: [] };
  context.renderStatus();
  assert.equal(context.$("status-description").textContent, context.data.status.message);
  context.document.querySelector = () => ({ value: "existing" });
  context.updateConnectionMode();
  assert.equal(context.$("models-settings").hidden, true);
});

function backgroundHarness() {
  const context = harness();
  for (const name of ["backgroundSnapshot", "backgroundPages", "renderBackground", "endpoint", "loadBackground", "saveBackground", "perform"]) {
    vm.runInContext(declaration(name), context);
  }
  context.renderScope = context.renderBackground;
  context.refreshBootstrap = async () => {};
  context.data = {
    config: {allowed_accounts: [{platform_instance_id: "qq", user_id: "123"}]}, config_revision: "config-1", status: {}, scopes: [{
      scope_id: "one", platform_instance_id: "qq", user_id: "123", bank: "bank-1", summary: {}, background: {
        revision: 1, scope_revision: 2, page_id: "kp-1", enabled: true,
      },
    }],
  };
  context.scopeId = "one";
  context.renderBackground();
  return context;
}

test("background polling retains saved selection and clears references after Bank rotation", () => {
  const context = backgroundHarness();
  context.backgroundDraft.page_id = "unsaved-page";
  context.backgroundDraft.enabled = false;
  context.renderBackground();
  assert.equal(context.$("background-page").value, "kp-1");
  context.data.scopes[0].bank = "new-bank";
  context.data.scopes[0].background = { revision: 2, scope_revision: 3, enabled: false, page_id: "" };
  context.renderBackground();
  assert.equal(context.$("background-page").value, "");
});

test("per-page usage and failures remain visible after index loading and polling", async () => {
  const context = backgroundHarness();
  const saved = context.data.scopes[0].background;
  const content = () => context.$("background-selected").children.flatMap(row => row.children).map(row => row.textContent).join(" ");
  context.bridge = { apiGet: async () => ({ pages: [{ page_id: "kp-1", path: "Page" }],
    selection: { revision: 1 }, scope_revision: 2, config_revision: "config-1" }) };
  for (const message of ["已使用", "知识页太长", "读取超时", "正文为空", "页面不存在"]) {
    saved.status = { pages: [{ page_id: "kp-1", used: message === "已使用", message }] };
    context.renderBackground();
    assert.ok(content().includes(message));
    await context.loadBackground();
    context.renderBackground();
    assert.ok(content().includes(message));
  }
  context.scopeId = "two";
  context.data.scopes.push({ scope_id: "two", bank: "bank-2", summary: {}, background: { revision: 0, enabled: false } });
  context.renderBackground();
  assert.equal(content(), "");
  context.scopeId = "one";
  saved.invalidated = true;
  saved.enabled = false;
  saved.page_id = "";
  saved.message = "知识页选择已失效，请重新选择。";
  context.renderBackground();
  assert.equal(context.$("background-page").value, "");
  assert.equal(context.$("background-status").textContent, saved.message);
});

test("historical and maintenance scopes never offer page reads; maintenance permits closing", () => {
  const context = backgroundHarness();
  context.data.scopes[0].summary.maintenance = {};
  context.renderBackground();
  assert.equal(context.$("background-page").disabled, false);
  assert.equal(context.$("background-page").options[1].disabled, true);
  context.data.scopes[0].historical = true;
  context.renderBackground();
  assert.equal(context.$("background-page").disabled, true);
});

test("page rows show length and actual stale state without repeating routine status", async () => {
  const context = backgroundHarness();
  context.data.scopes[0].background.status = { pages: [{ page_id: "kp-1", used: true }] };
  const content = () => context.$("background-selected").children.flatMap(row => row.children).map(row => row.textContent).join(" ");
  for (const stale of [true, false, null]) {
    context.bridge = { apiGet: async () => ({
      pages: [{ page_id: "kp-1", path: "Page", index_stale: stale }],
      selection: { revision: 1 }, scope_revision: 2, config_revision: "config-1",
      checks: [{ page_id: "kp-1", tokens: 1800, chars: 4000, exceeded: ["知识页长度"], code: "background_too_large" }],
    }) };
    await context.loadBackground();
    assert.match(content(), /1800 token/);
    assert.match(content(), /已使用/);
    assert.equal(content().includes("有新记忆待刷新"), stale === true);
    assert.equal(context.$("background-status").hidden, true);
  }
  context.data.scopes[0].background.enabled = false;
  context.renderBackground();
  assert.doesNotMatch(content(), /1800 token|已使用|有新记忆待刷新/);
});

test("late background index cannot overwrite another user's draft", async () => {
  const context = backgroundHarness();
  let release;
  context.bridge = { apiGet: () => new Promise((resolve) => { release = resolve; }) };
  const loading = context.loadBackground();
  context.data.scopes.push({ scope_id: "two", bank: "bank-2", summary: {}, background: { revision: 0, scope_revision: 4, enabled: false, page_id: "" } });
  context.scopeId = "two";
  context.renderBackground();
  release({ pages: [{ page_id: "private-page", path: "Other user's page" }], selection: { revision: 1 }, scope_revision: 2 });
  await loading;
  assert.equal(context.backgroundDraft.id, "two");
  assert.equal(context.backgroundDraft.pages.length, 0);
  assert.equal(context.$("background-page").value, "");
});

test("background index timeout releases controls and never interprets names as HTML", async () => {
  const context = backgroundHarness();
  context.bridge = { apiGet: () => new Promise(() => {}) };
  await context.loadBackground();
  assert.match(context.$("background-feedback").textContent, /超时/);
  assert.equal(context.backgroundDraft.loading, false);
  context.bridge.apiGet = async () => ({
    pages: [{ page_id: "kp-1", path: "<img src=x onerror=alert(1)>" }],
    selection: { revision: 1 }, scope_revision: 2, config_revision: "config-1",
  });
  await context.loadBackground();
  assert.equal(context.$("background-page").children[1].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(context.$("background-page").children[1].innerHTML, undefined);
});

test("background save carries original revisions and closing does not delete a page", async () => {
  const context = backgroundHarness();
  const calls = [];
  context.bridge = { apiPost: async (endpoint, payload) => { calls.push({ endpoint, payload }); return { message: "saved" }; } };
  context.$("background-page").value = "kp-2";
  context.data.config_revision = "new-config";
  await context.saveBackground();
  assert.equal(calls[0].payload.config_revision, "config-1");
  assert.equal(JSON.stringify(calls[0].payload.page_ids), '["kp-1","kp-2"]');
  context.$("background-page").value = "";
  await context.saveBackground();
  assert.equal(calls[1].payload.enabled, false);
  assert.equal(JSON.stringify(calls[1].payload.page_ids), '["kp-1"]');
  assert.equal(calls[1].endpoint, "scopes/one/background");
  assert.equal("bank" in calls[1].payload, false);
});

test("settings refresh invalidates old drafts and allows closing during maintenance without a tree read", async () => {
  const context = backgroundHarness();
  vm.runInContext(declaration("refreshBootstrap"), context);
  context.hydrateConfig = () => {};
  const oldDraft = context.backgroundDraft;
  oldDraft.page_id = "unsaved-page";
  oldDraft.enabled = false;
  const next = structuredClone(context.data);
  next.config_revision = "config-2";
  next.scopes[0].summary.maintenance = {};
  const calls = [];
  context.bridge = {
    apiGet: async (route) => {
      calls.push(route);
      assert.equal(route, "bootstrap");
      return next;
    },
    apiPost: async (route, payload) => {
      assert.equal(route, "scopes/one/background");
      assert.equal(payload.config_revision, "config-2");
      assert.equal(JSON.stringify(payload.page_ids), '["kp-1"]');
      assert.equal(payload.enabled, false);
      next.scopes[0].background = { ...next.scopes[0].background, enabled: false, revision: 2 };
      return { message: "saved" };
    },
  };
  await context.refreshBootstrap({ hydrate: true, pollMaintenance: false });
  assert.notEqual(context.backgroundDraft, oldDraft);
  assert.equal(context.backgroundDraft.page_id, "kp-1");
  assert.equal(context.backgroundDraft.config_revision, "config-2");
  assert.equal(context.$("background-page").disabled, false);
  await context.refreshBootstrap({ pollMaintenance: false });
  assert.equal(context.backgroundDraft.config_revision, "config-2");
  context.$("background-page").value = "";
  await context.saveBackground();
  assert.equal(context.data.scopes[0].background.enabled, false);
  assert.ok(calls.length >= 3);
});

test("configuration change drops a pending old page list and unsaved choice rather than rebasing it", async () => {
  const context = backgroundHarness();
  let release;
  context.backgroundDraft.page_id = "unsaved-page";
  context.bridge = { apiGet: () => new Promise((resolve) => { release = resolve; }) };
  const loading = context.loadBackground();
  context.data.config_revision = "config-2";
  context.renderBackground();
  release({ pages: [{ page_id: "old-page", path: "old list" }], selection: { revision: 1 }, scope_revision: 2, config_revision: "config-1" });
  await loading;
  assert.equal(context.backgroundDraft.config_revision, "config-2");
  assert.equal(context.backgroundDraft.page_id, "kp-1");
  assert.equal(context.backgroundDraft.pages.length, 0);
  assert.equal(context.$("background-page").value, "kp-1");
});

test("multiple pages render usage per page and persist ordering and removal", async () => {
  const context = backgroundHarness();
  const saved = context.data.scopes[0].background;
  saved.pages = [{ page_id: "kp-1" }, { page_id: "kp-2" }];
  saved.status = { message: "上次聊天使用 1/2 篇知识页。", pages: [
    { page_id: "kp-1", message: "上次聊天已使用。" },
    { page_id: "kp-2", message: "知识页太长。" },
  ] };
  context.backgroundDraft.pages = [
    { page_id: "kp-1", path: "First", index_stale: false },
    { page_id: "kp-2", path: "<b>Second</b>", index_stale: true },
  ];
  context.renderBackground();
  const rows = context.$("background-selected").children;
  assert.equal(rows[1].children[0].textContent, "2. <b>Second</b>");
  assert.equal(rows[1].children[0].innerHTML, undefined);
  assert.equal(rows[1].children[1].textContent, "知识页太长。");
  assert.equal(rows[0].children.at(-1).children[0].disabled, true);
  assert.equal(rows[1].children.at(-1).children[1].disabled, true);
  const writes = [];
  context.bridge = { apiPost: async (route, payload) => { writes.push(payload); return {}; } };
  rows[1].children.at(-1).children[0].listeners.click();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(JSON.stringify(writes[0].page_ids), '["kp-2","kp-1"]');
  rows[0].children.at(-1).children[2].listeners.click();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(JSON.stringify(writes[1].page_ids), '["kp-2"]');
  assert.equal(writes[1].enabled, true);
});

test("reading mode preserves original revisions and drops feedback after user changes", async () => {
  const context = harness();
  for (const name of ["renderReading", "saveReading", "perform", "endpoint"]) vm.runInContext(declaration(name), context);
  context.$("reading-mode").replaceChildren(context.option("automatic", "auto"), context.option("tools", "tools"));
  context.data = { config_revision: "config-1", status: {}, scopes: [{
    scope_id: "one", bank: "bank-one", mode: "readonly", summary: {},
    reading: { revision: 2, scope_revision: 3, mode: "automatic", observed_available: false },
  }] };
  context.scopeId = "one";
  context.renderReading();
  assert.equal(context.$("reading-mode").options[1].disabled, true);
  context.data.scopes[0].reading.observed_available = true;
  context.renderReading();
  assert.equal(context.$("reading-mode").options[1].disabled, false);
  context.$("reading-mode").value = "tools";
  let release;
  context.bridge = { apiPost: (route, payload) => {
    assert.equal(route, "scopes/one/reading");
    assert.equal(payload.revision, 2);
    assert.equal(payload.scope_revision, 3);
    assert.equal(payload.config_revision, "config-1");
    return new Promise((resolve) => { release = resolve; });
  } };
  const saving = context.saveReading();
  context.scopeId = "two";
  context.data.scopes.push({ scope_id: "two", bank: "bank-two", summary: {},
    reading: { revision: 0, scope_revision: 1, mode: "automatic" } });
  context.renderReading();
  release({ message: "saved-other-user" });
  await saving;
  assert.equal(context.$("reading-mode").value, "automatic");
  assert.doesNotMatch(context.$("reading-feedback").textContent, /saved-other-user/);
});

test("Reflect keeps dirty values across polling, detects conflicts and ignores a former user's save", async () => {
  const context = harness();
  for (const name of ["renderReflect", "saveReflect", "perform", "endpoint"]) vm.runInContext(declaration(name), context);
  context.data = { config_revision: "config-1", status: {}, scopes: [{
    scope_id: "one", bank: "bank-one", mode: "readonly", summary: {},
    reflect: { revision: 2, scope_revision: 3, enabled: false, timeout: 12, observed_available: true },
  }] };
  context.scopeId = "one";
  context.renderReflect();
  context.$("reflect-enabled").checked = true;
  context.$("reflect-timeout").value = "20";
  context.reflectDraft.dirty = true;
  context.renderReflect();
  assert.equal(context.$("reflect-timeout").value, "20");
  context.data.scopes[0].reflect.revision = 3;
  context.renderReflect();
  assert.equal(context.$("reflect-conflict").hidden, false);
  assert.equal(context.$("reflect-save").disabled, true);
  assert.equal(context.$("reflect-timeout").value, "20");
  context.renderReflect({ discard: true });
  assert.equal(context.$("reflect-timeout").value, "12");
  context.$("reflect-enabled").checked = true;
  context.reflectDraft.dirty = true;
  let release;
  context.bridge = { apiPost: (route, payload) => {
    assert.equal(route, "scopes/one/reflect");
    assert.equal(payload.enabled, true);
    assert.equal(payload.timeout, 12);
    assert.equal(payload.revision, 3);
    return new Promise((resolve) => { release = resolve; });
  } };
  const saving = context.saveReflect();
  context.scopeId = "two";
  context.data.scopes.push({ scope_id: "two", bank: "bank-two", summary: {},
    reflect: { revision: 0, scope_revision: 1, enabled: false, timeout: 12 } });
  context.renderReflect();
  release({ message: "saved-former-user" });
  await saving;
  assert.equal(context.$("reflect-enabled").checked, false);
  assert.doesNotMatch(context.$("reflect-feedback").textContent, /saved-former-user/);
});

test("an enabled Reflect shows detected model unavailability and still allows disabling", () => {
  const context = harness();
  vm.runInContext(declaration("renderReflect"), context);
  const reflect = { revision: 1, scope_revision: 1, enabled: true, timeout: 12,
    observed_available: true, status: { state: "success" } };
  context.data = { config_revision: "one", status: {}, scopes: [
    { scope_id: "one", bank: "bank", mode: "active", summary: {}, reflect },
  ] };
  context.scopeId = "one";
  context.renderReflect();
  assert.equal(context.$("reflect-status").hidden, true);
  reflect.observed_available = false;
  reflect.status = { state: "unavailable" };
  context.renderReflect();
  assert.equal(context.$("reflect-enabled").checked, true);
  assert.equal(context.$("reflect-enabled").disabled, false);
  assert.equal(context.$("reflect-status").hidden, false);
  assert.match(context.$("reflect-status").textContent, /此会话暂不支持 Reflect/);
  reflect.enabled = false;
  context.renderReflect();
  assert.equal(context.$("reflect-status").hidden, true);
});

test("removed accounts stay out of user choices even while their Banks are retained", () => {
  const context = harness();
  context.data = maintenanceBootstrap();
  context.data.config.allowed_accounts = [{ platform_instance_id: "qq", user_id: "456" }];
  context.data.scopes.push({ scope_id: "other", platform_instance_id: "qq", user_id: "456", mode: "active", summary: {} });
  context.selectedAccount = JSON.stringify(["qq", "123"]);
  context.scopeId = "one";
  context.updateUserChoices();
  assert.equal(context.$("scope-picker").options.length, 1);
  assert.match(context.$("scope-picker").options[0].textContent, /456/);
  assert.equal(context.scopeId, "other");
  assert.equal(context.data.scopes[0].bank, "bank");
});

test("disconnected saved Banks remain accessible without being labeled as old robots", () => {
  const context = harness();
  context.data = maintenanceBootstrap();
  context.data.scopes[0] = { ...context.data.scopes[0], historical: true, availability: "disconnected", bot_id: "bot" };
  context.data.scopes.push({ scope_id: "pending", platform_instance_id: "qq", user_id: "123",
    pending: true, pending_reason: "platform" });
  context.selectedAccount = JSON.stringify(["qq", "123"]);
  context.updateUserChoices();
  assert.equal(context.$("bank-picker").options.length, 2);
  assert.equal(context.$("bank-picker").options[0].textContent, "等待连接");
  assert.equal(context.$("bank-picker").options[1].value, "one");
  assert.equal(context.$("bank-picker").options[1].textContent, "机器人 bot（未连接）");
});

test("failed mode changes keep the server's mode selected", async () => {
  const context = harness();
  installBootstrapChain(context);
  vm.runInContext(declaration("setMode"), context);
  context.data = maintenanceBootstrap();
  context.data.scopes[0].mode = "readonly";
  context.data.scopes[0].summary.maintenance = null;
  context.scopeId = "one";
  context.bridge = { apiPost: async () => { throw new Error("save failed"); } };
  await context.setMode(context.$("mode-active"), "active");
  assert.equal(context.$("mode-active").attributes["aria-pressed"], "false");
  assert.equal(context.$("mode-readonly").attributes["aria-pressed"], "true");
  assert.match(context.$("feedback-message").textContent, /save failed/);
});

test("ignored update notices never hide an unfinished upgrade", () => {
  const context = harness();
  vm.runInContext(declaration("renderServiceVersion"), context);
  context.data = { config: { configured: true, managed: true, connection_revision: "first" }, backend: {
    version: "1.1.0", target: "1.2.0", managed: true, upgrade_available: true, ignored: true,
  } };
  context.renderServiceVersion();
  assert.equal(context.$("upgrade-notice").hidden, true);
  assert.equal(context.$("go-upgrade").hidden, false);
  context.data.backend.upgrade_paused = true;
  context.renderServiceVersion();
  assert.equal(context.$("upgrade-notice").hidden, false);
  assert.equal(context.$("ignore-upgrade").hidden, true);
  context.data.backend.upgrade_paused = false;
  context.data.config.connection_revision = "second";
  context.data.backend.ignored = false;
  context.renderServiceVersion();
  assert.equal(context.$("upgrade-notice").hidden, false);
});

test("confirmed additions survive failed bootstrap reads and subsequent additions", async () => {
  const context = harness();
  for (const name of ["saveAccounts", "submitAccount", "perform"]) vm.runInContext(declaration(name), context);
  context.data = maintenanceBootstrap();
  context.settingsRevision = "revision-1";
  let server = context.data.config.allowed_accounts;
  let revision = 1;
  context.bridge = { apiPost: async (endpoint, payload) => {
    assert.equal(endpoint, "settings/save");
    assert.equal(payload.config_revision, `revision-${revision}`);
    server = payload.allowed_accounts.map((account) => ({ ...account }));
    return { saved: true, config_revision: `revision-${++revision}` };
  } };
  context.refreshBootstrap = async () => { throw new Error("bootstrap unavailable"); };
  context.requestAuthorization = async () => {};
  context.$("account-platform").value = "qq";
  for (const user of ["456", "789"]) {
    context.$("account-user").value = user;
    await context.submitAccount();
  }
  assert.deepEqual(Array.from(server, (account) => account.user_id), ["123", "456", "789"]);
  assert.equal(context.data.config_revision, "revision-3");
});

test("confirmed removals survive failed bootstrap reads without reviving users", async () => {
  const context = harness();
  for (const name of ["saveAccounts", "requestAccountRemoval", "confirmAccountRemoval", "perform"]) vm.runInContext(declaration(name), context);
  context.data = maintenanceBootstrap();
  context.data.config.allowed_accounts.push({ platform_instance_id: "qq", user_id: "456" });
  context.data.scopes.push({ scope_id: "two", platform_instance_id: "qq", user_id: "456", summary: {} });
  context.selectedAccount = JSON.stringify(["qq", "123"]);
  context.settingsRevision = "revision-1";
  let server;
  let revision = 1;
  context.bridge = { apiPost: async (endpoint, payload) => {
    assert.equal(payload.config_revision, `revision-${revision}`);
    server = payload.allowed_accounts.map((account) => ({ ...account }));
    return { saved: true, config_revision: `revision-${++revision}` };
  } };
  context.refreshBootstrap = async () => { throw new Error("bootstrap unavailable"); };
  for (let index = 0; index < 2; index++) {
    context.requestAccountRemoval();
    await context.confirmAccountRemoval();
  }
  assert.equal(server.length, 0);
  assert.equal(context.data.config.allowed_accounts.length, 0);
  assert.equal(context.$("no-scopes").hidden, false);
});

test("removed Banks with only a missing flag retain recovery and clearing controls", async () => {
  const context = harness();
  installBootstrapChain(context);
  const snapshot = maintenanceBootstrap();
  snapshot.config.allowed_accounts = [];
  snapshot.scopes[0].summary = { bank_missing: true, maintenance: null, counts: {} };
  context.bridge = { apiGet: async () => snapshot };
  await context.refreshBootstrap({ automatic: true });
  assert.equal(context.scopeId, "");
  assert.equal(context.$("paused-banks-notice").hidden, false);
  context.maintenanceScopeId = "one";
  context.renderMaintenance();
  assert.equal(context.$("maintenance-bank-check").hidden, false);
  assert.equal(context.$("maintenance-bank-check").disabled, false);
  assert.equal(context.$("maintenance-forget").hidden, false);
  assert.match(context.$("maintenance-detail").textContent, /找不到/);
  assert.equal(context.$("maintenance-resume").disabled, true);
});
