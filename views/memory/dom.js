export function createDom(view) {
  const feedbackTimers = new Map();

  function clearFeedback(target) {
    view.clearTimeout(feedbackTimers.get(target));
    feedbackTimers.delete(target);
    view.$(target).hidden = true;
  }

  function updateDirty(draft, values, target) {
    draft.dirty = JSON.stringify(values) !== draft.baseline;
    view.clearFeedback(target);
  }

  async function withTimeout(work, milliseconds, message) {
    let timer;
    try {
      return await Promise.race([
        work,
        new Promise((_, reject) => { timer = view.setTimeout(() => reject(new Error(message)), milliseconds); }),
      ]);
    } finally {
      view.clearTimeout(timer);
    }
  }

  function text(value) {
    if (value == null) return "";
    return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  }

  function el(tag, className, content) {
    const node = view.document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = view.text(content);
    return node;
  }

  function notify(message, error = false, target = "feedback") {
    if (view.$(target).closest("dialog") && !view.$(target).closest("dialog").open) target = "feedback";
    const node = view.$(target);
    view.clearTimeout(feedbackTimers.get(target));
    feedbackTimers.delete(target);
    (target === "feedback" ? view.$("feedback-message") : node).textContent = message;
    node.hidden = false;
    node.classList.toggle("error", error);
    node.setAttribute("role", error ? "alert" : "status");
    if (!error && (target === "feedback" || message === "已保存")) {
      const timer = view.setTimeout(() => {
        if (feedbackTimers.get(target) !== timer) return;
        node.hidden = true;
        feedbackTimers.delete(target);
      }, target === "feedback" ? 6500 : 2800);
      feedbackTimers.set(target, timer);
      if (target === "feedback") view.feedbackTimer = timer;
    }
  }

  function errorMessage(error) {
    const message = error?.message || "";
    if (/timeout|timed out/i.test(message)) return "请求超时，请稍后重试。";
    return message || "操作未完成，请稍后重试。";
  }

  async function perform(button, group, work, feedbackTarget = "feedback") {
    if (view.locks.has(group)) return;
    view.bootstrapReadRevision++;
    view.maintenanceReadRevision++;
    view.backgroundReadRevision++;
    view.locks.add(group);
    const fieldset = view.$(`${group}-fields`);
    if (fieldset) fieldset.disabled = true;
    const label = button?.textContent;
    if (button) {
      button.disabled = true;
      button.textContent = `正在${label}…`;
    }
    try {
      await work();
    } catch (error) {
      view.notify(view.errorMessage(error), true, feedbackTarget);
    } finally {
      view.locks.delete(group);
      if (fieldset) fieldset.disabled = !view.data || (group === "memory" && !view.scopeId);
      if (button) {
        button.textContent = label;
        button.disabled = false;
      }
      if (group === "setup") view.renderServiceVersion();
      if (group === "memory") view.renderScope();
      if (group === "parameters") view.renderParameters();
    }
  }

  function activateTab(name, focus = false) {
    if (name !== "setup") {
      view.clearCommands();
    }
    view.document.querySelectorAll('[role="tab"][id^="tab-"]').forEach((tab) => {
      const selected = tab.id === `tab-${name}`;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      view.$(tab.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) tab.focus();
    });
  }

  function option(value, label, disabled = false) {
    const node = view.el("option", "", label);
    node.value = value;
    node.disabled = disabled;
    return node;
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

  async function copyValue(field, message, target = "feedback", value = view.$(field).value) {
    if (!value) throw new Error("没有可复制的内容。");
    view.$(field).focus();
    view.$(field).select();
    try { await view.navigator.clipboard.writeText(value); }
    catch {
      const copy = (event) => {
        event.clipboardData.setData("text/plain", value);
        event.preventDefault();
      };
      view.document.addEventListener("copy", copy);
      try {
        if (!view.document.execCommand("copy")) throw new Error("内容已选中，请按 Ctrl+C 或长按复制。");
      } finally {
        view.document.removeEventListener("copy", copy);
      }
    }
    view.notify(message, false, target);
  }

  return { withTimeout, text, el, notify, clearFeedback, updateDirty, errorMessage, perform, activateTab, option, controlPlaneUrl, copyValue };
}
