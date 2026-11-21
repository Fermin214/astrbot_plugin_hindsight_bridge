export function createMaintenance(view) {
  async function upgradeAction(action, button) {
    await view.perform(button, "setup", async () => {
      if (action === "prepare" && !view.$("upgrade-backup-confirmed").checked) throw new Error("请先完成插件数据备份，并确认已停止其他写入。");
      const response = await view.bridge.apiPost("service/upgrade", {
        action, config_revision: view.settingsRevision, backup_confirmed: view.$("upgrade-backup-confirmed").checked,
      });
      if (response.config_revision) view.settingsRevision = response.config_revision;
      if (response.command) {
        view.$("upgrade-command").value = response.command;
        view.$("upgrade-installation").hidden = false;
      }
      if (["cancel", "verify", "resume"].includes(action)) {
        view.$("upgrade-command").value = "";
        view.$("upgrade-installation").hidden = true;
        view.$("upgrade-backup-confirmed").checked = false;
      }
      view.notify(response.message, false, "upgrade-result");
      await view.refreshBootstrap({ automatic: true, pollMaintenance: false });
      view.renderServiceVersion();
    }, "upgrade-result");
    view.renderServiceVersion();
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

  function maintenanceSnapshot(scope, snapshot = view.data) {
    if (!scope?.summary?.maintenance || scope.pending || scope.summary.bank_missing || snapshot?.status?.recovering) return "";
    const record = scope.summary.maintenance;
    return JSON.stringify([
      scope.scope_id, scope.bank, scope.owner_id, scope.generation, scope.revision,
      snapshot.config.connection_revision, snapshot.config.api_url,
      record.connection_id, record.started_at, record.discard_pending,
    ]);
  }

  function syncMaintenanceState() {
    const scope = view.maintenanceScope();
    const identity = view.maintenanceSnapshot(scope);
    if (identity !== view.maintenanceIdentity) {
      view.maintenanceReadRevision++;
      view.maintenanceIdentity = identity;
      view.maintenanceState = null;
      view.maintenanceCheckedAt = 0;
      view.$("discard-pending").checked = false;
    } else if (view.maintenanceState?.ready && Date.now() - view.maintenanceCheckedAt > 30000) {
      view.maintenanceState = { ready: false, error: "暂停检查结果已过期。" };
    }
    if (identity && view.data.status?.connected === false) {
      view.maintenanceState = { ready: false, error: view.data.status.message || "记忆服务未连接。" };
    }
    if (!view.maintenanceState?.ready) view.clearTimeout(view.maintenanceExpiryTimer);
  }

  function scheduleMaintenanceExpiry() {
    view.clearTimeout(view.maintenanceExpiryTimer);
    if (!view.maintenanceIdentity || !view.maintenanceState?.ready || view.maintenanceState.phase !== "ready") return;
    const identity = view.maintenanceIdentity;
    const result = view.maintenanceState;
    const checkedAt = view.maintenanceCheckedAt;
    view.maintenanceExpiryTimer = view.setTimeout(() => {
      if (identity !== view.maintenanceIdentity || result !== view.maintenanceState || checkedAt !== view.maintenanceCheckedAt) return;
      view.maintenanceState = { ready: false, error: "暂停检查结果已过期。" };
      view.renderMaintenance();
      view.renderScope();
    }, Math.max(0, 30000 - (Date.now() - checkedAt)));
  }

  async function readMaintenanceStatus() {
    view.syncMaintenanceState();
    const revision = ++view.maintenanceReadRevision;
    const identity = view.maintenanceIdentity;
    const id = view.maintenanceScope()?.scope_id;
    if (!identity) { view.renderMaintenance(); return; }
    let state;
    try {
      const response = await view.withTimeout(
        view.bridge.apiGet(view.endpoint("action", id), { action: "maintenance_status" }),
        15000, "获取暂停进度超时。",
      );
      state = response.maintenance;
      if (!state || typeof state.ready !== "boolean") throw new Error("服务未返回有效的暂停进度。");
      const scope = view.data?.scopes.find((item) => item.scope_id === id);
      const record = scope?.summary?.maintenance;
      if (state.bank && (state.bank !== scope?.bank || state.connection_id !== record?.connection_id
          || state.started_at !== record?.started_at || state.discard_pending !== record?.discard_pending)) {
        throw new Error("暂停范围已变化，请重新获取用户状态。");
      }
    } catch (error) {
      state = { phase: "draining", ready: false, error: view.errorMessage(error) };
    }
    if (revision !== view.maintenanceReadRevision || identity !== view.maintenanceIdentity || id !== view.maintenanceScope()?.scope_id) return;
    view.maintenanceState = state;
    view.maintenanceCheckedAt = Date.now();
    view.renderMaintenance();
    view.renderScope();
    view.scheduleMaintenanceExpiry();
  }

  function maintenanceScope() {
    return view.data?.scopes.find((item) => item.scope_id === (view.maintenanceScopeId || view.scopeId));
  }

  function renderMaintenance() {
    view.syncMaintenanceState();
    const scope = view.maintenanceScope();
    const scopes = (view.data?.scopes || []).filter((item) => item.summary?.maintenance || item.summary?.bank_missing || item.summary?.reflect_unknown?.length || item.scope_id === view.maintenanceScopeId);
    view.$("maintenance-scope").replaceChildren(...scopes.map((item) => view.option(item.scope_id, view.scopeName(item))));
    view.$("maintenance-scope").value = scope?.scope_id || "";
    view.$("maintenance-detail").textContent = scope?.summary?.maintenance || scope?.summary?.bank_missing
      ? view.maintenanceDescription(scope.summary, view.maintenanceState)
      : scope?.summary?.reflect_unknown?.length ? "有 Reflect 请求结果未知，请确认服务没有残留任务后解除阻塞。" : "这个记忆库未暂停读写。";
    view.$("maintenance-bank-check").hidden = !scope?.summary?.bank_missing;
    view.$("maintenance-reflect-override").hidden = !scope?.summary?.reflect_unknown?.length;
    view.$("maintenance-bank-check").disabled = view.locks.has("memory") || !!view.data?.status?.recovering || view.data?.status?.connected === false;
    view.$("maintenance-retry").hidden = !scope?.summary?.maintenance || !view.maintenanceState?.error;
    view.$("maintenance-resume").hidden = !scope?.summary?.maintenance;
    view.$("maintenance-resume").disabled = !scope || !!scope.summary?.bank_missing || !!view.maintenanceState?.bank_missing || view.data?.status?.connected === false || view.maintenanceState?.ready !== true || view.maintenanceState.phase !== "ready" || view.locks.has("memory");
    const discardable = scope?.summary?.can_discard === true;
    view.$("discard-records").hidden = !discardable;
    if (!discardable) view.$("discard-pending").checked = false;
    view.$("maintenance-discard").disabled = !discardable || !view.$("discard-pending").checked || view.locks.has("memory");
    const configured = (view.data?.config.allowed_accounts || []).some((account) => view.accountKey(account) === view.accountKey(scope || {}));
    view.$("maintenance-forget").hidden = configured || !scope;
    view.$("maintenance-forget").disabled = view.locks.has("memory") || !!view.data?.status?.recovering || !!view.data?.backend?.pending || !!view.data?.backend?.upgrade_paused;
  }

  function openMaintenance(id) {
    view.maintenanceScopeId = id || view.data?.scopes.find((item) => item.summary?.maintenance || item.summary?.bank_missing || item.summary?.reflect_unknown?.length)?.scope_id || "";
    view.renderMaintenance();
    view.$("maintenance-feedback").hidden = true;
    view.$("maintenance-dialog").showModal();
    void view.readMaintenanceStatus();
  }

  async function maintenanceAction(button, action, extra = {}) {
    if (action === "maintenance_status") { await view.readMaintenanceStatus(); return; }
    await view.perform(button, "memory", async () => {
      const id = view.maintenanceScope()?.scope_id;
      if (!id) throw new Error("请选择记忆库。");
      view.maintenanceState = null;
      view.maintenanceCheckedAt = 0;
      view.renderMaintenance();
      let response;
      try {
        response = await view.bridge.apiPost(view.endpoint("action", id), { action, ...extra,
          ...(["maintenance_begin", "bank_check"].includes(action) ? { config_revision: view.settingsRevision } : {}) });
      } catch (error) {
        if (action === "maintenance_begin") await view.refreshBootstrap({ automatic: true, pollMaintenance: false }).catch(() => {});
        throw error;
      }
      if (response.config_revision) view.settingsRevision = response.config_revision;
      await view.refreshBootstrap();
      if (action === "maintenance_begin") view.$("discard-pending").checked = false;
      view.notify(response.message || "读写暂停已解除。", false, "maintenance-feedback");
    }, "maintenance-feedback");
    view.renderMaintenance();
  }

  function clearUpgrade() {
    view.$("upgrade-command").value = "";
    view.$("upgrade-installation").hidden = true;
    view.$("upgrade-backup-confirmed").checked = false;
  }

  function clearCommands() {
    view.$("install-command").value = "";
    view.$("installation").hidden = true;
    view.clearUninstall();
    view.clearUpgrade();
  }

  function openUpgrade() {
    const backend = view.data?.backend;
    if (!backend?.managed || !(view.data.config.managed || backend.pending || backend.upgrade_paused)) return;
    view.activateTab("setup");
    if (!view.$("upgrade-dialog").open) view.$("upgrade-dialog").showModal();
  }

  async function prepareForget(button, id) {
    await view.perform(button, "memory", async () => {
      const response = await view.bridge.apiPost(view.endpoint("action", id), { action: "forget_prepare" });
      if (!response.confirmation) throw new Error("无法准备清空操作，请重试。");
      view.forgetState = { id, confirmation: response.confirmation };
      view.$("forget-user").textContent = view.scopeName(view.data.scopes.find((item) => item.scope_id === id));
      view.$("forget-message").textContent = "清空后暂停记忆，完成后可切回读写模式。";
      view.$("forget-feedback").textContent = "";
      view.$("forget-agree").checked = false;
      view.$("forget-agree").disabled = false;
      view.$("confirm-forget").disabled = true;
      view.$("forget-dialog").showModal();
    }, button === view.$("maintenance-forget") ? "maintenance-feedback" : "feedback");
  }

  return { upgradeAction, maintenanceDescription, maintenanceSnapshot, syncMaintenanceState, scheduleMaintenanceExpiry, readMaintenanceStatus, maintenanceScope, renderMaintenance, openMaintenance, maintenanceAction, clearUpgrade, clearCommands, openUpgrade, prepareForget };
}
