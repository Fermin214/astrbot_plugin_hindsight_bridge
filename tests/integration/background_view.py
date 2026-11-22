"""Chromium UI acceptance with a mock view SDK, not a native Dashboard test."""

import asyncio
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from playwright.async_api import async_playwright

OUTPUT = Path(os.environ.get("BRIDGE_BROWSER_OUTPUT", "/browser-output"))
VIEW = Path(__file__).resolve().parents[2] / "views/memory"
SDK = """
window.fixtureCalls = [];
window.fixtureCopies = [];
if (navigator.clipboard) {
  const writeText = navigator.clipboard.writeText.bind(navigator.clipboard);
  navigator.clipboard.writeText = async value => {
    await writeText(value);
    window.fixtureCopies.push(value);
  };
}
const execCommand = document.execCommand.bind(document);
document.execCommand = (...commandArguments) => {
  const recordCopy = event => window.fixtureCopies.push(event.clipboardData.getData('text/plain'));
  if (commandArguments[0] === 'copy') document.addEventListener('copy', recordCopy, {once: true});
  try { return execCommand(...commandArguments); }
  finally { document.removeEventListener('copy', recordCopy); }
};
window.fixtureBootstraps = 0;
window.fixtureRefreshGate = null;
window.fixtureConfigRevision = 'fixture-config';
window.fixtureParameters = {
  values: {background_max_tokens: 400, recall_max_tokens: 1200, recall_timeout: 8, background_timeout: 1,
    recall_query_tokens: 380, query_tokenizer: 'o200k_base', queue_max_tasks: 1000,
    queue_max_mib: 64, command_prefixes: ['!', '#']},
  deployment: {service_id: 'fixture-service', agent_id: 'fixture-agent'}
};
const fixtureSelection = () => ({revision: 0, prompt: '记忆提示', types: ['world', 'experience', 'observation'],
  exclude_assistant: false, capture_assistant: true, recall_on_quoted_or_link: false,
  recall_budget: 'mid', detail_limit: 12, history_limit: 30});
window.fixtureScope = {
  scope_id: 'one', bank: 'abm-v1-fixture-g0', platform_instance_id: 'qq', user_id: 'ExampleUserIdentifier0123456789AB',
  mode: 'active', summary: {counts: {}}, revision: 1, generation: 0, owner_id: 'fixture-owner', bot_id: 'fixture-bot',
  reference_revision: 0,
  reading: {revision: 0, scope_revision: 1, mode: 'automatic'},
  reflect: {revision: 0, scope_revision: 1, enabled: false, timeout: 12},
  background: {revision: 0, scope_revision: 1, enabled: false, page_id: '', pages: []}
};
window.fixtureOther = {
  ...structuredClone(window.fixtureScope), scope_id: 'two', bank: 'abm-v1-other-g0', user_id: 'OtherUser', owner_id: 'other-owner'
};
window.fixtureReferences = {one: fixtureSelection(), two: fixtureSelection()};
window.fixturePages = {
  one: [{page_id: 'kp-fixture', mental_model_id: 'mm-fixture', path: 'Folder / <script>not HTML</script>', index_stale: false},
    {page_id: 'kp-second', mental_model_id: 'mm-second', path: 'Second page', index_stale: true}],
  two: [{page_id: 'kp-other', mental_model_id: 'mm-other', path: 'Other user', index_stale: false}]
};
window.fixtureUsage = {
  one: [{id: 'fixture-turn', created_at: 1795295100, conversation_id: 'internal-conversation-uuid', message_id: 'internal-message-uuid',
    summary: {pages: 2, memories: 2, tools: 3, status: 'delivered'},
    deliveries: [
      {kind: 'request', status: 'delivered', text: 'EXACT REQUEST TEXT\\r\\nThis differs from the displayed cards.',
        pages: [{page_id: 'kp-fixture', mental_model_id: 'mm-fixture', name: 'Frozen page title', is_stale: true,
          last_refreshed_at: '2026-11-21T11:04:00+00:00', content: 'Frozen page body <script>as plain text</script>',
          text: 'Legacy-compatible wrapped page text'},
          {page_id: 'kp-second', mental_model_id: 'mm-second', name: 'Second frozen title', is_stale: false,
            last_refreshed_at: '2026-11-21T10:04:00+00:00', content: 'Second frozen page body'}],
        items: [{id: 'fact-fixture', document_id: 'uw1-0-' + 'a'.repeat(64), type: 'world', source_kind: 'user',
          occurred_start: '2026-11-07T11:04:00+00:00', text: '用户习惯喝无糖冰美式'}]},
      {kind: 'recall', status: 'delivered', text: 'EXACT RECALL TOOL TEXT\\r\\nSECOND TOOL LINE', arguments: {query: 'coffee preference'},
        items: [{id: 'external-fixture', type: 'experience', source_kind: 'external', text: '早期导入记忆正文'}]},
      {kind: 'read_page', status: 'delivered', text: 'EXACT PAGE TOOL TEXT',
        arguments: {page_id: 'kp-fixture', start_line: 2, end_line: 8}},
      {kind: 'reflect', status: 'delivered', text: 'EXACT REFLECT TOOL TEXT', arguments: {query: 'compare preferences'}}
    ]},
    {id: 'legacy-turn', created_at: 1795208700, summary: {pages: 1, memories: 0, tools: 1, status: 'delivered'},
      deliveries: [{kind: 'request', status: 'delivered', text: 'EXACT LEGACY REQUEST TEXT',
        pages: [{page_id: 'legacy-page-id', text: 'Legacy saved page body'}]},
        {kind: 'recall', status: 'no_match', text: 'EXACT LEGACY TOOL TEXT'}]}],
  two: [{id: 'other-turn', created_at: 1795295200, summary: {pages: 0, memories: 1, tools: 0, status: 'delivered'},
    deliveries: [{kind: 'request', status: 'delivered', text: 'OTHER USER EXACT TEXT',
      items: [{id: 'other-fact', type: 'world', source_kind: 'user', text: 'Other user memory'}]}]}]
};
const fixtureData = () => ({
  config_revision: window.fixtureConfigRevision, parameters: window.fixtureParameters,
  config: {configured: true, managed: false, api_url: 'http://fixture.test', has_api_key: true,
           connection_revision: 1,
           allowed_accounts: [window.fixtureScope, window.fixtureOther].map(
             ({platform_instance_id, user_id}) => ({platform_instance_id, user_id})), control_plane_url: 'http://fixture.test'},
  status: {connected: true}, providers: {},
  platforms: [{id: 'qq', name: 'Fixture', supported: true, user_label: 'User ID'}],
  scopes: [window.fixtureScope, window.fixtureOther]
});
const fixtureScopeFor = route => {
  const match = route.match(/^scopes\\/(one|two)\\/(.+)$/);
  if (!match) throw new Error('Unexpected scope route: ' + route);
  return {id: match[1], kind: match[2], scope: match[1] === 'one' ? window.fixtureScope : window.fixtureOther};
};
window.AstrBotPluginView = {
  ready: async () => ({isDark: false}), onContext: () => () => {},
  apiGet: async (route) => {
    if (route === 'bootstrap') {
      window.fixtureBootstraps++;
      return structuredClone(fixtureData());
    }
    const {id, kind, scope} = fixtureScopeFor(route);
    if (kind === 'background') {
      const primary = id === 'one';
      const response = {
        pages: structuredClone(window.fixturePages[id]),
        selection: structuredClone(scope.background), scope_revision: scope.background.scope_revision,
        config_revision: window.fixtureConfigRevision
      };
      if (primary && window.fixtureRefreshGate) {
        response.pages.push({page_id: 'kp-late', path: 'Stale page list'});
        await new Promise(resolve => { window.fixtureReleaseRefresh = resolve; });
      }
      return response;
    }
    if (kind === 'references') return {selection: structuredClone(window.fixtureReferences[id]),
      scope_revision: scope.revision, config_revision: window.fixtureConfigRevision,
      prompt_tokens: window.fixtureReferences[id].prompt.length, prompt_limit: 1024};
    if (kind === 'usage') return {turns: structuredClone(window.fixtureUsage[id]).map(({deliveries, ...turn}) => turn)};
    if (kind.startsWith('usage/')) return {turn: structuredClone(window.fixtureUsage[id].find(turn => turn.id === kind.slice(6)))};
    throw new Error('Unexpected GET: ' + route);
  },
  apiPost: async (route, payload) => {
    window.fixtureCalls.push({route, payload});
    if (route.endsWith('/references/count')) return {prompt_tokens: payload.prompt.length};
    if (window.fixtureSaveError) throw new Error('Synthetic save failure');
    if ('bank' in payload) throw new Error('Unsafe payload');
    if (payload.config_revision !== window.fixtureConfigRevision) throw new Error('Stale configuration');
    if (route === 'parameters/save') {
      window.fixtureParameters.values = structuredClone(payload.parameters);
      window.fixtureConfigRevision += '-saved';
      return {saved: true, config_revision: window.fixtureConfigRevision,
        parameters: structuredClone(window.fixtureParameters)};
    }
    const {id, kind, scope} = fixtureScopeFor(route);
    if (payload.scope_revision !== scope.revision) throw new Error('Stale scope');
    if (kind === 'references') {
      if (payload.revision !== scope.reference_revision) throw new Error('Stale references');
      window.fixtureReferences[id] = {...payload, revision: payload.revision + 1};
      scope.reference_revision = payload.revision + 1;
      if (payload.history_limit === 0) window.fixtureUsage[id] = [];
      return {selection: structuredClone(window.fixtureReferences[id]), prompt_tokens: payload.prompt.length};
    }
    if (!['background', 'reading', 'reflect'].includes(kind)) throw new Error('Unexpected POST: ' + route);
    if (payload.revision !== scope[kind].revision) throw new Error('Stale selection');
    if (kind === 'background') {
      const pages = payload.page_ids.map(page_id => {
        const page = window.fixturePages[id].find(item => item.page_id === page_id);
        if (!page) throw new Error('Page does not belong to this user');
        return {page_id, mental_model_id: page.mental_model_id};
      });
      scope.background = {...scope.background, ...payload, pages,
        page_id: payload.page_ids[0] || '', revision: payload.revision + 1};
    } else scope[kind] = {...scope[kind], ...payload, revision: payload.revision + 1};
    return {selection: structuredClone(scope[kind]), message: 'Saved'};
  }
};
"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEW), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        if "sandbox=1" in self.path:
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-downloads")
        super().end_headers()


async def dirty_state(page, button_id, dirty):
    await page.wait_for_function("([id, dirty]) => document.getElementById(id).disabled === !dirty", arg=[button_id, dirty])
    card = page.locator(".card").filter(has=page.locator("#" + button_id))
    assert await card.locator(".dirty-indicator").is_visible() is dirty, button_id


async def saved_feedback(page, button_id, feedback_id, *, fade=True):
    await page.locator("#" + button_id).click()
    feedback = page.locator("#" + feedback_id)
    await feedback.filter(has_text="已保存").wait_for()
    assert await feedback.inner_text() == "已保存", feedback_id
    await dirty_state(page, button_id, False)
    if fade:
        await page.clock.run_for(2900)
        assert not await feedback.is_visible(), feedback_id


async def failed_feedback(page, button_id, feedback_id):
    await page.evaluate("window.fixtureSaveError = true")
    await page.locator("#" + button_id).click()
    feedback = page.locator("#" + feedback_id).filter(has_text="Synthetic save failure")
    await feedback.wait_for()
    await page.clock.run_for(3200)
    assert await feedback.is_visible(), feedback_id
    assert await feedback.get_attribute("role") == "alert", feedback_id
    await page.evaluate("window.fixtureSaveError = false")


async def five_cards(page, checks, name):
    await page.locator("#reading-mode").select_option("tools")
    await dirty_state(page, "reading-save", True)
    await page.locator("#reading-mode").select_option("automatic")
    await dirty_state(page, "reading-save", False)
    await page.locator("#reading-mode").select_option("tools")
    await saved_feedback(page, "reading-save", "reading-feedback")
    await page.locator("#reading-mode").select_option("automatic")
    await saved_feedback(page, "reading-save", "reading-feedback", fade=False)
    await page.locator("#reading-mode").select_option("tools")
    await failed_feedback(page, "reading-save", "reading-feedback")
    await page.locator("#reading-mode").select_option("automatic")
    await dirty_state(page, "reading-save", False)

    for field in ("reference-world", "reference-exclude", "reference-capture-assistant", "reference-quoted"):
        checked = await page.locator("#" + field).is_checked()
        await page.locator("#" + field).set_checked(not checked)
        await dirty_state(page, "reference-save", True)
        await page.locator("#" + field).set_checked(checked)
        await dirty_state(page, "reference-save", False)
    await page.locator("#reference-budget").select_option("high")
    await dirty_state(page, "reference-save", True)
    await page.locator("#reference-budget").select_option("mid")
    await dirty_state(page, "reference-save", False)
    await page.locator("#reference-limit").fill("13")
    await dirty_state(page, "reference-save", True)
    await page.locator("#reference-limit").fill("12.0")
    await dirty_state(page, "reference-save", False)
    await page.locator("#reference-prompt").fill("")
    await saved_feedback(page, "reference-save", "reference-feedback")
    assert await page.evaluate("window.fixtureReferences.one.prompt") == ""
    await page.locator("#reference-prompt").fill("自定义提示")
    await saved_feedback(page, "reference-save", "reference-feedback", fade=False)
    await page.locator("#reference-prompt").fill("新的草稿")
    await failed_feedback(page, "reference-save", "reference-feedback")
    await page.locator("#reference-prompt").fill("自定义提示")
    await dirty_state(page, "reference-save", False)

    await page.locator("#background-enabled").check()
    await dirty_state(page, "background-save", True)
    await page.locator("#background-enabled").uncheck()
    await dirty_state(page, "background-save", False)
    await page.locator("#background-page").select_option("kp-second")
    await saved_feedback(page, "background-save", "background-feedback")
    assert await page.evaluate("window.fixtureScope.background.pages.map(page => page.page_id)") == ["kp-fixture", "kp-second"]
    await page.get_by_role("button", name="移除第 2 篇知识页", exact=True).click()
    await dirty_state(page, "background-save", True)
    await page.locator("#background-page").select_option("kp-second")
    await dirty_state(page, "background-save", False)
    await page.get_by_role("button", name="上移第 2 篇知识页", exact=True).click()
    await dirty_state(page, "background-save", True)
    await page.get_by_role("button", name="下移第 1 篇知识页", exact=True).click()
    await dirty_state(page, "background-save", False)
    await page.locator("#background-enabled").uncheck()
    await saved_feedback(page, "background-save", "background-feedback", fade=False)
    await page.locator("#background-enabled").check()
    await failed_feedback(page, "background-save", "background-feedback")
    await page.locator("#background-enabled").uncheck()
    await dirty_state(page, "background-save", False)

    await page.locator("#reflect-enabled").check()
    await dirty_state(page, "reflect-save", True)
    await page.locator("#reflect-enabled").uncheck()
    await dirty_state(page, "reflect-save", False)
    await page.locator("#reflect-timeout").fill("13")
    await dirty_state(page, "reflect-save", True)
    await page.locator("#reflect-timeout").fill("12")
    await dirty_state(page, "reflect-save", False)
    await page.locator("#reflect-enabled").check()
    await saved_feedback(page, "reflect-save", "reflect-feedback")
    await page.locator("#reflect-timeout").fill("13")
    await saved_feedback(page, "reflect-save", "reflect-feedback", fade=False)
    await page.locator("#reflect-timeout").fill("14")
    await failed_feedback(page, "reflect-save", "reflect-feedback")
    await page.locator("#reflect-timeout").fill("13")
    await dirty_state(page, "reflect-save", False)

    await page.locator("#tab-setup").click()
    await page.locator("#memory-recall_max_tokens").fill("1300")
    await dirty_state(page, "save-parameters", True)
    await page.locator("#memory-recall_max_tokens").fill("01200")
    await dirty_state(page, "save-parameters", False)
    await page.locator("#memory-command_prefixes").fill(" !， # ")
    await dirty_state(page, "save-parameters", False)
    await page.locator("#memory-recall_max_tokens").fill("1201")
    await saved_feedback(page, "save-parameters", "parameters-feedback")
    await page.locator("#memory-recall_max_tokens").fill("1202")
    await saved_feedback(page, "save-parameters", "parameters-feedback", fade=False)
    await page.locator("#memory-recall_max_tokens").fill("1203")
    await failed_feedback(page, "save-parameters", "parameters-feedback")
    await page.locator("#memory-recall_max_tokens").fill("1202")
    await dirty_state(page, "save-parameters", False)
    await page.locator("#tab-memories").click()
    assert await page.locator("details").count() == 0
    checks.append(name + "_five_cards_business_values_ordered_pages_conflicts_and_feedback_timers")


async def revision_conflicts(page):
    await page.locator("#reading-mode").select_option("tools")
    await page.locator("#reference-prompt").fill("冲突中的提示草稿")
    await page.locator("#reflect-timeout").fill("14")
    await page.locator("#background-enabled").check()
    await page.locator("#tab-setup").click()
    await page.locator("#memory-recall_max_tokens").fill("1203")
    await page.evaluate("""() => {
      window.fixtureScope.reading = {...window.fixtureScope.reading, revision: window.fixtureScope.reading.revision + 1, mode: 'tools'};
      window.fixtureScope.reflect = {...window.fixtureScope.reflect, revision: window.fixtureScope.reflect.revision + 1, timeout: 20};
      window.fixtureScope.background = {...window.fixtureScope.background, revision: window.fixtureScope.background.revision + 1, enabled: true};
      window.fixtureReferences.one = {...window.fixtureReferences.one, revision: window.fixtureReferences.one.revision + 1, prompt: '另一页面保存的提示'};
      window.fixtureScope.reference_revision = window.fixtureReferences.one.revision;
      window.fixtureParameters.values.recall_max_tokens = 2400;
      window.fixtureConfigRevision += '-external';
    }""")
    bootstraps = await page.evaluate("window.fixtureBootstraps")
    await page.clock.run_for(5500)
    await page.wait_for_function("previous => window.fixtureBootstraps > previous", arg=bootstraps)
    await page.locator("#parameters-conflict").wait_for()
    await page.locator("#memory-recall_max_tokens").fill("1202")
    assert await page.locator("#memory-recall_max_tokens").input_value() == "1202"
    assert await page.locator("#parameters-conflict").is_visible()
    assert await page.locator("#save-parameters").is_disabled()
    assert not await page.locator("#parameters-form .dirty-indicator").is_visible()
    await page.locator("#memory-recall_max_tokens").fill("1203")
    assert await page.locator("#parameters-form .dirty-indicator").is_visible()
    assert await page.locator("#save-parameters").is_disabled()
    await page.locator("#memory-recall_max_tokens").fill("1202")
    await page.locator("#tab-memories").click()
    cases = [
        ("reading-mode", "reading-save", "reading-status", "automatic", "tools"),
        ("reference-prompt", "reference-save", "reference-feedback", "自定义提示", "冲突中的提示草稿"),
        ("reflect-timeout", "reflect-save", "reflect-conflict", "13", "14"),
    ]
    for field, button, feedback, original, edited in cases:
        locator = page.locator("#" + field)
        if field == "reading-mode":
            await locator.select_option(original)
        else:
            await locator.fill(original)
        assert await locator.input_value() == original
        await page.locator("#" + feedback).filter(has_text="设置已变化" if field == "reference-prompt" else "设置已更新").wait_for()
        assert await page.locator("#" + button).is_disabled()
        card = page.locator(".card").filter(has=page.locator("#" + button))
        assert not await card.locator(".dirty-indicator").is_visible()
        if field == "reading-mode":
            await locator.select_option(edited)
        else:
            await locator.fill(edited)
        assert await card.locator(".dirty-indicator").is_visible()
        assert await page.locator("#" + button).is_disabled()
        if field == "reading-mode":
            await locator.select_option(original)
        else:
            await locator.fill(original)
    await page.locator("#background-enabled").uncheck()
    assert not await page.locator("#background-enabled").is_checked()
    await page.locator("#background-status").filter(has_text="设置已更新").wait_for()
    assert await page.locator("#background-save").is_disabled()
    card = page.locator(".card").filter(has=page.locator("#background-save"))
    assert not await card.locator(".dirty-indicator").is_visible()
    await page.locator("#background-enabled").check()
    assert await card.locator(".dirty-indicator").is_visible()
    assert await page.locator("#background-save").is_disabled()
    await page.locator("#background-enabled").uncheck()
    bootstraps = await page.evaluate("window.fixtureBootstraps")
    await page.clock.run_for(5500)
    await page.wait_for_function("previous => window.fixtureBootstraps > previous", arg=bootstraps)
    assert await page.locator("#reading-mode").input_value() == "automatic"
    assert await page.locator("#reference-prompt").input_value() == "自定义提示"
    assert await page.locator("#reflect-timeout").input_value() == "13"
    assert not await page.locator("#background-enabled").is_checked()
    assert await page.locator("#reading-status").is_visible()
    assert await page.locator("#reference-feedback").is_visible()
    assert await page.locator("#reflect-conflict").is_visible()
    assert await page.locator("#background-status").is_visible()
    await page.reload()
    await page.locator('#background-page option[value="kp-fixture"]').wait_for(state="attached")
    assert await page.locator("#reading-mode").input_value() == "automatic"
    assert await page.locator("#reference-prompt").input_value() == "记忆提示"
    assert await page.locator("#reflect-timeout").input_value() == "12"
    assert not await page.locator("#reflect-conflict").is_visible()


async def usage_tabs(page, checks, name, size):
    await page.locator("#usage-open").click()
    await page.locator(".usage-card").filter(has_text="Frozen page title").wait_for()
    assert await page.locator("#usage-tab-content").get_attribute("aria-selected") == "true"
    assert await page.locator("#usage-panel-content").is_visible()
    assert not await page.locator("#usage-panel-text").is_visible()
    assert not await page.locator("#usage-panel-json").is_visible()
    labels = await page.locator("#usage-turn option").all_text_contents()
    assert "2 篇知识页" in labels[0] and "2 条记忆" in labels[0] and "3 次工具调用" in labels[0]
    assert all("uuid" not in label and "fixture-turn" not in label for label in labels)
    assert await page.locator("#usage-deliveries").get_by_text("Frozen page body <script>as plain text</script>", exact=True).count() == 1
    assert await page.locator("#usage-deliveries").get_by_text("用户习惯喝无糖冰美式", exact=True).count() == 1
    assert await page.locator("#usage-deliveries").get_by_text("早期导入记忆正文", exact=True).count() == 1
    assert "有新记忆待刷新" in await page.locator("#usage-deliveries").inner_text()
    assert "已是最新" in await page.locator("#usage-deliveries").inner_text()
    assert "早期/外部记录" in await page.locator("#usage-deliveries").inner_text()
    assert "查询：coffee preference" in await page.locator("#usage-deliveries").inner_text()
    assert "起始行：2" in await page.locator("#usage-deliveries").inner_text()
    assert "fact-fixture" not in await page.locator("#usage-deliveries").inner_text()
    await page.get_by_role("button", name="复制Frozen page title ID", exact=True).click()
    await page.locator("#usage-feedback").filter(has_text="ID 已复制").wait_for()
    assert await page.evaluate("window.fixtureCopies.at(-1)") == "kp-fixture"
    tabs = await page.locator(".usage-tabs [role=tab]").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().top)")
    assert max(tabs) - min(tabs) < 1
    width = await page.locator("#usage-dialog").evaluate("node => node.getBoundingClientRect().width")
    assert width <= min(760, size["width"] - 32) + 1, (name, width)
    if size["width"] >= 640:
        assert abs(width - 760) <= 1, (name, width)
    assert await page.locator("#usage-dialog").evaluate("node => node.scrollWidth <= node.clientWidth"), name
    await page.screenshot(path=str(OUTPUT / ("usage-content-" + name + ".png")))
    await page.locator("#usage-tab-text").click()
    assert await page.locator("#usage-panel-text").is_visible()
    expected = await page.evaluate("window.fixtureUsage.one[0].deliveries.map(delivery => delivery.text).filter(Boolean).join('\\n\\n')")
    assert await page.locator("#usage-text").input_value() == expected.replace("\r\n", "\n")
    await page.locator("#usage-copy-text").click()
    await page.locator("#usage-feedback").filter(has_text="原文已复制").wait_for()
    assert await page.evaluate("window.fixtureCopies.at(-1)") == expected
    assert await page.locator("#usage-text").evaluate("node => node.getBoundingClientRect().height >= 330")
    await page.locator("#usage-tab-text").focus()
    await page.keyboard.press("ArrowRight")
    assert await page.locator("#usage-tab-json").get_attribute("aria-selected") == "true"
    assert await page.locator("#usage-panel-json").is_visible()
    frozen_json = await page.locator("#usage-json").input_value()
    assert json.loads(frozen_json) == await page.evaluate("window.fixtureUsage.one[0]")
    assert await page.locator("#usage-copy-json").is_visible()
    await page.locator("#usage-copy-json").click()
    await page.locator("#usage-feedback").filter(has_text="JSON 已复制").wait_for()
    assert await page.evaluate("window.fixtureCopies.at(-1)") == frozen_json
    await page.locator("#usage-tab-json").focus()
    await page.keyboard.press("Home")
    assert await page.locator("#usage-tab-content").get_attribute("aria-selected") == "true"
    await page.locator('#usage-dialog [data-close="usage-dialog"]').click()
    await page.evaluate("window.fixturePages.one[0].path = 'Renamed live page'; window.fixturePages.one[0].index_stale = false")
    await page.locator("#background-recheck").click()
    await page.locator('#background-page option[value="kp-fixture"]').filter(has_text="Renamed live page").wait_for(state="attached")
    await page.locator("#usage-open").click()
    await page.locator(".usage-card").filter(has_text="Frozen page title").wait_for()
    await page.locator("#usage-tab-json").click()
    assert await page.locator("#usage-json").input_value() == frozen_json
    await page.locator("#usage-turn").select_option("legacy-turn")
    await page.wait_for_function("document.getElementById('usage-json').value.includes('EXACT LEGACY REQUEST TEXT')")
    await page.locator("#usage-tab-content").click()
    assert "Legacy saved page body" in await page.locator("#usage-deliveries").inner_text()
    assert "已是最新" not in await page.locator("#usage-deliveries").inner_text()
    assert "有新记忆待刷新" not in await page.locator("#usage-deliveries").inner_text()
    assert "查询：" not in await page.locator("#usage-deliveries").inner_text()
    await page.locator('#usage-dialog [data-close="usage-dialog"]').click()
    await page.wait_for_function("document.getElementById('usage-json').value === ''")
    await page.locator("#reference-history").fill("0")
    await page.locator("#reference-pruning").filter(has_text="关闭记录").wait_for()
    await saved_feedback(page, "reference-save", "reference-feedback")
    assert await page.evaluate("window.fixtureUsage.one.length") == 0
    await page.locator("#usage-open").click()
    await page.locator("#usage-feedback").filter(has_text="暂无使用记录").wait_for()
    assert not await page.locator("#usage-picker").is_visible()
    assert await page.locator("#usage-json").input_value() == ""
    assert await page.locator("#usage-text").input_value() == ""
    await page.locator('#usage-dialog [data-close="usage-dialog"]').click()
    checks.append(name + "_usage_three_horizontal_tabs_exact_text_json_and_frozen_legacy_snapshots")


async def main(port):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    contrasts = {}
    assets = {asset.name: hashlib.sha256(asset.read_bytes()).hexdigest()
              for asset in sorted(VIEW.iterdir()) if asset.suffix in {".html", ".js", ".css"}}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        browser_version = browser.version
        try:
            for name, size, sandboxed in (
                ("desktop", {"width": 1280, "height": 900}, False),
                ("phone", {"width": 390, "height": 844}, False),
                ("desktop-sandbox", {"width": 1280, "height": 900}, True),
                ("phone-sandbox", {"width": 390, "height": 844}, True),
            ):
                page = await browser.new_page(viewport=size, has_touch=size["width"] < 640)
                await page.clock.install()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("requestfailed", lambda request: errors.append(request.url))
                page.on("response", lambda response: errors.append(f"{response.status} {response.url}")
                        if response.status >= 400 else None)
                await page.add_init_script(SDK)
                await page.goto(f"http://127.0.0.1:{port}/index.html" + ("?sandbox=1" if sandboxed else ""))
                await page.locator('#background-page option[value="kp-fixture"]').wait_for(state="attached")
                await page.locator("#background-page").select_option("kp-fixture")
                await page.evaluate("window.bootstrapsBeforePolling = window.fixtureBootstraps")
                await page.clock.run_for(5500)
                await page.wait_for_function("window.fixtureBootstraps > window.bootstrapsBeforePolling")
                assert await page.locator("#background-page").input_value() == "kp-fixture"
                assert await page.locator("#background-save").is_enabled()
                assert await page.evaluate("window.fixtureCalls.length") == 0
                await page.locator("#background-save").click()
                await page.wait_for_function("window.fixtureScope.background.enabled === true")
                await page.locator("#background-page").wait_for(state="visible")
                await page.wait_for_function("!document.getElementById('background-page').disabled")
                await page.locator("#background-page").select_option("")
                await page.locator("#background-save").click()
                await page.wait_for_function("window.fixtureScope.background.enabled === false")
                calls = await page.evaluate("window.fixtureCalls")
                assert len(calls) == 2 and all(call["route"] == "scopes/one/background" for call in calls)
                assert [call["payload"]["revision"] for call in calls] == [0, 1]
                assert calls[-1]["payload"]["page_ids"] == ["kp-fixture"]
                assert await page.evaluate("window.fixtureScope.mode") == "active"
                assert await page.evaluate("window.fixtureCalls.length") == 2
                await five_cards(page, checks, name)
                await usage_tabs(page, checks, name, size)
                await revision_conflicts(page)
                await page.evaluate("window.fixtureRefreshGate = true")
                await page.locator("#background-recheck").click()
                await page.wait_for_function("typeof window.fixtureReleaseRefresh === 'function'")
                await page.locator("#scope-picker").select_option('["qq","OtherUser"]')
                await page.locator('#background-page option[value="kp-other"]').wait_for(state="attached")
                await page.locator("#background-page").select_option("kp-other")
                await page.evaluate("window.fixtureReleaseRefresh()")
                await page.clock.run_for(25)
                assert await page.locator('#background-page option[value="kp-late"]').count() == 0
                assert await page.locator("#background-page").input_value() == "kp-other"
                assert await page.locator("#background-save").is_enabled()
                await page.locator("#usage-open").click()
                await page.locator("#usage-deliveries").get_by_text("Other user memory", exact=True).wait_for()
                assert "Frozen page" not in await page.locator("#usage-deliveries").inner_text()
                await page.locator("#usage-tab-text").click()
                assert await page.locator("#usage-text").input_value() == "OTHER USER EXACT TEXT"
                await page.locator('#usage-dialog [data-close="usage-dialog"]').click()
                assert not errors, errors
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
                await page.screenshot(path=str(OUTPUT / ("background-" + name + ".png")), full_page=True)
                checks.append(name + "_knowledge_save_polling_draft_and_late_response")
                await page.evaluate("window.fixtureSaveError = true")
                await page.locator("#background-save").click()
                await page.locator("#background-feedback").filter(has_text="Synthetic save failure").wait_for()
                for theme in ("light", "dark"):
                    await page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                    palette = await page.evaluate("""() => {
                        const style = getComputedStyle(document.documentElement);
                        return Object.fromEntries(['text', 'bg', 'muted', 'surface-subtle', 'button-bg', 'button-hover']
                            .map(key => [key, style.getPropertyValue('--' + key).trim()]));
                    }""")
                    measured = {label: contrast(palette[foreground] if foreground != "white" else "#ffffff", palette[background])
                                for label, foreground, background in (
                                    ("text", "text", "bg"), ("muted", "muted", "surface-subtle"),
                                    ("primary", "white", "button-bg"), ("primary_hover", "white", "button-hover"),
                                )}
                    error_colors = await page.locator("#background-feedback").evaluate("""node => {
                        const hex = color => '#' + color.match(/\\d+/g).slice(0, 3)
                            .map(channel => Number(channel).toString(16).padStart(2, '0')).join('');
                        return {text: hex(getComputedStyle(node).color),
                            background: hex(getComputedStyle(node.closest('.memory-feature')).backgroundColor)};
                    }""")
                    measured["inline_error"] = contrast(error_colors["text"], error_colors["background"])
                    assert all(value >= 4.5 for value in measured.values()), (name, theme, measured)
                    contrasts[name + "-" + theme] = measured
                    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (name, theme)
                    if theme == "dark":
                        await page.screenshot(path=str(OUTPUT / ("background-" + name + "-dark.png")), full_page=True)
                checks.append(name + "_failed_save_error_text_contrast")
                await page.emulate_media(reduced_motion="reduce", forced_colors="active")
                await page.keyboard.press("Tab")
                assert await page.evaluate("getComputedStyle(document.activeElement).outlineStyle !== 'none'")
                assert await page.locator("#background-save").evaluate("node => getComputedStyle(node).transitionDuration === '0s'")
                assert not errors, errors
                checks.append(name + "_themes_contrast_focus_and_reduced_motion")
                await page.close()
        finally:
            await browser.close()
    assert assets == {asset.name: hashlib.sha256(asset.read_bytes()).hexdigest()
                      for asset in sorted(VIEW.iterdir()) if asset.suffix in {".html", ".js", ".css"}}, "View assets changed during browser acceptance"
    (OUTPUT / "report.json").write_text(json.dumps({
        "browser": "Chromium", "browser_version": browser_version,
        "sdk_and_backend": "mock", "real_astrbot_dashboard": False,
        "acceptance_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "assets_sha256": assets,
        "contrast": contrasts,
        "passed": checks, "complete": True,
    }, indent=2))


def contrast(foreground, background):
    def luminance(value):
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                  for channel in channels]
        return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    brighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    asyncio.run(main(server.server_port))
finally:
    server.shutdown()
    server.server_close()
    worker.join()
