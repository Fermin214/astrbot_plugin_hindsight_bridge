export function createSetup(view) {
  function providerSelect(kind, selected) {
    const select = view.$(`${kind}-provider`);
    select.replaceChildren(view.option("", kind === "rerank" ? "不使用重排模型" : "请选择模型"));
    const providers = (view.managedMode() && view.managedSetup ? view.managedSetup.providers : view.data.providers)?.[kind] || [];
    providers.forEach((provider) => {
      const label = [provider.name || provider.label || provider.id, provider.model].filter(Boolean).join(" / ");
      select.append(view.option(provider.id, provider.supported === false ? `${label}（${provider.reason || "暂不支持"}）` : label, provider.supported === false));
    });
    const found = providers.find((provider) => provider.id === selected);
    if (selected && !found) select.append(view.option(selected, `${selected}（供应商已不存在，请重新选择）`, true));
    select.value = selected || "";
  }

  function updateProviderHelp() {
    if (!view.data) return;
    const missing = [];
    for (const [kind, name] of [["chat", "记忆处理"], ["embedding", "向量"]]) {
      if (!((view.managedMode() && view.managedSetup ? view.managedSetup.providers : view.data.providers)?.[kind] || []).some((provider) => provider.supported !== false)) missing.push(name);
    }
    view.$("provider-help").textContent = missing.length ? `缺少可用的${missing.join("、")}模型，请到 AstrBot「模型供应商」添加。` : "";
    view.$("provider-help").hidden = !missing.length;
    view.$("save-models").disabled = missing.length > 0 || view.locks.has("setup") || !!view.data.backend?.pending || !!view.data.backend?.upgrade_paused;
  }

  function clearUninstall() {
    view.$("uninstallation").hidden = true;
    view.$("uninstall-command").value = "";
  }

  function managedMode() {
    return view.data?.capabilities?.managed_installation !== false
      && view.document.querySelector('[name="connection-mode"]:checked')?.value === "managed";
  }

  async function loadManagedSetup() {
    const revision = ++view.managedReadRevision;
    const response = await view.withTimeout(view.bridge.apiPost("managed/options", { mode: "managed", config_revision: view.settingsRevision }),
      10000, "获取配套安装记录超时，请重试。");
    if (revision !== view.managedReadRevision || !view.managedMode()) return;
    view.managedSetup = response;
    view.updateConnectionMode();
    view.$("api-url").value = response.api_url || "";
    for (const kind of ["chat", "embedding", "rerank"]) view.providerSelect(kind, response.selection?.[`${kind}_provider_id`]);
    view.updateProviderHelp();
    if (["running", "interrupted"].includes(response.job?.status) && !view.modelWatch) void view.watchModels(response.job.request_id, true);
  }

  function updateConnectionMode() {
    const managed = view.managedMode();
    const installable = view.data?.capabilities?.managed_installation !== false;
    view.$("connection-options").hidden = !installable;
    view.$("connection-options").disabled = !installable;
    view.$("setup-intro").hidden = !managed || (view.data?.config.managed === true && view.data?.config.configured === true);
    view.$("managed-settings").hidden = !managed;
    view.$("models-settings").hidden = !managed;
    view.$("maintenance").hidden = !managed || !view.data?.has_installation;
    view.$("api-key-field").hidden = managed;
    view.$("api-url").readOnly = managed;
    view.$("api-url").required = !managed;
    view.$("save-settings").hidden = managed;
    view.$("save-access").hidden = !managed;
    view.$("check-connection").hidden = managed;
  }

  function renderServiceVersion() {
    const backend = view.data?.backend || {};
    view.$("service-version").hidden = !view.data;
    view.$("service-version-text").textContent = backend.version ? `Hindsight ${backend.version}` : "版本尚未确认";
    if (view.versionCheck && view.versionCheck.identity !== view.versionCheckIdentity()) { view.versionCheckRevision++; view.versionCheck = null; }
    view.$("version-check-result").hidden = !view.versionCheck;
    view.$("version-check-result").textContent = view.versionCheck?.message || "";
    view.$("version-check-result").classList.toggle("error", view.versionCheck?.error === true);
    const upgrading = backend.managed && (!!backend.pending || !!backend.upgrade_paused);
    const available = view.data?.config.managed && backend.managed && backend.upgrade_available === true;
    const installable = view.data?.capabilities?.managed_installation !== false;
    view.$("upgrade-notice").hidden = !upgrading && (!available || backend.ignored === true);
    view.$("upgrade-notice-title").textContent = upgrading
      ? backend.resume_available ? "Hindsight 已升级，读写仍暂停" : "Hindsight 升级待完成"
      : `Hindsight 可升级：${backend.version} → ${backend.target}`;
    view.$("ignore-upgrade").hidden = upgrading;
    view.$("view-upgrade").textContent = upgrading ? "查看升级进度" : "查看升级";
    view.$("go-upgrade").hidden = !available && !upgrading;
    view.$("go-upgrade").textContent = upgrading ? "查看升级进度" : "升级服务";
    view.$("service-upgrade").hidden = !available && !upgrading;
    view.$("upgrade-begin-step").hidden = !installable;
    view.$("upgrade-prepare-step").hidden = !installable;
    view.$("reinstall").disabled = view.locks.has("setup") || upgrading || !!available;
    view.$("upgrade-begin").disabled = view.locks.has("setup") || !available || upgrading;
    view.$("upgrade-prepare").disabled = view.locks.has("setup") || !available || !upgrading;
    view.$("upgrade-abandon").hidden = backend.version !== "1.1.0" || !upgrading;
    view.$("upgrade-cancel").disabled = view.locks.has("setup") || backend.version !== "1.1.0" || !upgrading;
    view.$("upgrade-verify").disabled = view.locks.has("setup") || !backend.pending;
    view.$("upgrade-resume").hidden = !backend.resume_available;
    view.$("upgrade-resume").disabled = view.locks.has("setup") || backend.version !== backend.target || !backend.resume_available;
    for (const id of ["save-settings", "add-account", "save-account", "remove-account", "prepare-uninstall"]) view.$(id).disabled = upgrading || view.locks.has("setup");
    view.updateProviderHelp();
  }

  function versionCheckIdentity() {
    return JSON.stringify([view.data?.config.connection_revision, view.$("api-url").value,
      view.document.querySelector('[name="connection-mode"]:checked')?.value, view.data?.installation_api_url]);
  }

  async function checkServiceVersion() {
    const revision = ++view.versionCheckRevision;
    const identity = view.versionCheckIdentity();
    view.versionCheck = { identity, message: "正在检查 Hindsight 版本…" };
    view.renderServiceVersion();
    try {
      const request = view.managedMode() && view.managedSetup?.installation_id
        ? view.bridge.apiPost("managed/version", { mode: "managed", installation_id: view.managedSetup.installation_id })
        : view.bridge.apiGet("service/status");
      const backend = await view.withTimeout(request, 10000, "检查版本超时，请重试。");
      if (revision !== view.versionCheckRevision || identity !== view.versionCheckIdentity()) return;
      let message = backend.message || "";
      if (!message && !backend.version) message = "版本尚未确认，请先完成服务连接后重试。";
      if (!message && backend.version) {
        if (!backend.managed) message = `检查完成：Hindsight ${backend.version}${backend.supported === false ? "，插件未确认兼容此版本" : ""}。`;
        else if (backend.version === backend.target) message = `已是插件支持的配套版本 ${backend.target}。`;
        else if (backend.upgrade_available) message = `检查完成：可从 ${backend.version} 升级到插件支持的配套版本 ${backend.target}。`;
        else if (backend.supported === true) {
          message = `插件兼容 Hindsight ${backend.version}。`;
          if (view.data.capabilities?.managed_installation === false) message += "此环境无法生成升级命令。";
        }
        else message = `检查完成：Hindsight ${backend.version}，插件支持的配套版本为 ${backend.target}；此版本未确认兼容，不执行降级。`;
      }
      view.versionCheck = { identity, message, error: !!backend.message || !backend.version };
      view.data.backend = backend;
    } catch (error) {
      if (revision !== view.versionCheckRevision || identity !== view.versionCheckIdentity()) return;
      view.versionCheck = { identity, message: view.errorMessage(error), error: true };
    } finally {
      if (revision === view.versionCheckRevision) view.renderServiceVersion();
    }
  }

  function hydrateConfig() {
    const config = view.data.config || {};
    view.settingsRevision = view.data.config_revision;
    view.$("api-url").value = config.api_url || "";
    view.$("api-key").value = "";
    view.$("control-plane-url").value = config.control_plane_url || "";
    view.$("connection-result").hidden = true;
    view.$("api-key").placeholder = config.has_api_key ? "留空沿用已保存的密钥" : "填写服务 API 密钥";
    const installable = view.data.capabilities?.managed_installation !== false;
    const mode = installable && (config.managed || !config.api_url) ? "managed" : "existing";
    view.document.querySelector(`[name="connection-mode"][value="${mode}"]`).checked = true;
    view.managedSetup = config.managed ? { installation_id: view.data.installation_id, api_url: view.data.installation_api_url,
      providers: view.data.providers, has_installation: view.data.has_installation } : null;
    view.updateConnectionMode();
    for (const kind of ["chat", "embedding", "rerank"]) view.providerSelect(kind, config[`${kind}_provider_id`]);
    view.refreshAccountPlatforms();
    view.updateProviderHelp();
  }

  function renderParameters({ discard = false } = {}) {
    const snapshot = view.data?.parameters;
    if (!snapshot) return;
    if (discard || !view.parameterDraft || !view.parameterDraft.dirty) {
      view.parameterDraft = { revision: view.data.config_revision, dirty: false };
      for (const key of view.parameterKeys) {
        view.$("memory-" + key).value = key === "command_prefixes"
          ? (Array.isArray(snapshot.values[key]) ? snapshot.values[key].join(", ") : "") : String(snapshot.values[key] ?? "");
      }
    }
    view.$("parameters-fields").disabled = view.locks.has("parameters");
    view.$("parameters-conflict").hidden = view.parameterDraft.revision === view.data.config_revision;
    view.$("save-parameters").disabled = view.locks.has("parameters") || !view.parameterDraft.dirty
      || view.parameterDraft.revision !== view.data.config_revision || !view.$("parameters-form").checkValidity();
    const labels = { service_id: "记忆服务识别码", agent_id: "记忆归属识别码",
      service_address_revision: "服务地址变更序号", control_state_dir: "控制目录" };
    view.$("deployment-values").replaceChildren(...Object.entries(snapshot.deployment).flatMap(([key, value]) =>
      [view.el("dt", "", labels[key] || key), view.el("dd", "", value || (key === "control_state_dir"
        ? "data/plugin_data/astrbot_plugin_hindsight_bridge_control" : "未设置"))]));
    view.renderParameterLimits();
  }

  function renderParameterLimits() {
    const page = Number(view.$("memory-background_max_tokens").value);
    const shared = Number(view.$("memory-recall_max_tokens").value);
    const seconds = Number(view.$("memory-background_timeout").value);
    const total = Number(view.$("memory-recall_timeout").value);
    const messages = [];
    if (page > shared) messages.push("知识页长度超过记忆参考长度。");
    if (seconds > total / 4) messages.push(`知识页有效超时 ≤ ${total / 4} 秒。`);
    view.$("parameters-limits").textContent = messages.join(" ");
    view.$("parameters-limits").hidden = !messages.length;
  }

  function parameterPayload() {
    const parameters = {};
    for (const key of view.parameterKeys) {
      const raw = view.$("memory-" + key).value;
      if (key === "command_prefixes") parameters[key] = raw.split(/[,，\r\n]+/).map((prefix) => prefix.trim()).filter(Boolean);
      else if (key === "query_tokenizer") parameters[key] = raw.trim();
      else {
        const value = Number(raw);
        if (!raw.trim() || !Number.isFinite(value) || (!key.endsWith("timeout") && !Number.isSafeInteger(value))) {
          throw new Error("记忆参数须填写有效数值；数量和长度须为整数。");
        }
        parameters[key] = value;
      }
    }
    return { config_revision: view.parameterDraft.revision, parameters };
  }

  async function saveParameters() {
    if (!view.parameterDraft || !view.$("parameters-form").reportValidity()) return;
    const payload = view.parameterPayload();
    await view.perform(view.$("save-parameters"), "parameters", async () => {
      const response = await view.withTimeout(view.bridge.apiPost("parameters/save", payload), 25000,
        "保存超时，请刷新后确认结果。");
      if (!response.saved || !response.parameters || !response.config_revision) throw new Error(response.message || "记忆设置未保存，请重试。");
      if (view.data.config_revision !== payload.config_revision && view.data.config_revision !== response.config_revision) {
        await view.refreshBootstrap({ automatic: true, pollMaintenance: false });
        throw new Error("设置已更新，请刷新页面。");
      }
      if (view.settingsRevision === payload.config_revision) view.settingsRevision = response.config_revision;
      view.data.parameters = response.parameters;
      view.data.config_revision = response.config_revision;
      view.renderParameters({ discard: true });
      if (!view.backgroundDraft?.dirty) view.backgroundDraft = null;
      view.notify("已保存", false, "parameters-feedback");
      await view.refreshBootstrap({ automatic: true, pollMaintenance: false });
    }, "parameters-feedback");
  }

  function connectionPayload() {
    const apiUrl = view.$("api-url").value.trim();
    let parsed;
    try { parsed = new URL(apiUrl); } catch { throw new Error("请填写完整的 API 地址，例如 http://hindsight:8888。"); }
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("API 地址应使用 http 或 https，密钥请填写在 API 密钥中。");
    return { api_url: apiUrl, api_key: view.$("api-key").value.trim() };
  }

  function settingsPayload() {
    return { ...view.connectionPayload(), mode: view.managedMode() ? "managed" : "existing",
      control_plane_url: view.controlPlaneUrl(view.$("control-plane-url").value), config_revision: view.settingsRevision };
  }

  async function checkConnection(payload, target = "connection-result") {
    const response = await view.bridge.apiPost("connection/check", payload);
    if (response.ok !== true) throw new Error(response.message || "连接未成功，请检查 API 地址和密钥。");
    view.notify(response.message || "服务连接成功。", false, target);
    return response;
  }

  function watchModels(requestId = "", managed = false) {
    if (view.modelWatch) return view.modelWatch;
    view.bootstrapReadRevision++;
    view.locks.add("setup");
    view.$("setup-fields").disabled = true;
    view.$("model-progress").hidden = false;
    view.modelWatch = (async () => {
      let failures = 0;
      while (true) {
        let job;
        try {
          job = await view.withTimeout(managed
            ? view.bridge.apiPost("managed/status", { mode: "managed", installation_id: view.managedSetup?.installation_id })
            : view.bridge.apiGet("models/status"), 10000, "获取接入进度超时。");
          failures = 0;
        } catch {
          if (managed && ++failures >= 3) throw new Error("无法获取接入进度，请重新打开页面核对结果后重试。");
          view.$("model-progress").textContent = "正在重新获取进度…";
          await view.delay(5000);
          continue;
        }
        if (requestId && job.request_id !== requestId) {
          view.$("model-progress").textContent = "请求未确认，请点击「保存并连接」重试。";
          if (job.status === "running") {
            requestId = job.request_id;
            continue;
          }
          return;
        }
        view.$("model-progress").textContent = job.message || "正在处理模型配置…";
        if (job.status === "running") {
          await view.delay(2000);
          continue;
        }
        if (job.status === "succeeded") {
          view.settingsRevision = job.config_revision;
          view.$("model-progress").textContent = job.message || "模型已验证并保存，服务已启动。";
          await view.refreshBootstrap({ hydrate: managed });
          view.notify(job.message || "模型已验证并保存，服务已启动。");
        } else {
          if (managed && job.config_revision) view.settingsRevision = job.config_revision;
          view.$("model-progress").textContent = job.status === "idle"
            ? "接入检查已中断，请重新保存并连接。"
            : `${job.message || "模型验证或保存失败。"} 请修正问题后重试。`;
          await view.refreshBootstrap({ automatic: true });
        }
        return;
      }
    })().catch((error) => {
      view.$("model-progress").textContent = view.errorMessage(error);
    }).finally(() => {
      view.modelWatch = null;
      view.modelRequestId = "";
      view.locks.delete("setup");
      view.$("setup-fields").disabled = !view.data;
      view.renderServiceVersion();
    });
    return view.modelWatch;
  }

  async function saveSettings() {
    if (!view.$("settings-form").reportValidity()) return;
    const payload = view.settingsPayload();
    await view.perform(view.$("save-settings"), "setup", async () => {
      const response = await view.bridge.apiPost("settings/save", payload);
      if (response.saved !== true) throw new Error(response.message || "连接未保存，请重试。");
      view.settingsRevision = response.config_revision;
      view.$("api-key").value = "";
      view.clearCommands();
      await view.refreshBootstrap({ hydrate: true });
      view.notify("连接与管理地址已保存。");
    });
  }

  async function saveAccess() {
    if (!view.$("settings-form").reportValidity()) return;
    await view.perform(view.$("save-access"), "setup", async () => {
      const response = await view.bridge.apiPost("access/save", { config_revision: view.settingsRevision,
        control_plane_url: view.controlPlaneUrl(view.$("control-plane-url").value) });
      if (!response.saved) throw new Error(response.message || "官方管理地址未保存，请重试。");
      view.settingsRevision = response.config_revision;
      view.notify(response.message, false, "connection-result");
      await view.refreshBootstrap({ automatic: true });
    });
  }

  async function prepareInstallation(button, redeploy = false) {
    await view.perform(button, "setup", async () => {
      const response = await view.bridge.apiPost("install/prepare", {
        config_revision: view.settingsRevision, repair: redeploy && !view.data?.backend?.version,
        mode: "managed",
        control_plane_url: view.controlPlaneUrl(view.$("control-plane-url").value),
      });
      if (!response.command) throw new Error("未生成部署命令，请稍后重试。");
      if (response.config_revision) view.settingsRevision = response.config_revision;
      view.$("install-command").value = response.command;
      view.$("installation").hidden = false;
      view.data.has_installation = true;
      view.data.installation_api_url = response.api_url;
      view.managedSetup = { ...view.managedSetup, installation_id: response.installation_id, has_installation: true,
        api_url: response.api_url, providers: view.managedSetup?.providers || view.data.providers };
      if (response.api_url) view.$("api-url").value = response.api_url;
      if (response.control_plane_url && !view.$("control-plane-url").value.trim()) view.$("control-plane-url").value = view.controlPlaneUrl(response.control_plane_url);
      view.updateConnectionMode();
      view.$("generate-deployment").hidden = false;
      view.$("generate-deployment").textContent = "重新生成命令";
      view.$("deployment-dialog").dataset.redeploy = String(redeploy);
      view.$("check-deployment").hidden = !redeploy;
      view.$("deployment-heading").textContent = redeploy ? "重新部署" : "安装配套服务";
      view.$("deployment-description").textContent = redeploy
        ? "在原服务器、原安装目录执行。"
        : "在 AstrBot 所在主机执行，再选择模型连接。";
      view.$("deployment-feedback").hidden = true;
      if (!view.$("deployment-dialog").open) view.$("deployment-dialog").showModal();
    }, "deployment-feedback");
  }

  async function copyCommand(field, container, message) {
    if (!view.$(field).value) throw new Error("请先生成命令。");
    view.$(field).focus();
    view.$(field).select();
    try {
      if (!view.navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await view.navigator.clipboard.writeText(view.$(field).value);
    } catch {
      if (!view.document.execCommand("copy")) throw new Error("浏览器限制了自动复制。命令已选中，请按 Ctrl+C 或长按复制。");
    }
    view.$(field).value = "";
    view.$(container).hidden = true;
    view.notify(message);
  }

  async function reloadSettings() {
    view.clearCommands();
    await view.refreshBootstrap({ hydrate: true, pollMaintenance: false });
    view.notify("设置已重新加载。");
  }

  return { providerSelect, updateProviderHelp, clearUninstall, managedMode, loadManagedSetup, updateConnectionMode, renderServiceVersion, versionCheckIdentity, checkServiceVersion, hydrateConfig, renderParameters, renderParameterLimits, parameterPayload, saveParameters, connectionPayload, settingsPayload, checkConnection, watchModels, saveSettings, saveAccess, prepareInstallation, copyCommand, reloadSettings };
}
