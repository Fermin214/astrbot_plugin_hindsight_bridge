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
    backgroundReadRevision: 0, backgroundDraft: null,
    renderBackground() {},
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

function backgroundHarness() {
  const context = harness();
  for (const name of ["backgroundSnapshot", "renderBackground", "endpoint", "loadBackground", "saveBackground", "perform"]) {
    vm.runInContext(declaration(name), context);
  }
  context.renderScope = context.renderBackground;
  context.refreshBootstrap = async () => {};
  context.data = {
    config: {}, config_revision: "config-1", status: {}, scopes: [{
      scope_id: "one", bank: "bank-1", summary: {}, background: {
        revision: 1, scope_revision: 2, page_id: "kp-1", enabled: true,
      },
    }],
  };
  context.scopeId = "one";
  context.renderBackground();
  return context;
}

test("background polling preserves an unsaved choice but clears an old bank draft", () => {
  const context = backgroundHarness();
  context.backgroundDraft.page_id = "unsaved-page";
  context.backgroundDraft.enabled = false;
  context.renderBackground();
  assert.equal(context.$("background-page").value, "unsaved-page");
  assert.equal(context.$("background-enabled").checked, false);
  context.data.scopes[0].bank = "new-bank";
  context.data.scopes[0].background = { revision: 2, scope_revision: 3, enabled: false, page_id: "" };
  context.renderBackground();
  assert.equal(context.$("background-page").value, "");
  assert.equal(context.$("background-enabled").checked, false);
});

test("historical and maintenance scopes never offer page reads; maintenance permits closing", () => {
  const context = backgroundHarness();
  context.data.scopes[0].summary.maintenance = {};
  context.renderBackground();
  for (const id of ["background-page", "background-load", "background-save"]) assert.equal(context.$(id).disabled, true);
  assert.equal(context.$("background-close").disabled, false);
  context.data.scopes[0].historical = true;
  context.renderBackground();
  assert.equal(context.$("background-close").disabled, true);
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
  assert.equal(context.$("background-load").disabled, false);
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
  context.backgroundDraft.page_id = "kp-2";
  context.data.config_revision = "new-config";
  await context.saveBackground();
  assert.equal(calls[0].payload.config_revision, "config-1");
  assert.equal(calls[0].payload.page_id, "kp-2");
  await context.saveBackground(true);
  assert.equal(calls[1].payload.enabled, false);
  assert.equal(calls[1].payload.page_id, "kp-1");
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
      assert.equal(payload.page_id, "kp-1");
      assert.equal(payload.enabled, false);
      next.scopes[0].background = { ...next.scopes[0].background, enabled: false, revision: 2 };
      return { message: "saved" };
    },
  };
  await context.refreshBootstrap({ hydrate: true, pollMaintenance: false });
  assert.notEqual(context.backgroundDraft, oldDraft);
  assert.equal(context.backgroundDraft.page_id, "kp-1");
  assert.equal(context.backgroundDraft.config_revision, "config-2");
  assert.equal(context.$("background-load").disabled, true);
  assert.equal(context.$("background-close").disabled, false);
  await context.refreshBootstrap({ pollMaintenance: false });
  assert.equal(context.backgroundDraft.config_revision, "config-2");
  await context.saveBackground(true);
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
