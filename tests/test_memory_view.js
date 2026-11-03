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
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { this.listeners[name] = handler; }, setAttribute() {},
      removeAttribute(name) { delete this[name === "maxlength" ? "maxLength" : name]; },
      showModal() { this.open = true; }, close() { this.open = false; },
    };
  }
  const context = vm.createContext({
    console, Promise, Date, Error, setTimeout: (callback) => setTimeout(callback, 5), clearTimeout,
    $: (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    el: (tag, kind, content) => Object.assign(node(), { textContent: content, className: kind || "" }),
    window: { AstrBotPluginView: null, location: { reload() { context.reloads++; } } },
    document: { documentElement: { dataset: {} }, querySelector: () => ({ value: "existing" }) },
    data: null, bridge: null, startRunning: false, viewReady: false, offContext: undefined, reloads: 0,
    locks: new Set(), scopeId: "", modeDirty: false, maintenanceState: null,
    authorizationState: null, settingsRevision: "",
    maintenanceReadRevision: 0, bootstrapReadRevision: 0, statusSignature: "",
    modelWatch: null, feedbackTimer: null, modeHelp: { active: "读取并保存" },
    hydrateConfig() {}, providerSelect() {}, refreshAccountPlatforms() {}, updateProviderHelp() {},
    readMaintenanceStatus: async () => {},
    option: (value, label, disabled = false) => Object.assign(node(), { value, textContent: label, disabled }),
    activateTab() {}, scheduleRefresh() {}, modeLabels: { active: "正常记忆" },
    scopeName: () => "user", countDescription: () => "queue", maintenanceDescription: () => "maintenance",
    refreshBootstrap: async () => {
      context.data = { config: { configured: true } };
      context.$("startup").hidden = true;
    },
  });
  for (const name of ["withTimeout", "errorMessage", "start", "refreshView", "notify", "recallDescription", "renderStatus", "updateConnectionMode"]) {
    vm.runInContext(declaration(name), context);
  }
  return context;
}

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

test("account fields use platform hints and keep opaque identifiers as strings", () => {
  const context = harness();
  vm.runInContext(declaration("addAccount"), context);
  context.data = { platforms: [
    { id: "onebot", supported: true, numeric_user_id: true, user_label: "用户 QQ 号" },
    { id: "official", supported: false, numeric_user_id: false, user_label: "用户 OpenID", user_hint: "OpenID，不是 QQ 号" },
  ] };
  const select = context.addAccount({ platform_instance_id: "onebot", user_id: "9007199254740993" });
  const row = context.$("accounts").children[0];
  const caption = row.children[1].children[0];
  const input = row.children[1].children[1];
  assert.equal(input.value, "9007199254740993");
  assert.equal(input.inputMode, "numeric");
  assert.equal(input.pattern, "[0-9]+");
  assert.equal(select.children[2].disabled, true);
  input.value = "AbC_opaque-id";
  select.value = "official";
  select.listeners.change();
  assert.equal(caption.textContent, "用户 OpenID");
  assert.equal(input.placeholder, "OpenID，不是 QQ 号");
  assert.equal(input.inputMode, "text");
  assert.equal(input.pattern, undefined);
  assert.equal(input.value, "AbC_opaque-id");
});

test("historical scope disables modes and retry while preserving maintenance and deletion", () => {
  const context = harness();
  vm.runInContext(declaration("text"), context);
  vm.runInContext(declaration("renderScope"), context);
  context.data = { config: {}, scopes: [{
    scope_id: "old", historical: true, mode: "paused", bank: "old-bank",
    summary: { counts: {}, maintenance: null },
  }] };
  context.scopeId = "old";
  context.renderScope();
  for (const id of ["scope-mode", "save-mode", "retry"]) assert.equal(context.$(id).disabled, true);
  for (const id of ["maintenance-begin", "forget"]) assert.equal(context.$(id).disabled, false);
  assert.match(context.$("mode-help").textContent, /历史范围/);
});

function installBootstrapChain(context) {
  for (const name of ["text", "pendingScopeHelp", "maintenanceDescription", "renderScope", "endpoint", "readMaintenanceStatus", "selectScope", "refreshBootstrap", "perform", "maintenanceAction"]) {
    vm.runInContext(declaration(name), context);
  }
}

function maintenanceBootstrap() {
  return {
    config: { configured: true, managed: false }, platforms: [], providers: {},
    status: { connected: true }, scopes: [{
      scope_id: "one", mode: "active", bank: "bank", summary: { counts: {}, maintenance: {} },
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
  current.scopes[0].summary.maintenance = null;
  context.bridge.apiGet = async () => current;
  await context.refreshBootstrap({ automatic: true });
  older(maintenanceBootstrap());
  await first;
  assert.equal(context.$("scope-bank").value, "new-bank");
  assert.equal(context.$("save-mode").disabled, true);
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
  assert.equal(context.$("save-mode").disabled, true);
});

test("failed authorization drops the consumed token and allows preparing a fresh confirmation", async () => {
  const context = harness();
  for (const name of ["perform", "prepareAuthorization", "confirmAuthorization"]) {
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
  assert.match(context.maintenanceState.error, /获取维护状态超时/);
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
  context.setTimeout = (callback) => setTimeout(callback, 1000);
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
  context.setTimeout = (callback) => setTimeout(callback, 1000);
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
  await context.maintenanceAction(context.$("maintenance-status"), "maintenance_status");
  assert.equal(context.$("memory-fields").disabled, false);
  assert.equal(context.$("maintenance-resume").disabled, true);
  assert.match(context.$("feedback-message").textContent, /获取维护状态超时/);
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
  context.data = {
    config: { managed: false }, status: { connected: true, label: "recall_timeout" },
    scopes: [{ mode: "active", summary: { counts: {}, recall: {
      outcome: "error", code: "recall_timeout", elapsed_ms: 10000,
      last_at: 1700000000, consecutive_failures: 3, error_message: "可增加召回等待时间",
    } } }],
  };
  context.renderStatus();
  assert.equal(context.$("status-label").textContent, "服务已连接");
  const row = context.$("scope-statuses").children[0];
  const text = row.children.map((child) => child.textContent).join(" ");
  assert.match(text, /最近记忆检索：超时/);
  assert.match(text, /连续失败 3 次/);
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
