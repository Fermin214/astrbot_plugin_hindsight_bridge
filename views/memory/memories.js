export function createMemories(view) {
  function refreshAccountPlatforms() {
    const select = view.$("account-platform");
    const previous = select.value;
    select.replaceChildren(view.option("", "请选择平台实例"));
    (view.data.platforms || []).forEach((platform) => select.append(view.option(platform.id, platform.name || platform.id,
      platform.supported === false || platform.enabled === false)));
    if ((view.data.platforms || []).some((platform) => platform.id === previous)) select.value = previous;
    view.updateAccountHint();
  }

  function updateAccountHint() {
    const platform = view.data?.platforms?.find((item) => item.id === view.$("account-platform").value);
    const input = view.$("account-user");
    view.$("account-user-label").textContent = platform?.user_label || "用户 ID";
    input.inputMode = platform?.numeric_user_id ? "numeric" : "text";
    if (platform?.numeric_user_id) { input.pattern = "[0-9]+"; input.maxLength = 20; }
    else { input.removeAttribute("pattern"); input.removeAttribute("maxlength"); }
    input.placeholder = platform?.user_hint || "填写用户 ID";
  }

  function addAccount() {
    view.$("account-form").reset();
    view.refreshAccountPlatforms();
    view.$("account-feedback").hidden = true;
    view.$("account-dialog").showModal();
  }

  function accountKey(account) {
    return JSON.stringify([account.platform_instance_id, account.user_id]);
  }

  function accountScopes(key = view.selectedAccount) {
    const account = (view.data?.config.allowed_accounts || []).find((item) => view.accountKey(item) === key);
    if (!account) return [];
    const scopes = view.data.scopes.filter((item) => view.accountKey(item) === key);
    const primary = scopes.find((item) => !item.pending && !item.historical) || scopes.find((item) => item.pending);
    return [primary, ...scopes.filter((item) => item !== primary
      && ["historical", "disconnected", "confirmation"].includes(item.availability))].filter(Boolean);
  }

  function updateUserChoices() {
    const accounts = view.data.config.allowed_accounts || [];
    if (!accounts.some((item) => view.accountKey(item) === view.selectedAccount)) view.selectedAccount = accounts[0] ? view.accountKey(accounts[0]) : "";
    const choices = accounts.map((account) => {
      const platform = view.data.platforms?.find((item) => item.id === account.platform_instance_id);
      return [view.accountKey(account), `${account.user_id} · ${platform?.name || account.platform_instance_id}`];
    });
    view.$("scope-picker").replaceChildren(...(choices.length ? choices : [["", "还没有用户"]]).map(([id, name]) => view.option(id, name)));
    view.$("scope-picker").value = view.selectedAccount;
    view.$("scope-picker").disabled = !accounts.length;
    const scopes = view.accountScopes();
    if (!scopes.some((item) => item.scope_id === view.scopeId)) view.scopeId = scopes[0]?.scope_id || "";
    view.$("bank-picker").replaceChildren(...scopes.map((item) => view.option(item.scope_id,
      item.pending ? item.pending_reason === "platform" ? "等待连接" : "等待开通"
        : `${item.availability === "historical" ? "旧记忆库 · " : ""}机器人 ${item.bot_id}${item.availability === "disconnected" ? "（未连接）" : item.availability === "confirmation" ? "（身份待确认）" : ""}`)));
    view.$("bank-picker").value = view.scopeId;
    view.$("bank-range-field").hidden = scopes.length < 2;
    view.$("memory-fields").hidden = !accounts.length;
    view.$("no-scopes").hidden = !!accounts.length;
  }

  function scopeName(scope) {
    if (!scope) return "记忆库";
    const platform = view.data?.platforms?.find((item) => item.id === scope.platform_instance_id);
    return `${scope.user_id} · ${platform?.name || scope.platform_instance_id}${scope.bot_id ? ` · 机器人 ${scope.bot_id}` : ""}`;
  }

  function recallDescription(recall) {
    if (!recall) return "尚未检索";
    const labels = { success: "成功", empty: "无相关结果", error: "失败", skipped: "已跳过", on_demand: "按需读取" };
    const label = recall.outcome === "error" && recall.code?.includes("timeout") ? "超时" : labels[recall.outcome] || "结果未知";
    let result = `${new Date(recall.last_at * 1000).toLocaleString()} · ${label}`;
    if (recall.error_message) result += `：${recall.error_message}`;
    return result;
  }

  function renderScope() {
    view.renderMaintenance();
    const scope = view.data?.scopes.find((item) => item.scope_id === view.scopeId);
    const summary = scope?.summary || {};
    const account = (view.data?.config.allowed_accounts || []).find((item) => view.accountKey(item) === view.selectedAccount);
    view.$("user-bot").textContent = scope?.bot_id ? `机器人 ${scope.bot_id}` : "";
    view.$("user-availability").textContent = { historical: "旧记忆库", disconnected: "机器人未连接", confirmation: "身份待确认" }[scope?.availability] || "";
    view.$("pending-account").hidden = !scope?.pending;
    view.$("pending-account-message").textContent = scope?.pending ? view.pendingScopeHelp(scope) : "";
    view.$("authorize-account").hidden = scope?.pending_reason !== "authorization";
    const unavailable = !scope || scope.pending || scope.historical || view.data?.status?.recovering;
    view.$("mode-control").hidden = !!scope?.historical;
    view.$("historical-mode").hidden = !scope?.historical;
    view.$("historical-mode").textContent = scope?.historical ? `原记忆模式：${view.modeLabels[scope.mode] || scope.mode}` : "";
    for (const mode of ["active", "readonly", "paused"]) {
      const button = view.document.querySelector(`[data-mode="${mode}"]`);
      button.setAttribute("aria-pressed", String(scope?.mode === mode));
      button.disabled = !!unavailable || view.locks.has("memory") || (mode !== "paused" && !!(summary.maintenance || summary.bank_missing));
    }
    view.$("forget").disabled = !scope || !!scope.pending || !!view.data?.status?.recovering || view.locks.has("memory");
    view.$("remove-account").disabled = !account || view.locks.has("setup") || !!view.data?.backend?.pending || !!view.data?.backend?.upgrade_paused;
    view.$("scope-bank").value = scope?.bank || "";
    view.$("copy-bank").disabled = !scope?.bank;
    view.$("bank-state").textContent = scope?.bank_state === "new" ? "等待首次保存" : "";
    view.$("bank-state").hidden = !view.$("bank-state").textContent;
    const counts = summary.counts || {};
    view.$("saved-count").textContent = Number.isSafeInteger(summary.saved_count) && summary.saved_count >= 0 ? summary.saved_count : !scope || scope.pending ? "待开通" : "暂不可用";
    view.$("pending-count").textContent = ["ready", "submitting", "unknown", "tracking", "deferred", "reconcile_required", "held", "cancel_requested"].reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    view.$("failed-count").textContent = (Number(counts.failed) || 0) + (Number(counts.dead) || 0);
    view.$("queue-summary").textContent = counts.reconcile_required ? `有 ${counts.reconcile_required} 条记录需要确认保存结果。` : "";
    view.$("queue-summary").hidden = !view.$("queue-summary").textContent;
    view.$("scope-error").hidden = !summary.error_message;
    view.$("scope-error").textContent = summary.error_message || "";
    view.$("retry").disabled = !!unavailable || !!summary.maintenance || !!summary.bank_missing || view.locks.has("memory");
    view.$("retry").hidden = view.$("retry").disabled || !(counts.failed || counts.dead);
    view.$("scope-recall-status").textContent = view.recallDescription(summary.recall);
    view.$("scope-skip-row").hidden = !summary.skip;
    view.$("scope-skip-status").textContent = summary.skip
      ? `${new Date(summary.skip.at * 1000).toLocaleString()} · ${summary.skip.message}` : "";
    view.$("maintenance-state").textContent = summary.bank_missing ? "找不到原记忆库，读写已暂停。请恢复原库，或清空旧记忆后重新开始。" : summary.maintenance ? "这个记忆库的插件读写已暂停。" : "";
    view.$("maintenance-state").hidden = !view.$("maintenance-state").textContent;
    view.$("maintenance-view").hidden = !summary.maintenance && !summary.bank_missing;
    view.renderBackground();
    view.renderReading();
    view.renderReferences();
    view.renderReflect();
  }

  function clearReferenceDraft() {
    view.clearTimeout(view.promptCountTimer);
    view.resetUsage();
    view.$("reference-form").reset();
    view.$("reference-fields").disabled = true;
    view.$("reference-save").disabled = true;
    view.$("usage-open").disabled = true;
    view.$("reference-tokens").textContent = "";
    view.$("reference-pruning").hidden = true;
    view.$("reference-feedback").hidden = true;
    view.referenceDraft = null;
  }

  function renderReferences() {
    const scope = view.data?.scopes.find((item) => item.scope_id === view.scopeId);
    const available = Number.isInteger(scope?.reference_revision) && !scope.historical && !scope.pending;
    const key = available ? JSON.stringify([
      view.data.config.connection_revision, view.scopeId, scope.owner_id, scope.bank, scope.generation, scope.revision,
    ]) : undefined;
    const changed = available && view.referenceDraft && (view.referenceDraft.revision !== scope.reference_revision
      || view.referenceDraft.configRevision !== view.data.config_revision);
    if (view.referenceDraft?.key !== key || (changed && !view.referenceDraft.dirty)) {
      view.clearReferenceDraft();
      if (available) view.referenceDraft = { key, id: view.scopeId, revision: scope.reference_revision,
        observedRevision: scope.reference_revision, scopeRevision: scope.revision, configRevision: view.data.config_revision };
    }
    const draft = view.referenceDraft;
    const conflict = changed && draft?.dirty;
    if (draft && draft.observedRevision !== scope.reference_revision) {
      draft.observedRevision = scope.reference_revision;
      view.resetUsage();
    }
    const blocked = !draft?.selection || view.locks.has("memory");
    view.$("reference-fields").disabled = blocked;
    view.$("reference-save").disabled = blocked || !draft.dirty || conflict;
    view.$("reference-reload").hidden = !(draft?.failed || conflict);
    view.$("usage-open").disabled = !available || view.locks.has("memory");
    if (scope?.reference_error || conflict) {
      view.notify(scope.reference_error || "设置已变化，请重新载入后修改。", true, "reference-feedback");
    }
    const keep = view.$("reference-history").value;
    const pruning = keep !== "" && Number(keep) >= 0 && Number(keep) < draft?.selection?.history_limit;
    view.$("reference-pruning").textContent = pruning
      ? (Number(keep) === 0 ? "保存后关闭记录，并清除已有记录。" : "保存后删除超出的最旧记录。") : "";
    view.$("reference-pruning").hidden = !pruning;
    if (view.viewReady && draft && !draft.attempted) void view.loadReferences(draft);
  }

  async function loadReferences(draft) {
    draft.attempted = true;
    const revision = draft.revision;
    try {
      const response = await view.bridge.apiGet(view.endpoint("references", draft.id));
      if (view.referenceDraft !== draft || revision !== draft.revision) return;
      const scope = view.data.scopes.find((item) => item.scope_id === draft.id);
      const saved = response.selection;
      if (response.scope_revision !== scope?.revision || response.config_revision !== view.data.config_revision
        || saved?.revision !== revision) throw new Error("设置已变化，请重新载入。");
      draft.selection = saved;
      draft.promptLimit = response.prompt_limit;
      view.$("reference-prompt").value = saved.prompt;
      view.$("reference-tokens").textContent = `${response.prompt_tokens} / ${draft.promptLimit} token`;
      for (const type of ["world", "experience", "observation"]) view.$("reference-" + type).checked = saved.types.includes(type);
      view.$("reference-exclude").checked = saved.exclude_assistant;
      view.$("reference-capture-assistant").checked = saved.capture_assistant ?? true;
      view.$("reference-quoted").checked = saved.recall_on_quoted_or_link ?? false;
      view.$("reference-budget").value = saved.recall_budget ?? "mid";
      view.$("reference-limit").value = saved.detail_limit;
      view.$("reference-history").value = saved.history_limit;
    } catch (error) {
      if (view.referenceDraft !== draft || revision !== draft.revision) return;
      draft.failed = true;
      view.notify(view.errorMessage(error), true, "reference-feedback");
    }
    view.renderReferences();
  }

  function countPrompt(draft) {
    view.clearTimeout(view.promptCountTimer);
    const prompt = view.$("reference-prompt").value;
    view.$("reference-tokens").textContent = `… / ${draft.promptLimit} token`;
    view.promptCountTimer = view.setTimeout(async () => {
      let count = "—";
      try {
        const response = await view.bridge.apiPost(view.endpoint("references/count", draft.id), { prompt });
        count = response.prompt_tokens;
      } catch {}
      if (view.referenceDraft === draft && view.$("reference-prompt").value === prompt) {
        view.$("reference-tokens").textContent = `${count} / ${draft.promptLimit} token`;
      }
    }, 250);
  }

  async function saveReferences() {
    view.renderReferences();
    const draft = view.referenceDraft;
    if (!draft?.selection || view.$("reference-save").disabled || !view.$("reference-form").reportValidity()) return;
    const payload = { prompt: view.$("reference-prompt").value, exclude_assistant: view.$("reference-exclude").checked,
      capture_assistant: view.$("reference-capture-assistant").checked, recall_budget: view.$("reference-budget").value,
      recall_on_quoted_or_link: view.$("reference-quoted").checked,
      types: ["world", "experience", "observation"].filter((type) => view.$("reference-" + type).checked),
      detail_limit: view.$("reference-limit").valueAsNumber, history_limit: view.$("reference-history").valueAsNumber,
      revision: draft.revision, scope_revision: draft.scopeRevision, config_revision: draft.configRevision };
    await view.perform(view.$("reference-save"), "memory", async () => {
      const response = await view.bridge.apiPost(view.endpoint("references", draft.id), payload);
      if (view.referenceDraft !== draft) return;
      draft.dirty = false;
      draft.revision = response.selection.revision;
      draft.selection = response.selection;
      view.data.scopes.find((item) => item.scope_id === draft.id).reference_revision = draft.revision;
      view.$("reference-tokens").textContent = `${response.prompt_tokens} / ${draft.promptLimit} token`;
      view.notify("已保存", false, "reference-feedback");
      await view.refreshBootstrap();
    }, "reference-feedback");
  }

  function clearUsageDetail() {
    view.$("usage-deliveries").replaceChildren();
    view.$("usage-json").value = "";
    view.$("usage-json").hidden = true;
    view.$("usage-copy-json").hidden = true;
    view.$("usage-feedback").textContent = "";
  }

  function resetUsage() {
    view.usageReadRevision++;
    view.clearUsageDetail();
    view.$("usage-turn").replaceChildren();
    if (view.$("usage-dialog").open) view.$("usage-dialog").close();
  }

  async function openUsage() {
    const draft = view.referenceDraft;
    if (!draft) return;
    const revision = ++view.usageReadRevision;
    view.clearUsageDetail();
    view.$("usage-picker").hidden = true;
    view.$("usage-turn").replaceChildren();
    view.$("usage-dialog").showModal();
    view.notify("正在读取使用记录…", false, "usage-feedback");
    try {
      const { turns } = await view.bridge.apiGet(view.endpoint("usage", draft.id));
      if (revision !== view.usageReadRevision || view.referenceDraft !== draft || !view.$("usage-dialog").open) return;
      if (!turns.length) { view.notify("暂无使用记录。", false, "usage-feedback"); return; }
      view.$("usage-turn").replaceChildren(...turns.map((turn) => view.option(turn.id,
        `${new Date(turn.created_at * 1000).toLocaleString()} · ${turn.conversation_id} · ${turn.message_id}`)));
      view.$("usage-picker").hidden = false;
      await view.loadUsageTurn();
    } catch (error) {
      if (revision === view.usageReadRevision && view.$("usage-dialog").open) view.notify(view.errorMessage(error), true, "usage-feedback");
    }
  }

  async function loadUsageTurn() {
    const draft = view.referenceDraft;
    const turnId = view.$("usage-turn").value;
    const revision = ++view.usageReadRevision;
    view.clearUsageDetail();
    if (!turnId || !draft) return;
    try {
      const { turn } = await view.bridge.apiGet(view.endpoint(`usage/${encodeURIComponent(turnId)}`, draft.id));
      if (revision !== view.usageReadRevision || view.referenceDraft !== draft || !view.$("usage-dialog").open) return;
      view.$("usage-json").value = JSON.stringify(turn, null, 2);
      view.$("usage-copy-json").hidden = false;
      view.$("usage-deliveries").replaceChildren(...turn.deliveries.map((delivery, index) => view.usageDelivery(delivery, index, turn)));
    } catch (error) {
      if (revision === view.usageReadRevision && view.$("usage-dialog").open) view.notify(view.errorMessage(error), true, "usage-feedback");
    }
  }

  function usageDelivery(delivery, index, turn) {
    const kinds = { request: "聊天请求", recall: "细节检索", read_page: "读取知识页", search_pages: "搜索知识页",
      page_sources: "知识页来源", page_history: "知识页版本", reflect: "Reflect" };
    const statuses = { delivered: "已交付", no_match: "无匹配", filtered: "已筛除", duplicate: "条目重复",
      limit: "达到条数或长度上限", error: "读取失败", budget: "额度已用完", not_called: "工具未调用" };
    const sourceKinds = { user: "用户发言", assistant: "助手发言", mixed: "混合来源", unknown: "来源未知" };
    const types = { world: "世界事实", experience: "经历", observation: "观察" };
    const section = view.el("section", "usage-delivery");
    const title = kinds[delivery.kind] || delivery.kind;
    section.append(view.el("h3", "", `${index + 1}. ${title}`));
    const usedTools = delivery.kind === "request" && delivery.status === "not_called" && turn.deliveries.length > 1;
    if (!usedTools) section.append(view.el("p", "", [statuses[delivery.status] || delivery.status, delivery.code].filter(Boolean).join(" · ")));
    for (const item of delivery.items || []) {
      const labels = [item.id, types[item.type] || item.type, sourceKinds[item.source_kind] || "来源未知",
        item.document_id, item.occurred_start, item.occurred_end, item.mentioned_at].filter(Boolean);
      section.append(view.el("p", "usage-meta", labels.join(" · ")));
    }
    for (const page of delivery.pages || []) {
      section.append(view.el("p", "usage-meta", [page.page_id, page.mental_model_id, page.version, page.last_refreshed_at].filter(Boolean).join(" · ")));
    }
    if (delivery.text) {
      const field = view.el("textarea", "usage-text");
      field.id = `usage-text-${index}`;
      field.readOnly = true;
      field.rows = 5;
      field.value = delivery.text;
      field.setAttribute("aria-label", `${title}原文`);
      const button = view.el("button", "", "复制原文");
      button.type = "button";
      button.addEventListener("click", () => void view.copyValue(field.id, "原文已复制。", "usage-feedback", delivery.text)
        .catch((error) => view.notify(view.errorMessage(error), true, "usage-feedback")));
      section.append(field, button);
    }
    return section;
  }

  function renderReading() {
    const scope = view.data?.scopes.find((item) => item.scope_id === view.scopeId);
    const saved = scope?.reading || { revision: 0, mode: "automatic" };
    const snapshot = JSON.stringify([view.scopeId, scope?.bank, saved.revision, saved.scope_revision, view.data?.config_revision]);
    if (!view.readingDraft || view.readingDraft.id !== view.scopeId || view.readingDraft.bank !== scope?.bank || !view.readingDraft.dirty) {
      if (!view.readingDraft || view.readingDraft.id !== view.scopeId || view.readingDraft.bank !== scope?.bank) view.$("reading-feedback").hidden = true;
      view.readingDraft = { snapshot, id: view.scopeId, bank: scope?.bank, revision: saved.revision,
        scope_revision: saved.scope_revision, config_revision: view.data?.config_revision, dirty: false };
      view.$("reading-mode").value = saved.mode || "automatic";
    }
    const select = view.$("reading-mode");
    select.disabled = !scope || !!scope.pending || !!scope.historical || !!view.data?.status?.recovering
      || !!saved.error || view.locks.has("memory");
    const conflict = view.readingDraft.snapshot !== snapshot;
    view.$("reading-save").disabled = select.disabled || conflict || !view.readingDraft.dirty;
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
    view.$("reading-status").textContent = messages.join(" ");
    view.$("reading-status").hidden = !messages.length;
  }

  async function saveReading() {
    const draft = view.readingDraft;
    if (!draft) return;
    let saved = false;
    const payload = { mode: view.$("reading-mode").value, revision: draft.revision,
      scope_revision: draft.scope_revision, config_revision: draft.config_revision };
    await view.perform(view.$("reading-save"), "memory", async () => {
      const response = await view.bridge.apiPost(view.endpoint("reading", draft.id), payload);
      if (view.readingDraft !== draft || view.scopeId !== draft.id) return;
      draft.dirty = false;
      await view.refreshBootstrap();
      saved = true;
    }, "reading-feedback");
    if (saved && view.scopeId === draft.id) view.notify("已保存", false, "reading-feedback");
  }

  function renderReflect({ discard = false } = {}) {
    const scope = view.data?.scopes.find((item) => item.scope_id === view.scopeId);
    const saved = scope?.reflect || { revision: 0, enabled: false, timeout: 12 };
    const snapshot = JSON.stringify([view.scopeId, scope?.bank, saved.revision, saved.scope_revision, view.data?.config_revision]);
    const changedScope = !view.reflectDraft || view.reflectDraft.id !== view.scopeId || view.reflectDraft.bank !== scope?.bank;
    if (discard || changedScope || !view.reflectDraft.dirty) {
      view.reflectDraft = { snapshot, id: view.scopeId, bank: scope?.bank, revision: saved.revision,
        scope_revision: saved.scope_revision, config_revision: view.data?.config_revision, dirty: false };
      view.$("reflect-enabled").checked = !!saved.enabled;
      view.$("reflect-timeout").value = String(saved.timeout ?? 12);
      if (discard || changedScope) view.$("reflect-feedback").hidden = true;
    }
    const conflict = view.reflectDraft.snapshot !== snapshot;
    const unavailable = !scope || scope.pending || scope.historical || view.data?.status?.recovering || saved.error || view.locks.has("memory");
    view.$("reflect-enabled").disabled = !!unavailable || (!saved.enabled && !!scope?.summary?.maintenance);
    view.$("reflect-timeout").disabled = !!unavailable;
    view.$("reflect-save").disabled = !!unavailable || conflict || !view.reflectDraft.dirty;
    view.$("reflect-conflict").hidden = !conflict;
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
    view.$("reflect-status").textContent = messages.join(" ");
    view.$("reflect-status").hidden = !messages.length;
  }

  async function saveReflect() {
    const draft = view.reflectDraft;
    if (!draft) return;
    await view.perform(view.$("reflect-save"), "memory", async () => {
      const timeout = Number(view.$("reflect-timeout").value);
      if (!Number.isSafeInteger(timeout) || timeout < 3 || timeout > 30) throw new Error("Reflect 等待须为 3 到 30 秒的整数。");
      const payload = { enabled: view.$("reflect-enabled").checked, timeout, revision: draft.revision,
        scope_revision: draft.scope_revision, config_revision: draft.config_revision };
      const response = await view.bridge.apiPost(view.endpoint("reflect", draft.id), payload);
      if (view.reflectDraft !== draft || view.scopeId !== draft.id) return;
      draft.dirty = false;
      await view.refreshBootstrap();
      if (view.scopeId === draft.id) view.notify("已保存", false, "reflect-feedback");
    }, "reflect-feedback");
  }

  function backgroundSnapshot(scope) {
    return JSON.stringify([
      scope?.scope_id, scope?.bank, scope?.background?.scope_revision, scope?.background?.revision,
      view.data?.config_revision,
      !!scope?.historical, !!scope?.pending, !!scope?.summary?.maintenance, !!scope?.summary?.bank_missing,
    ]);
  }

  function backgroundPages(selection) {
    return selection?.pages || (selection?.page_id ? [{ page_id: selection.page_id }] : []);
  }

  function renderBackground() {
    const scope = view.data?.scopes.find((item) => item.scope_id === view.scopeId);
    const saved = scope?.background || { revision: 0, enabled: false, page_id: "" };
    const snapshot = view.backgroundSnapshot(scope);
    const changedScope = !view.backgroundDraft || view.backgroundDraft.id !== view.scopeId || view.backgroundDraft.bank !== scope?.bank;
    if (changedScope || (!view.backgroundDraft.dirty && view.backgroundDraft.snapshot !== snapshot)) {
      view.backgroundReadRevision++;
      view.backgroundDraft = { snapshot, id: view.scopeId, bank: scope?.bank, page_id: saved.page_id || "", enabled: saved.enabled === true,
        revision: saved.revision, scope_revision: saved.scope_revision, config_revision: view.data?.config_revision,
        selected: view.backgroundPages(saved).map((page) => ({ ...page })), dirty: false,
        pages: [], checks: [], loaded: false, loading: false, attempted: false };
      view.$("background-feedback").hidden = true;
    }
    const unavailable = !scope || scope.pending || scope.historical || view.data?.status?.recovering || saved.error;
    const blocked = unavailable || scope.summary?.maintenance || scope.summary?.bank_missing || scope.bank_state === "new";
    const select = view.$("background-page");
    const draft = view.backgroundDraft;
    const conflict = draft.snapshot !== snapshot;
    const selected = draft.selected;
    select.disabled = !!unavailable || view.locks.has("memory") || (blocked && !saved.enabled);
    const options = [view.option("", "不使用知识页")];
    view.backgroundDraft.pages.forEach((page) => options.push(view.option(page.page_id, page.path, !!blocked)));
    if (draft.page_id && !draft.pages.some((page) => page.page_id === draft.page_id)) options.push(view.option(draft.page_id, draft.page_id, !!blocked));
    select.replaceChildren(...options);
    select.value = draft.enabled ? draft.page_id : "";
    const rows = selected.map((page, index) => {
      const row = view.el("li", "knowledge-page");
      const native = draft.pages.find((item) => item.page_id === page.page_id);
      row.append(view.el("div", "knowledge-page-title", `${index + 1}. ${native?.path || page.page_id}`));
      const check = draft.enabled ? draft.checks.find((item) => item.page_id === page.page_id) : null;
      const usage = saved.status?.pages?.find((item) => item.page_id === page.page_id);
      if (check?.tokens != null) {
        const limit = check.limits?.tokens;
        const exceeded = limit != null && check.tokens > limit;
        row.append(view.el("span", `page-metrics${exceeded ? " error" : ""}`, limit == null
          ? `${check.tokens} token` : `${check.tokens} / ${limit} token${exceeded ? "（超限）" : ""}`));
        if (check.limits?.chars != null && check.chars > check.limits.chars) {
          row.append(view.el("span", "page-metrics error", `${check.chars} / ${check.limits.chars} 字符（超限）`));
        }
      }
      if (check?.code && check.code !== "background_too_large") row.append(view.el("p", "operation-status error", check.message));
      if (!draft.dirty && saved.enabled && usage) row.append(view.el("span", "page-status", usage.used ? "已使用" : usage.message));
      if (native?.index_stale === true && draft.enabled) row.append(view.el("span", "page-status", "有新记忆待刷新"));
      const actions = view.el("div", "knowledge-page-actions");
      for (const [label, direction] of [["上移", -1], ["下移", 1], ["移除", 0]]) {
        const button = view.el("button", "", label);
        button.type = "button";
        button.setAttribute("aria-label", `${label}第 ${index + 1} 篇知识页`);
        button.disabled = !!blocked || view.locks.has("memory") || (direction === -1 && index === 0)
          || (direction === 1 && index === selected.length - 1);
        button.addEventListener("click", () => {
          if (view.backgroundDraft !== draft) return;
          if (direction === 0) draft.selected.splice(index, 1);
          else [draft.selected[index], draft.selected[index + direction]] = [draft.selected[index + direction], draft.selected[index]];
          draft.page_id = draft.selected[0]?.page_id || "";
          draft.enabled = draft.enabled && draft.selected.length > 0;
          draft.dirty = true;
          view.$("background-feedback").hidden = true;
          view.renderBackground();
        });
        actions.append(button);
      }
      row.append(actions);
      return row;
    });
    view.$("background-selected").replaceChildren(...rows);
    const status = conflict ? "设置已更新，请刷新页面。" : saved.error || (saved.invalidated ? saved.message || "知识页选择已失效" : "");
    view.$("background-status").textContent = status;
    view.$("background-status").hidden = !status;
    view.$("background-recheck").disabled = !!blocked || view.backgroundDraft.loading || view.locks.has("memory");
    view.$("background-recheck").textContent = draft.loading ? "刷新中…" : "刷新知识页";
    view.$("background-save").disabled = !!unavailable || view.locks.has("memory") || conflict || !draft.dirty;
    if (view.viewReady && !blocked && !view.backgroundDraft.attempted && !view.locks.has("memory")) void view.loadBackground();
  }

  async function loadBackground({ manual = false } = {}) {
    const draft = view.backgroundDraft;
    if (!draft || draft.loading) return;
    const revision = ++view.backgroundReadRevision;
    draft.loading = true;
    draft.attempted = true;
    view.renderBackground();
    try {
      const response = await view.withTimeout(view.bridge.apiGet(view.endpoint("background", draft.id)), 8500, "获取知识页列表超时，请刷新页面重试。");
      if (revision !== view.backgroundReadRevision || view.backgroundDraft !== draft || view.scopeId !== draft.id) return;
      if (!Array.isArray(response.pages) || !response.selection) throw new Error("知识页列表无法读取，请刷新页面重试。");
      if (response.selection.revision !== draft.revision || response.scope_revision !== draft.scope_revision || response.config_revision !== draft.config_revision) throw new Error("记忆范围或知识页选择已变化，请重新获取用户状态。");
      draft.pages = response.pages;
      draft.checks = response.checks || [];
      draft.loaded = true;
      view.renderBackground();
      if (manual) view.notify(response.pages.length ? `已刷新 ${response.pages.length} 篇` : "暂无知识页", false, "background-feedback");
    } catch (error) {
      if (revision === view.backgroundReadRevision && view.backgroundDraft === draft && view.scopeId === draft.id) {
        view.notify(`知识页读取失败：${view.errorMessage(error)}`, true, "background-feedback");
        await view.refreshBootstrap({ automatic: true, pollMaintenance: false }).catch(() => {});
      }
    } finally {
      draft.loading = false;
      if (revision !== view.backgroundReadRevision) draft.attempted = false;
      if (view.backgroundDraft === draft && view.scopeId === draft.id) view.renderBackground();
    }
  }

  function changeBackground() {
    const draft = view.backgroundDraft;
    if (!draft) return;
    const pageId = view.$("background-page").value;
    if (pageId && !draft.selected.some((page) => page.page_id === pageId)) {
      if (draft.selected.length >= 8) {
        view.notify("最多选择 8 篇知识页", true, "background-feedback");
        view.renderBackground();
        return;
      }
      draft.selected.push({ page_id: pageId });
    }
    draft.enabled = !!pageId;
    draft.page_id = draft.selected[0]?.page_id || "";
    draft.dirty = true;
    view.$("background-feedback").hidden = true;
    view.renderBackground();
  }

  async function saveBackground() {
    const draft = view.backgroundDraft;
    if (!draft) return;
    let saved = false;
    const payload = { page_ids: draft.selected.map((page) => page.page_id), enabled: draft.enabled,
      revision: draft.revision, scope_revision: draft.scope_revision, config_revision: draft.config_revision };
    await view.perform(view.$("background-save"), "memory", async () => {
      const response = await view.bridge.apiPost(view.endpoint("background", draft.id), payload);
      if (view.backgroundDraft !== draft || view.scopeId !== draft.id) return;
      draft.dirty = false;
      await view.refreshBootstrap();
      view.renderBackground();
      if (view.backgroundDraft?.id === draft.id && response.selection?.revision === view.backgroundDraft.revision) {
        view.backgroundDraft.checks = response.checks || [];
        view.renderBackground();
      }
      saved = true;
    }, "background-feedback");
    if (saved && view.scopeId === draft.id) view.notify("已保存", false, "background-feedback");
  }

  function pendingScopeHelp(scope) {
    if (scope.pending_reason === "service") return "请先在插件设置中完成服务连接。";
    if (scope.pending_reason === "recovering") return "正在核对记忆库和保存任务，请等待检查完成。";
    if (scope.pending_reason === "authorization") return "请确认机器人身份，完成用户开通。";
    return "机器人尚未连接，请先在 AstrBot 中连接。";
  }

  function selectScope(id) {
    view.bootstrapReadRevision++;
    view.maintenanceReadRevision++;
    view.backgroundReadRevision++;
    view.backgroundDraft = null;
    view.scopeId = id;
    view.$("bank-picker").value = id;
    if (!view.$("maintenance-dialog").open) { view.maintenanceScopeId = ""; view.maintenanceState = null; view.maintenanceIdentity = ""; }
    view.$("discard-pending").checked = false;
    view.renderScope();
    void view.readMaintenanceStatus();
  }

  function endpoint(suffix, id = view.scopeId) {
    if (!id) throw new Error("请先选择用户。");
    return `scopes/${encodeURIComponent(id)}/${suffix}`;
  }

  async function requestAuthorization(platform, user) {
    const payload = { action: "prepare", platform_instance_id: platform, user_id: user, config_revision: view.settingsRevision };
    const response = await view.bridge.apiPost("accounts/authorize", payload);
    view.authorizationState = { ...payload, action: "confirm", confirmation: response.confirmation };
    view.$("authorization-description").textContent = `用户 ${user} / 机器人 ${response.bot_id}`;
    view.$("authorization-dialog").showModal();
  }

  async function prepareAuthorization(button, platform, user) {
    await view.perform(button, "setup", () => view.requestAuthorization(platform, user));
  }

  async function confirmAuthorization() {
    if (!view.authorizationState) return;
    const pending = view.authorizationState;
    view.authorizationState = null;
    await view.perform(view.$("confirm-authorization"), "authorization", async () => {
      let response;
      try { response = await view.bridge.apiPost("accounts/authorize", pending); }
      finally { view.$("authorization-dialog").close(); }
      await view.refreshBootstrap({ automatic: true });
      view.notify(response.message);
    });
  }

  async function saveAccounts(accounts, revision) {
    const response = await view.bridge.apiPost("settings/save", { allowed_accounts: accounts, config_revision: revision });
    if (response.saved !== true) throw new Error(response.message || "用户设置未保存，请重试。");
    view.settingsRevision = response.config_revision;
    view.data.config_revision = response.config_revision;
    const previous = new Set((view.data.config.allowed_accounts || []).map(view.accountKey));
    view.data.config.allowed_accounts = accounts.map((account) => ({ ...account }));
    const confirmed = new Set(accounts.map(view.accountKey));
    for (const scope of view.data.scopes) {
      const key = view.accountKey(scope);
      if (previous.has(key) !== confirmed.has(key)) {
        scope.historical = true;
        scope.availability = confirmed.has(key) ? "confirmation" : "removed";
      }
    }
    view.clearCommands();
    view.updateUserChoices();
    view.$("memory-fields").disabled = !view.scopeId || view.locks.has("memory");
    view.renderScope();
    try { await view.refreshBootstrap({ automatic: true }); }
    catch {
      view.$("update-error").hidden = false;
      view.$("update-error-message").textContent = "用户设置已保存，暂时无法获取最新状态，正在自动重试。";
      view.scheduleRefresh();
    }
  }

  async function submitAccount() {
    if (!view.$("account-form").reportValidity()) return;
    const platform = view.$("account-platform").value;
    const user = view.$("account-user").value.trim();
    if (!platform || !user || /[:\x00-\x1f\x7f]/.test(user)) throw new Error("请选择平台并填写有效的用户 ID。");
    const accounts = view.data.config.allowed_accounts || [];
    if (accounts.some((item) => item.platform_instance_id === platform && item.user_id === user)) throw new Error("这个用户已经添加，请在用户页确认机器人身份。");
    const revision = view.settingsRevision;
    await view.perform(view.$("save-account"), "setup", async () => {
      view.selectedAccount = view.accountKey({ platform_instance_id: platform, user_id: user });
      await view.saveAccounts([...accounts, { platform_instance_id: platform, user_id: user }], revision);
      view.$("account-dialog").close();
      try { await view.requestAuthorization(platform, user); }
      catch (error) { throw new Error(`用户已添加，身份确认尚未完成：${view.errorMessage(error)} 请在用户页点击「确认机器人身份」。`); }
    }, "account-feedback");
  }

  function requestAccountRemoval() {
    const account = (view.data.config.allowed_accounts || []).find((item) => view.accountKey(item) === view.selectedAccount);
    if (!account) return;
    view.accountRemovalState = { account, accounts: view.data.config.allowed_accounts.map((item) => ({ ...item })), revision: view.settingsRevision };
    view.$("remove-account-user").textContent = `${account.user_id} · ${view.data.platforms?.find((item) => item.id === account.platform_instance_id)?.name || account.platform_instance_id}`;
    view.$("remove-account-feedback").hidden = true;
    view.$("remove-account-dialog").showModal();
  }

  async function confirmAccountRemoval() {
    const pending = view.accountRemovalState;
    if (!pending) return;
    await view.perform(view.$("confirm-remove-account"), "setup", async () => {
      await view.saveAccounts(pending.accounts.filter((item) => view.accountKey(item) !== view.accountKey(pending.account)), pending.revision);
      view.accountRemovalState = null;
      view.$("remove-account-dialog").close();
      view.notify("用户已移除，已有记忆保留。");
    }, "remove-account-feedback");
  }

  async function setMode(button, mode) {
    const id = view.scopeId;
    if (!view.modeLabels[mode]) return;
    await view.perform(button, "memory", async () => {
      const response = await view.bridge.apiPost(view.endpoint("action", id), { action: "mode", mode });
      await view.refreshBootstrap();
      view.notify(response.message || "记忆模式已更新。");
    });
  }

  return { refreshAccountPlatforms, updateAccountHint, addAccount, accountKey, accountScopes, updateUserChoices, scopeName, recallDescription, renderScope, clearReferenceDraft, renderReferences, loadReferences, countPrompt, saveReferences, clearUsageDetail, resetUsage, openUsage, loadUsageTurn, usageDelivery, renderReading, saveReading, renderReflect, saveReflect, backgroundSnapshot, backgroundPages, renderBackground, loadBackground, changeBackground, saveBackground, pendingScopeHelp, selectScope, endpoint, requestAuthorization, prepareAuthorization, confirmAuthorization, saveAccounts, submitAccount, requestAccountRemoval, confirmAccountRemoval, setMode };
}
