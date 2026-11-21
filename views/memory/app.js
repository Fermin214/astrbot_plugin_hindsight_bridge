import { createState } from "./state.js";
import { createDom } from "./dom.js";
import { createSetup } from "./setup.js";
import { createMaintenance } from "./maintenance.js";
import { createMemories } from "./memories.js";

export function createApp(view) {
  function renderStatus() {
    const status = view.data.status || {};
    const connected = status.connected;
    view.$("status-label").textContent = status.recovering ? "恢复中" : connected === true ? "已连接" : status.label || (connected === false ? "服务未连接" : "连接状态未知");
    view.$("status-dot").className = `status-dot${!status.recovering && connected === true ? " good" : connected === false ? " bad" : ""}`;
    let description = status.recovering ? "正在核对记忆库与保存任务。" : connected !== true ? status.message || "请检查服务地址、密钥和服务运行状态。" : "";
    if (view.data.config.managed && view.data.managed_service?.installed && !status.recovering && !view.data.managed_service.ready) description = "服务已安装，请配置记忆处理模型和向量模型。";
    view.$("status-description").textContent = description;
    view.$("status-description").hidden = !description;
    const setup = view.$("panel-setup");
    const first = view.$(connected === true || view.data.config.configured ? "parameters-form" : "setup-fields");
    if (setup.firstElementChild !== first) {
      if (setup.moveBefore) setup.moveBefore(first, setup.firstElementChild);
      else setup.prepend(first);
    }
    const attention = !!status.recovering || !view.data.config.configured || connected !== true;
    view.$("service-alert").hidden = !attention;
    view.$("service-alert-message").textContent = attention ? description || view.$("status-label").textContent : "";
  }

  async function refreshBootstrap({ hydrate = false, automatic = false, pollMaintenance = true } = {}) {
    const revision = ++view.bootstrapReadRevision;
    let next;
    try {
      next = await view.withTimeout(view.bridge.apiGet("bootstrap"), 15000, "获取插件状态超时，请点击重新获取。");
      if (!next || !next.config || !Array.isArray(next.scopes)) throw new Error("设置数据不完整，请刷新页面重试。");
    } catch (error) {
      if (revision !== view.bootstrapReadRevision) return;
      if (view.maintenanceIdentity) { view.maintenanceReadRevision++; view.maintenanceState = { ready: false, error: view.errorMessage(error) }; view.renderMaintenance(); }
      throw error;
    }
    if (revision !== view.bootstrapReadRevision) return;
    if (view.settingsRevision && next.config_revision !== view.settingsRevision) view.clearCommands();
    view.data = next;
    view.renderParameters();
    if (view.managedSetup?.installation_id && view.data.config.managed && view.data.installation_id === view.managedSetup.installation_id) view.managedSetup.providers = view.data.providers;
    if (hydrate) view.hydrateConfig();
    else if (!automatic || view.document.activeElement?.tagName !== "SELECT") {
      for (const kind of ["chat", "embedding", "rerank"]) {
        const select = view.$(`${kind}-provider`);
        const version = JSON.stringify(next.providers?.[kind] || []);
        if (!automatic || select.dataset.providers !== version) { view.providerSelect(kind, select.value); select.dataset.providers = version; }
      }
      if (!view.$("account-dialog").open) view.refreshAccountPlatforms();
      view.updateConnectionMode();
    }
    const nextStatusSignature = JSON.stringify([view.data.config.configured, view.data.status, view.data.managed_service]);
    if (!automatic || nextStatusSignature !== view.statusSignature) { view.renderStatus(); view.statusSignature = nextStatusSignature; }
    const previous = view.scopeId;
    view.updateUserChoices();
    view.$("memory-fields").disabled = !view.scopeId || view.locks.has("memory");
    view.$("setup-fields").disabled = view.locks.has("setup");
    if (previous !== view.scopeId) view.backgroundDraft = null;
    if (!view.locks.has("memory")) view.renderScope();
    else view.renderReferences();
    const configured = new Set((view.data.config.allowed_accounts || []).map(view.accountKey));
    view.$("paused-banks-notice").hidden = !view.data.scopes.some((item) => (!configured.has(view.accountKey(item)) || item.historical)
      && (item.summary?.maintenance || item.summary?.bank_missing || item.summary?.reflect_unknown?.length));
    view.$("paused-banks-notice").querySelector("span").textContent = view.data.scopes.some((item) => item.summary?.reflect_unknown?.length)
      ? "有 Reflect 请求需要解除阻塞" : "有记忆库暂停读写";
    if (pollMaintenance) await view.readMaintenanceStatus();
    view.renderServiceVersion();
    view.$("settings-conflict").hidden = !view.settingsRevision || view.data.config_revision === view.settingsRevision;
    view.$("startup").hidden = true;
    view.$("update-error").hidden = true;
    if (["running", "interrupted"].includes(view.data.connection_job?.status) && view.managedMode() && !view.modelWatch) void view.watchModels(view.data.connection_job.request_id, true);
    else if (view.data.model_job?.status === "running" && !view.modelWatch) void view.watchModels();
    else if (view.managedMode() && view.data.connection_job?.config_revision === view.data.config_revision && view.data.connection_job?.message) {
      view.$("model-progress").hidden = false;
      view.$("model-progress").textContent = view.data.connection_job.message;
    }
  }

  function scheduleRefresh(milliseconds = 5000) {
    view.clearTimeout(view.refreshTimer);
    if (!view.viewReady) return;
    view.refreshTimer = view.setTimeout(() => void view.autoRefresh(), milliseconds);
  }

  async function autoRefresh() {
    if (view.document.hidden || view.refreshRunning || !view.viewReady || view.locks.size) {
      view.scheduleRefresh();
      return;
    }
    view.refreshRunning = true;
    try {
      await view.refreshBootstrap({ automatic: true });
      view.refreshFailures = 0;
      view.$("update-error").hidden = true;
    } catch {
      view.refreshFailures++;
      if (view.data) { view.data.status = { connected: null, label: "连接状态无法确认", message: "无法获取插件状态，请检查 AstrBot 连接。" }; view.statusSignature = ""; view.renderStatus(); }
      view.$("update-error").hidden = false;
      view.$("update-error-message").textContent = "暂时无法获取最新数据，正在自动重试。";
    } finally {
      view.refreshRunning = false;
      view.scheduleRefresh(Math.min(30000, 5000 * (view.refreshFailures + 1)));
    }
  }

  async function refreshView() {
    if (!view.viewReady) { view.window.location.reload(); return; }
    await view.refreshBootstrap();
    view.$("update-error").hidden = true;
    view.notify("状态已刷新。");
    view.scheduleRefresh();
  }

  async function start() {
    if (view.startRunning) return;
    view.startRunning = true;
    view.$("startup").hidden = false;
    view.$("startup").textContent = "正在连接 AstrBot…";
    view.$("startup").classList.remove("error");
    view.$("refresh").disabled = true;
    try {
      view.bridge = view.window.AstrBotPluginView;
      if (!view.bridge) throw new Error("请从 AstrBot 的插件详情页打开「记忆管理」。");
      const context = await view.withTimeout(view.bridge.ready(), 10000, "连接 AstrBot 超时，请重新加载插件页面。");
      const theme = (value) => { view.document.documentElement.dataset.theme = (value || view.bridge.getContext?.())?.isDark ? "dark" : "light"; };
      theme(context); view.offContext?.(); view.offContext = view.bridge.onContext?.(theme);
      await view.refreshBootstrap({ hydrate: true, pollMaintenance: false });
      view.$("refresh").disabled = false;
      view.$("refresh").textContent = "重新获取";
      view.$("update-error").hidden = true;
      view.activateTab(view.data.config.configured ? "memories" : "setup");
      view.viewReady = true;
      view.renderScope();
      view.scheduleRefresh();
      void view.readMaintenanceStatus().catch((error) => view.notify(view.errorMessage(error), true));
    } catch (error) {
      view.$("startup").textContent = view.errorMessage(error);
      view.$("startup").classList.add("error"); view.$("startup").setAttribute("role", "alert");
      view.$("refresh").disabled = false;
      view.$("refresh").textContent = view.viewReady ? "重新获取" : "重新加载页面";
      view.$("update-error").hidden = false; view.$("update-error-message").textContent = "页面加载失败，请重试。";
    } finally { view.startRunning = false; }
  }

  function bindEvents() {
  const tabs = [...view.document.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => view.activateTab(tab.id.slice(4)));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next == null) return;
      event.preventDefault();
      view.activateTab(tabs[next].id.slice(4), true);
    });
  });
  view.$("settings-form").addEventListener("submit", (event) => { event.preventDefault(); void (view.managedMode() ? view.saveAccess() : view.saveSettings()).catch((error) => view.notify(view.errorMessage(error), true)); });
  view.$("parameters-form").addEventListener("input", () => {
    if (view.parameterDraft) view.parameterDraft.dirty = true;
    view.$("parameters-feedback").hidden = true;
    view.renderParameters();
  });
  view.$("parameters-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void view.saveParameters().catch((error) => view.notify(view.errorMessage(error), true, "parameters-feedback"));
  });
  view.$("background-recheck").addEventListener("click", () => void view.loadBackground({ manual: true }));
  view.$("background-save").addEventListener("click", () => void view.saveBackground());
  view.$("background-enabled")?.addEventListener("change", () => view.toggleBackground());
  view.$("background-settings").addEventListener("click", () => {
    view.activateTab("setup");
    view.$("memory-background_max_tokens").focus();
  });
  view.$("check-connection").addEventListener("click", () => void view.perform(view.$("check-connection"), "setup", async () => {
    await view.checkConnection(view.connectionPayload());
    await view.refreshBootstrap();
  }));
  view.$("add-account").addEventListener("click", view.addAccount);
  view.$("account-platform").addEventListener("change", view.updateAccountHint);
  view.$("account-form").addEventListener("submit", (event) => { event.preventDefault(); void view.submitAccount().catch((error) => view.notify(view.errorMessage(error), true, "account-feedback")); });
  view.$("authorize-account").addEventListener("click", () => {
    const scope = view.data.scopes.find((item) => item.scope_id === view.scopeId);
    if (scope) void view.prepareAuthorization(view.$("authorize-account"), scope.platform_instance_id, scope.user_id);
  });
  view.$("confirm-authorization").addEventListener("click", () => void view.confirmAuthorization());
  view.$("cancel-authorization").addEventListener("click", () => view.$("authorization-dialog").close());
  view.$("authorization-dialog").addEventListener("close", () => { view.authorizationState = null; });
  view.$("remove-account").addEventListener("click", view.requestAccountRemoval);
  view.$("cancel-remove-account").addEventListener("click", () => view.$("remove-account-dialog").close());
  view.$("confirm-remove-account").addEventListener("click", () => void view.confirmAccountRemoval());
  view.$("remove-account-dialog").addEventListener("close", () => { view.accountRemovalState = null; });
  for (const radio of view.document.querySelectorAll('[name="connection-mode"]')) radio.addEventListener("change", () => {
    view.clearCommands();
    view.$("api-key").value = "";
    view.$("connection-result").hidden = true;
    view.$("api-url").value = radio.value === "managed" ? view.data.installation_api_url || "" : view.data.config.api_url || "";
    view.updateConnectionMode();
    view.managedReadRevision++;
    view.managedSetup = null;
    if (radio.value === "managed") void view.loadManagedSetup().catch((error) => view.notify(view.errorMessage(error), true));
    view.renderServiceVersion();
  });
  view.$("save-access").addEventListener("click", () => void view.saveAccess());
  view.$("prepare").addEventListener("click", () => void view.prepareInstallation(view.$("prepare")));
  view.$("reinstall").addEventListener("click", () => {
    view.clearCommands();
    view.$("deployment-heading").textContent = "重新部署";
    view.$("deployment-description").textContent = "在原服务器、原安装目录执行。";
    view.$("generate-deployment").hidden = false;
    view.$("generate-deployment").textContent = "生成部署命令";
    view.$("deployment-dialog").dataset.redeploy = "true";
    view.$("check-deployment").hidden = true;
    view.$("deployment-feedback").hidden = true;
    view.$("deployment-dialog").showModal();
  });
  view.$("generate-deployment").addEventListener("click", () => void view.prepareInstallation(
    view.$("generate-deployment"), view.$("deployment-dialog").dataset.redeploy === "true",
  ));
  view.$("check-deployment").addEventListener("click", () => void view.perform(view.$("check-deployment"), "setup", () => view.checkConnection({}, "deployment-feedback"), "deployment-feedback"));
  view.$("service-version-check").addEventListener("click", () => void view.perform(view.$("service-version-check"), "setup", view.checkServiceVersion));
  view.$("view-upgrade").addEventListener("click", view.openUpgrade);
  view.$("go-upgrade").addEventListener("click", view.openUpgrade);
  view.$("ignore-upgrade").addEventListener("click", () => void view.perform(view.$("ignore-upgrade"), "setup", async () => {
    const response = await view.bridge.apiPost("service/upgrade/ignore", {
      config_revision: view.settingsRevision, connection_revision: view.data.config.connection_revision, target: view.data.backend.target,
    });
    view.settingsRevision = response.config_revision;
    await view.refreshBootstrap({ automatic: true, pollMaintenance: false });
    view.notify(response.message);
  }));
  for (const action of ["begin", "prepare", "verify", "resume", "cancel"]) view.$("upgrade-" + action).addEventListener("click", () => void view.upgradeAction(action, view.$("upgrade-" + action)));
  view.$("copy-upgrade").addEventListener("click", () => void view.perform(view.$("copy-upgrade"), "copy", () => view.copyCommand("upgrade-command", "upgrade-installation", "升级命令已复制，请在原服务器执行。")));
  view.$("copy-command").addEventListener("click", () => void view.perform(view.$("copy-command"), "copy", () => view.copyCommand("install-command", "installation", "部署命令已复制，请在运行 AstrBot 的服务器执行。")));
  for (const button of view.document.querySelectorAll("[data-guide]")) button.addEventListener("click", () => {
    const dialog = view.$(button.dataset.guide);
    if (button.dataset.guide === "backup-dialog") {
      const managed = view.data?.config.managed === true;
      for (const item of dialog.querySelectorAll("[data-managed-backup]")) item.hidden = !managed;
      view.$("backup-existing-service").hidden = managed;
      view.$("backup-heading").textContent = managed ? "备份与恢复" : "Bridge 备份与恢复";
    }
    if (!dialog.open) dialog.showModal();
  });
  for (const button of view.document.querySelectorAll("[data-copy]")) button.addEventListener("click", () => void view.perform(
    button, "copy", () => view.copyValue(button.dataset.copy, "已复制。", button.dataset.feedback), button.dataset.feedback,
  ));
  view.$("prepare-uninstall").addEventListener("click", () => {
    view.clearUninstall();
    view.$("delete-memory-data").checked = false;
    view.$("uninstall-feedback").hidden = true;
    view.$("uninstall-dialog").showModal();
  });
  view.$("generate-uninstall").addEventListener("click", () => void view.perform(view.$("generate-uninstall"), "setup", async () => {
    if (!view.$("delete-memory-data").checked) throw new Error("请先确认删除范围。");
    const response = await view.bridge.apiPost("uninstall/prepare", { config_revision: view.settingsRevision, delete_memory_data: true });
    if (!response.command) throw new Error("未生成卸载命令，请重试。");
    view.$("uninstall-command").value = response.command;
    view.$("uninstallation").hidden = false;
  }, "uninstall-feedback"));
  view.$("delete-memory-data").addEventListener("change", view.clearUninstall);
  view.$("copy-uninstall").addEventListener("click", () => void view.perform(view.$("copy-uninstall"), "copy", () => view.copyCommand("uninstall-command", "uninstallation", "卸载命令已复制，请在服务器执行。")));
  view.$("save-models").addEventListener("click", () => void view.perform(view.$("save-models"), "setup", async () => {
    if (!view.managedSetup?.has_installation && !view.data?.has_installation) throw new Error("请先生成并执行配套服务安装命令。");
    if (!view.managedSetup?.installation_id) await view.loadManagedSetup();
    view.modelRequestId ||= globalThis.crypto?.randomUUID?.() || `model-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const selection = { config_revision: view.settingsRevision, mode: "managed", installation_id: view.managedSetup?.installation_id, request_id: view.modelRequestId };
    for (const kind of ["chat", "embedding", "rerank"]) {
      const select = view.$(`${kind}-provider`);
      if ((kind !== "rerank" && !select.value) || select.selectedOptions[0]?.disabled) throw new Error("请选择可用的记忆处理模型和向量模型。");
      selection[`${kind}_provider_id`] = select.value;
    }
    view.$("model-progress").hidden = false;
    view.$("model-progress").textContent = "正在提交模型配置…";
    let response;
    try { response = await view.bridge.apiPost("managed/connect", selection); }
    catch (error) {
      const status = await view.bridge.apiPost("managed/status", { mode: "managed", installation_id: view.managedSetup?.installation_id }).catch(() => null);
      if (!status) { view.$("model-progress").textContent = "正在确认处理结果…"; await view.watchModels(view.modelRequestId, true); return; }
      if (status.request_id !== view.modelRequestId && status.status !== "running") { view.modelRequestId = ""; throw error; }
      response = status;
    }
    await view.watchModels(response.request_id, true);
  }));
  view.$("refresh").addEventListener("click", () => void view.perform(view.$("refresh"), "refresh", view.refreshView));
  view.$("reload-settings").addEventListener("click", () => void view.perform(view.$("reload-settings"), "setup", view.reloadSettings));
  view.$("service-alert-setup").addEventListener("click", () => view.activateTab("setup", true));
  view.$("dismiss-feedback").addEventListener("click", () => { view.$("feedback").hidden = true; view.clearTimeout(view.feedbackTimer); });
  view.$("scope-picker").addEventListener("change", () => { view.selectedAccount = view.$("scope-picker").value; view.scopeId = ""; view.updateUserChoices(); view.selectScope(view.scopeId); });
  view.$("bank-picker").addEventListener("change", () => view.selectScope(view.$("bank-picker").value));
  for (const button of view.document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => void view.setMode(button, button.dataset.mode));
  view.$("background-page").addEventListener("change", view.changeBackground);
  view.$("reading-mode").addEventListener("change", () => {
    if (view.readingDraft) view.readingDraft.dirty = true;
    view.$("reading-feedback").hidden = true;
    view.renderReading();
  });
  view.$("reading-save").addEventListener("click", () => void view.saveReading());
  view.$("reference-form").addEventListener("submit", (event) => { event.preventDefault(); void view.saveReferences(); });
  view.$("reference-form").addEventListener("input", (event) => {
    if (!view.referenceDraft?.selection) return;
    view.referenceDraft.dirty = true;
    view.$("reference-feedback").hidden = true;
    if (event.target.id === "reference-prompt") view.countPrompt(view.referenceDraft);
    view.renderReferences();
  });
  view.$("reference-reload").addEventListener("click", () => {
    view.clearReferenceDraft();
    void view.refreshBootstrap().catch((error) => view.notify(view.errorMessage(error), true, "reference-feedback"));
  });
  view.$("usage-open").addEventListener("click", () => void view.openUsage());
  view.$("usage-turn").addEventListener("change", () => void view.loadUsageTurn());
  view.$("usage-dialog").addEventListener("close", () => { if (!view.$("usage-dialog").open) view.resetUsage(); });
  view.$("usage-copy-json").addEventListener("click", () => {
    view.$("usage-json").hidden = false;
    void view.copyValue("usage-json", "JSON 已复制。", "usage-feedback").then(() => { view.$("usage-json").hidden = true; })
      .catch((error) => view.notify(view.errorMessage(error), true, "usage-feedback"));
  });
  for (const id of ["reflect-enabled", "reflect-timeout"]) view.$(id).addEventListener("input", () => {
    if (view.reflectDraft) view.reflectDraft.dirty = true;
    view.renderReflect();
  });
  view.$("reflect-save").addEventListener("click", () => void view.saveReflect());
  view.$("reflect-override").addEventListener("click", () => view.openReflectOverride());
  view.$("maintenance-reflect-override").addEventListener("click", () => view.openReflectOverride(view.maintenanceScope()?.scope_id));
  view.$("reflect-override-confirm").addEventListener("change", () => {
    view.$("reflect-override-submit").disabled = !view.$("reflect-override-confirm").checked;
  });
  view.$("reflect-override-submit").addEventListener("click", () => void view.confirmReflectOverride());
  view.$("reflect-override-dialog").addEventListener("close", () => {
    view.reflectOverrideState = null;
    view.$("reflect-override-confirm").checked = false;
  });
  view.$("copy-bank").addEventListener("click", () => void view.perform(view.$("copy-bank"), "copy",
    () => view.copyValue("scope-bank", "Bank ID 已复制。")));
  view.$("copy-control-plane").addEventListener("click", () => void view.perform(view.$("copy-control-plane"), "copy",
    () => view.copyValue("control-plane-url", "已复制")));
  view.$("scope-bank").addEventListener("focus", () => view.$("scope-bank").select());
  view.$("retry").addEventListener("click", () => void view.perform(view.$("retry"), "memory", async () => {
    const response = await view.bridge.apiPost(view.endpoint("action"), { action: "retry" });
    await view.refreshBootstrap();
    view.notify(response.message || "已安排重试。");
  }));
  view.$("maintenance-view").addEventListener("click", () => view.openMaintenance(view.scopeId));
  view.$("show-paused-banks").addEventListener("click", () => view.openMaintenance(view.data.scopes.find((item) => (item.summary?.maintenance || item.summary?.bank_missing || item.summary?.reflect_unknown?.length)
    && (item.historical || !(view.data.config.allowed_accounts || []).some((account) => view.accountKey(account) === view.accountKey(item))))?.scope_id));
  view.$("maintenance-scope").addEventListener("change", () => { view.maintenanceScopeId = view.$("maintenance-scope").value; view.renderMaintenance(); void view.readMaintenanceStatus(); });
  view.$("maintenance-retry").addEventListener("click", () => void view.readMaintenanceStatus());
  view.$("maintenance-resume").addEventListener("click", () => void view.maintenanceAction(view.$("maintenance-resume"), "maintenance_resume"));
  view.$("maintenance-bank-check").addEventListener("click", () => void view.maintenanceAction(view.$("maintenance-bank-check"), "bank_check"));
  view.$("maintenance-forget").addEventListener("click", () => void view.prepareForget(view.$("maintenance-forget"), view.maintenanceScope()?.scope_id));
  view.$("discard-pending").addEventListener("change", view.renderMaintenance);
  view.$("maintenance-discard").addEventListener("click", () => void view.maintenanceAction(view.$("maintenance-discard"), "maintenance_begin", { discard_pending: view.$("discard-pending").checked }));
  view.$("maintenance-dialog").addEventListener("close", () => { view.maintenanceScopeId = ""; view.maintenanceIdentity = ""; view.maintenanceState = null; });
  view.$("forget").addEventListener("click", () => void view.prepareForget(view.$("forget"), view.scopeId));
  view.$("forget-agree").addEventListener("change", () => { view.$("confirm-forget").disabled = !view.$("forget-agree").checked; });
  view.$("cancel-forget").addEventListener("click", () => { if (!view.locks.has("forget")) view.$("forget-dialog").close(); });
  view.$("forget-dialog").addEventListener("cancel", (event) => { if (view.locks.has("forget")) event.preventDefault(); });
  view.$("forget-dialog").addEventListener("close", () => { view.forgetState = null; view.$("forget-agree").checked = false; });
  view.$("confirm-forget").addEventListener("click", () => {
    if (!view.forgetState || !view.$("forget-agree").checked) return;
    const pending = view.forgetState;
    void view.perform(view.$("confirm-forget"), "forget", async () => {
      view.maintenanceReadRevision++;
      view.$("forget-agree").disabled = true;
      view.$("cancel-forget").disabled = true;
      try {
        const response = await view.bridge.apiPost(view.endpoint("action", pending.id), { action: "forget_confirm", confirmation: pending.confirmation });
        view.forgetState = null;
        view.$("forget-dialog").close();
        await view.refreshBootstrap();
        view.notify(response.message || "正在清空旧记忆。");
      } finally {
        view.$("forget-agree").disabled = false;
        view.$("cancel-forget").disabled = false;
        if (view.$("forget-dialog").open) {
          view.forgetState = null;
          view.$("forget-agree").checked = false;
          view.$("forget-agree").disabled = true;
          view.$("forget-message").textContent = "操作未完成，请关闭窗口后重新发起清空。";
        }
      }
    }, "forget-feedback").finally(() => { view.$("confirm-forget").disabled = true; });
  });
  for (const button of view.document.querySelectorAll("[data-close]")) button.addEventListener("click", () => view.$(button.dataset.close).close());
  view.$("deployment-dialog").addEventListener("close", () => { view.$("install-command").value = ""; view.$("installation").hidden = true; });
  view.$("uninstall-dialog").addEventListener("close", view.clearUninstall);
  view.$("upgrade-dialog").addEventListener("close", view.clearUpgrade);
  view.document.addEventListener("visibilitychange", () => { if (view.document.hidden) view.clearCommands(); });
  view.window.addEventListener("pagehide", () => { view.clearCommands(); view.backgroundReadRevision++; view.backgroundDraft = null; });


  view.document.addEventListener("visibilitychange", () => {
    view.clearTimeout(view.refreshTimer);
    if (!view.document.hidden) {
      if (view.backgroundDraft?.dirty) {
        if (!view.backgroundDraft.loading) view.backgroundDraft.attempted = false;
      } else {
        view.backgroundReadRevision++;
        view.backgroundDraft = null;
      }
      if (view.data) view.renderScope();
      view.scheduleRefresh(0);
    }
  });
  view.window.addEventListener("beforeunload", () => { view.clearTimeout(view.refreshTimer); view.clearTimeout(view.maintenanceExpiryTimer); view.offContext?.(); }, { once: true });

  if (!("closedBy" in (view.window.HTMLDialogElement?.prototype || {}))) {
    view.document.querySelectorAll('dialog[closedby="any"]').forEach((dialog) => {
      let outsidePointer;
      const outside = (event) => {
        const rect = dialog.getBoundingClientRect();
        return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
      };
      dialog.addEventListener("pointerdown", (event) => {
        outsidePointer = dialog.open && event.button === 0 && outside(event) ? event.pointerId : undefined;
      });
      dialog.addEventListener("pointerup", (event) => {
        if (dialog.open && outsidePointer === event.pointerId && outside(event)) dialog.close();
        outsidePointer = undefined;
      });
      dialog.addEventListener("pointercancel", () => { outsidePointer = undefined; });
      dialog.addEventListener("close", () => { outsidePointer = undefined; });
    });
  }

  }


  return { renderStatus, refreshBootstrap, scheduleRefresh, autoRefresh, refreshView, start, bindEvents };
}

export function createView(environment = globalThis) {
  const view = createState(environment);
  Object.assign(view, createDom(view), createSetup(view), createMaintenance(view), createMemories(view), createApp(view));
  return view;
}

if (typeof document !== "undefined") {
  const view = createView();
  view.bindEvents();
  void view.start();
}
