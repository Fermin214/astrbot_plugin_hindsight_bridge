"""Chromium UI acceptance with a mock view SDK, not a native Dashboard test."""

import asyncio
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.async_api import async_playwright

OUTPUT = Path("/browser-output")
VIEW = Path("/candidate-source/views/memory")
SDK = """
window.fixtureCalls = [];
window.fixtureScope = {
  scope_id: 'one', bank: 'abm-v1-fixture-g0', platform_instance_id: 'qq', user_id: '123',
  mode: 'active', summary: {counts: {}},
  background: {revision: 0, scope_revision: 1, enabled: false, page_id: ''}
};
const fixtureData = () => ({
  config_revision: 'fixture-config',
  config: {configured: true, managed: false, api_url: 'http://fixture.test', has_api_key: true,
           allowed_accounts: [], control_plane_url: 'http://fixture.test'},
  status: {connected: true}, providers: {},
  platforms: [{id: 'qq', name: 'Fixture', supported: true, user_label: 'User ID'}],
  scopes: [window.fixtureScope]
});
window.AstrBotPluginView = {
  ready: async () => ({isDark: false}), onContext: () => () => {},
  apiGet: async (route) => {
    if (route === 'bootstrap') return structuredClone(fixtureData());
    if (route.endsWith('/background')) return {
      pages: [{page_id: 'kp-fixture', path: 'Folder / <script>not HTML</script>'}],
      selection: structuredClone(window.fixtureScope.background), scope_revision: 1, config_revision: 'fixture-config'
    };
    throw new Error('Unexpected GET');
  },
  apiPost: async (route, payload) => {
    window.fixtureCalls.push({route, payload});
    if (!route.endsWith('/background') || 'bank' in payload) throw new Error('Unsafe payload');
    if (payload.revision !== window.fixtureScope.background.revision) throw new Error('Stale selection');
    window.fixtureScope.background = {...window.fixtureScope.background, ...payload, revision: payload.revision + 1};
    return {message: 'Saved'};
  }
};
"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEW), **kwargs)

    def log_message(self, *args):
        pass


async def main(port):
    checks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for name, size in (("desktop", {"width": 1280, "height": 900}), ("phone", {"width": 390, "height": 844})):
                page = await browser.new_page(viewport=size)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script(SDK)
                await page.goto(f"http://127.0.0.1:{port}/index.html")
                await page.locator("#background-load").wait_for(state="visible")
                await page.locator("#background-load").click()
                await page.locator("#background-page").select_option("kp-fixture")
                await page.locator("#background-enabled").check()
                await page.locator("#background-save").click()
                await page.wait_for_function("window.fixtureScope.background.enabled === true")
                await page.locator("#background-close").click()
                await page.wait_for_function("window.fixtureScope.background.enabled === false")
                calls = await page.evaluate("window.fixtureCalls")
                assert len(calls) == 2 and all(call["route"] == "scopes/one/background" for call in calls)
                assert calls[-1]["payload"]["page_id"] == "kp-fixture"
                assert not errors, errors
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
                await page.screenshot(path=str(OUTPUT / ("background-" + name + ".png")), full_page=True)
                checks.append(name + "_load_select_save_close_without_js_errors_or_horizontal_overflow")
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
