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
window.fixtureBootstraps = 0;
window.fixtureRefreshGate = null;
window.fixtureScope = {
  scope_id: 'one', bank: 'abm-v1-fixture-g0', platform_instance_id: 'qq', user_id: 'ExampleUserIdentifier0123456789AB',
  mode: 'active', summary: {counts: {}},
  background: {revision: 0, scope_revision: 1, enabled: false, page_id: ''}
};
window.fixtureOther = {
  ...window.fixtureScope, scope_id: 'two', bank: 'abm-v1-other-g0', user_id: 'OtherUser',
  background: {revision: 0, scope_revision: 1, enabled: false, page_id: ''}
};
const fixtureData = () => ({
  config_revision: 'fixture-config',
  config: {configured: true, managed: false, api_url: 'http://fixture.test', has_api_key: true,
           allowed_accounts: [window.fixtureScope, window.fixtureOther].map(
             ({platform_instance_id, user_id}) => ({platform_instance_id, user_id})), control_plane_url: 'http://fixture.test'},
  status: {connected: true}, providers: {},
  platforms: [{id: 'qq', name: 'Fixture', supported: true, user_label: 'User ID'}],
  scopes: [window.fixtureScope, window.fixtureOther]
});
window.AstrBotPluginView = {
  ready: async () => ({isDark: false}), onContext: () => () => {},
  apiGet: async (route) => {
    if (route === 'bootstrap') {
      window.fixtureBootstraps++;
      return structuredClone(fixtureData());
    }
    if (route.endsWith('/background')) {
      const primary = route === 'scopes/one/background';
      const scope = primary ? window.fixtureScope : window.fixtureOther;
      const response = {
        pages: [{page_id: primary ? 'kp-fixture' : 'kp-other', path: primary ? 'Folder / <script>not HTML</script>' : 'Other user'}],
        selection: structuredClone(scope.background), scope_revision: scope.background.scope_revision,
        config_revision: 'fixture-config'
      };
      if (primary && window.fixtureRefreshGate) {
        response.pages.push({page_id: 'kp-late', path: 'Stale page list'});
        await new Promise(resolve => { window.fixtureReleaseRefresh = resolve; });
      }
      return response;
    }
    throw new Error('Unexpected GET');
  },
  apiPost: async (route, payload) => {
    window.fixtureCalls.push({route, payload});
    if (!route.endsWith('/background') || 'bank' in payload) throw new Error('Unsafe payload');
    if (payload.revision !== window.fixtureScope.background.revision ||
        payload.scope_revision !== window.fixtureScope.background.scope_revision ||
        payload.config_revision !== 'fixture-config') throw new Error('Stale selection');
    const pages = payload.page_ids.map(page_id => ({page_id, mental_model_id: 'mm-fixture'}));
    window.fixtureScope.background = {...window.fixtureScope.background, ...payload, pages,
      page_id: payload.page_ids[0] || '', revision: payload.revision + 1};
    return {selection: structuredClone(window.fixtureScope.background), message: 'Saved'};
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


async def main(port):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for name, size, sandboxed in (
                ("desktop", {"width": 1280, "height": 900}, False),
                ("phone", {"width": 390, "height": 844}, False),
                ("desktop-sandbox", {"width": 1280, "height": 900}, True),
                ("phone-sandbox", {"width": 390, "height": 844}, True),
            ):
                page = await browser.new_page(viewport=size)
                await page.clock.install()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
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
                assert not errors, errors
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
                await page.screenshot(path=str(OUTPUT / ("background-" + name + ".png")), full_page=True)
                checks.append(name + "_knowledge_save_polling_draft_and_late_response")
                await page.close()
        finally:
            await browser.close()
    (OUTPUT / "report.json").write_text(json.dumps({
        "browser": "Chromium", "sdk_and_backend": "mock", "real_astrbot_dashboard": False,
        "assets_sha256": {name: hashlib.sha256((VIEW / name).read_bytes()).hexdigest()
                          for name in ("index.html", "app.js", "style.css")},
        "passed": checks, "complete": True,
    }, indent=2))


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    asyncio.run(main(server.server_port))
finally:
    server.shutdown()
    server.server_close()
    worker.join()
