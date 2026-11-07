"""Actual OneBot socket -> AstrBot pipeline -> Bridge -> Hindsight -> models.

Only the OneBot peer is simulated. Browser tests use the actual dashboard,
plugin iframe, SDK and authenticated extension APIs. All messages are synthetic.
"""

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
from urllib.parse import unquote, urlsplit

import aiohttp
from playwright.async_api import TimeoutError as BrowserTimeout, async_playwright

ROOT = Path("/delivery")
NAME = "astrbot_plugin_hindsight_bridge"
ALICE, BOB, OUTSIDER = 123001, 123002, 123003
BOT = 990001
SUCCESS = "Hindsight 长期记忆：插件已根据本轮对话自动检索"
EMPTY = "Hindsight 检索完成，本轮未匹配到相关记忆。"
TIMEOUT = "Hindsight 记忆检索超时，已有记忆未载入。检索与新聊天保存分开执行，不能据此判断保存失败；保存结果以插件的保存进度为准。"
FAILED = "Hindsight 连接失败，记忆暂未载入。"


def jsonl(name):
    path = ROOT / (name + ".jsonl")
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines]


def database(path, query, args=()):
    with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, args)]


def message_segments(value):
    if isinstance(value, str):
        return [{"type": "text", "data": {"text": value}}]
    if isinstance(value, list):
        return [part for item in value for part in message_segments(item)]
    if isinstance(value, dict):
        if value.get("type") == "node":
            return message_segments(value.get("data", {}).get("content", []))
        return [value]
    return []


async def until(predicate, *, timeout=120, label="condition"):
    async with asyncio.timeout(timeout):
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            await asyncio.sleep(0.25)


class OneBot:
    sequence = 1000

    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self.bot_id = settings.get("bot_id", BOT)
        self.socket = None
        self.reader = None
        self.actions = []
        self.files = []
        self.replies = asyncio.Queue()
        self.message_id = type(self).sequence

    async def connect(self):
        async def attempt():
            try:
                self.socket = await self.session.ws_connect(
                    f'http://127.0.0.1:{self.settings["onebot_port"]}/ws',
                    headers={"X-Self-ID": str(self.bot_id), "X-Client-Role": "Universal",
                             "Authorization": "Bearer " + self.settings["onebot_token"]})
                return True
            except (aiohttp.ClientError, OSError):
                return False
        await until(attempt, timeout=30, label="OneBot connection")
        self.reader = asyncio.create_task(self.read())
        await self.socket.send_json({"time": int(time.time()), "self_id": self.bot_id, "post_type": "meta_event",
                                     "meta_event_type": "lifecycle", "sub_type": "connect"})

    async def read(self):
        async for message in self.socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            body = json.loads(message.data)
            action, params = body.get("action", ""), body.get("params", {})
            self.actions.append(body)
            result = {}
            if action == "get_login_info":
                await asyncio.sleep(getattr(self, "login_info_delay", 0))
                result = {"user_id": self.bot_id, "nickname": "Delivery bot"}
            elif action == "get_status":
                result = {"online": True, "good": True}
            elif action in {"send_msg", "send_private_msg", "send_group_msg", "send_private_forward_msg", "send_group_forward_msg"}:
                result = {"message_id": len(self.actions) + 10000}
                segments = message_segments(params.get("message", params.get("messages", [])))
                for part in segments:
                    if part.get("type") != "file":
                        continue
                    uri = part.get("data", {}).get("file", "")
                    path = Path(unquote(urlsplit(uri).path) if uri.startswith("file:") else uri).resolve()
                    if not path.is_relative_to(ROOT):
                        raise RuntimeError("OneBot file fixture may only read the dedicated test directory")
                    self.files.append(json.loads(path.read_text()))
                await self.replies.put({**params, "message": segments})
            await self.socket.send_json({"status": "ok", "retcode": 0, "data": result, "echo": body.get("echo")})

    async def send(self, user, text, *, group=False, message_id=None):
        if any(text.startswith("/hindsight " + command) for command in (
            "on", "readonly", "pause", "retry", "cancel", "forget",
        )):
            # OneBot seconds cannot order a command against a boundary in the same second.
            await asyncio.sleep(1.05)
        type(self).sequence += 1
        self.message_id = type(self).sequence
        while not self.replies.empty():
            self.replies.get_nowait()
        event = {
            "time": int(time.time()), "self_id": self.bot_id, "post_type": "message", "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend", "user_id": user,
            "message_id": message_id or self.message_id, "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0, "sender": {"user_id": user, "nickname": "Synthetic user", "role": "member"},
        }
        if group:
            event["group_id"] = 880001
        await self.socket.send_json(event)
        return event["message_id"]

    async def chat(self, user, text):
        await self.send(user, text)
        async with asyncio.timeout(100):
            while True:
                reply = await self.replies.get()
                content = reply.get("message", [])
                result = content if isinstance(content, str) else "".join(
                    part.get("data", {}).get("text", "") for part in content if part.get("type") == "text")
                if result:
                    return result

    async def close(self):
        if self.socket:
            await self.socket.close()
        if self.reader:
            await self.reader


class Acceptance:
    def __init__(self):
        self.settings = json.loads((ROOT / "live-settings.json").read_text())
        self.dashboard = f'http://127.0.0.1:{self.settings["dashboard_port"]}'
        self.prefix = "/api/v1/plugins/extensions/" + NAME
        self.token = ""
        self.report = {
            "astrbot": self.settings.get("astrbot_version", "4.29.1"),
            "hindsight": self.settings["hindsight_version"],
            "hindsight_image_id": self.settings.get("hindsight_image_id"),
            "postgres": "17 + PGroonga 4.0.8 + pgvector 0.8.6",
            "chat_model": self.settings["chat_model"], "embedding_model": "qwen3.7-text-embedding-flash",
            "rerank_model": "qwen3.7-text-rerank", "message_peer": "local OneBot V11 protocol simulator, no QQ login",
            "acceptance_scope": "existing-service bridge end-to-end with real models and browser",
            "not_covered": ["real QQ account and platform delivery", "execution of managed-service installation wizard"],
            "candidate_tree_sha256": json.loads((ROOT / "package-inventory.json").read_text())["tree_sha256"],
            "passed": [], "measurements": {}, "complete": False,
        }
        self.runtime = ROOT / "verification/data/plugin_data" / NAME / "runtime.sqlite3"
        self.history = ROOT / "verification/data/data_v4.db"

    def passed(self, name):
        self.report["passed"].append(name)
        self.save()
        print("PASS " + name, flush=True)

    def save(self):
        (ROOT / "live-e2e-report.json").write_text(json.dumps(self.report, indent=2) + "\n")

    async def http(self, method, path, *, payload=None, form=None, auth=True):
        headers = {"Authorization": "Bearer " + self.token} if auth and self.token else {}
        async with self.session.request(method, self.dashboard + path, json=payload if form is None else None,
                                        data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as response:
            value = await response.json()
            if response.status >= 400 or value.get("status") == "error":
                raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {response.status}, API error")
            return value.get("data", value)

    async def control(self, action, **values):
        async with self.session.post(self.settings["gateway_url"] + "/control", json={"action": action, **values},
                                     headers={"X-Test-Token": self.settings["control_token"]}, timeout=aiohttp.ClientTimeout(total=120)) as response:
            assert response.status == 200

    async def backend_ready(self):
        async def ready():
            try:
                async with self.session.get(self.settings["hindsight_url"] + "/health", timeout=aiohttp.ClientTimeout(total=2)) as response:
                    return response.status == 200
            except (aiohttp.ClientError, TimeoutError):
                return False
        await until(ready, timeout=180)
        async with self.session.get(self.settings["hindsight_url"] + "/v1/version") as response:
            assert response.status == 200
            assert (await response.json())["api_version"] == self.settings["hindsight_version"]
        self.report["hindsight_image_id"] = self.settings.get("hindsight_image_id")

    async def install(self, filename):
        form = aiohttp.FormData()
        with (ROOT / filename).open("rb") as file:
            form.add_field("file", file, filename=filename, content_type="application/zip")
            await self.http("POST", "/api/v1/plugins/install/upload", form=form)

    async def config(self, **updates):
        value = await self.http("GET", "/api/v1/plugins/config?plugin_id=" + NAME)
        if updates:
            configuration = {**value["config"], **updates}
            await self.http("PUT", "/api/v1/plugins/config", payload={"plugin_id": NAME, "config": configuration})
            return configuration
        return value["config"]

    async def bootstrap(self):
        return await self.http("GET", self.prefix + "/bootstrap")

    async def usable(self):
        async def ready():
            value = await self.bootstrap()
            return value if value["status"]["connected"] and not value["status"]["recovering"] and all(not row["pending"] for row in value["scopes"]) else None
        return await until(ready, timeout=60)

    async def action(self, scope, action, **extra):
        return await self.http("POST", self.prefix + "/scopes/" + scope + "/action", payload={"action": action, **extra})

    async def memories(self, bank):
        async with self.session.get(self.settings["hindsight_url"] + "/v1/default/banks/" + bank + "/memories/list?limit=100",
                                    headers={"Authorization": "Bearer " + self.settings["memory_key"]}) as response:
            if response.status == 404:
                return {"items": [], "total": 0}
            assert response.status == 200
            return await response.json()

    def tasks(self):
        if not self.runtime.exists():
            return []
        return database(self.runtime, "SELECT id,state,operation_id,last_error FROM outbox")

    async def drained(self):
        settled = None
        async def check():
            nonlocal settled
            rows = self.tasks()
            failed = [row for row in rows if row["state"] == "failed"]
            if failed:
                raise RuntimeError("Retain task failed: " + str(failed[0]["last_error"]))
            terminal = len(rows) >= 2 and all(row["state"] in {"done", "discarded"} for row in rows)
            if not terminal:
                settled = None
                return False
            snapshot = (len(rows), tuple(row["id"] for row in rows))
            if settled is None or settled[0] != snapshot:
                settled = (snapshot, time.monotonic())
            return time.monotonic() - settled[1] >= 1.5
        await until(check, timeout=180)

    def requests(self):
        return [row["request"] for row in jsonl("chat-trace") if row["request"].get("max_tokens") == 512]

    def latest_wire(self):
        return json.dumps(self.requests()[-1], ensure_ascii=False)

    async def dismiss_host_notice(self, page):
        try:
            button = page.get_by_role("button", name="关闭", exact=True)
            await button.wait_for(state="visible", timeout=5000)
            await button.click()
        except BrowserTimeout:
            pass

    async def browser(self, *, failure_status=False):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_init_script("if (window.top === window) { localStorage.setItem('token', " + json.dumps(self.token) + "); localStorage.setItem('user','delivery-admin'); }")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            url = self.dashboard + "/#/plugin-view/" + NAME + "/memory"
            await page.goto(url)
            await self.dismiss_host_notice(page)
            frame = page.frame_locator("iframe")
            await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
            assert not errors, "Browser JavaScript error"
            await frame.locator("#tab-setup").click()
            await frame.locator('[name="connection-mode"][value="existing"]').check()
            assert await frame.locator("#models-settings").is_hidden()
            assert await frame.locator("#maintenance").is_hidden()
            await frame.locator("#service-version-check").click()
            await until(lambda: frame.locator("#service-version-check").is_enabled(), timeout=15)
            version = await frame.locator("#version-check-result").inner_text()
            assert "检查完成" in version and self.settings["hindsight_version"] in version
            assert "升级" not in version and "配套版本" not in version
            self.passed("existing_service_version_check_is_visible_and_read_only")
            await frame.locator("#tab-memories").click()
            if failure_status:
                assert "超时" in await frame.locator("#scope-recall-status").inner_text()
            await page.screenshot(path=str(ROOT / "desktop.png"), full_page=True)
            self.passed("real_dashboard_iframe_sdk_and_existing_service_form")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_timeout(400)
            actual = page.frames[-1]
            assert await actual.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "Mobile horizontal overflow"
            await page.screenshot(path=str(ROOT / "mobile.png"), full_page=True)
            self.passed("real_chromium_mobile_layout")

            async def blocked(route):
                await route.abort()
            await context.route("**/*bridge-sdk.js*", blocked)
            await page.reload()
            await frame.locator("#startup.error").wait_for(timeout=20000)
            assert await frame.locator("#refresh").is_enabled()
            await context.unroute("**/*bridge-sdk.js*", blocked)
            await frame.locator("#refresh").click()
            await frame.locator("#startup").wait_for(state="hidden", timeout=20000)
            self.passed("real_sdk_load_failure_and_current_iframe_reload")
            await context.close()
            await browser.close()

    async def browser_maintenance(self, scope):
        await self.action(scope, "maintenance_begin")
        gate = asyncio.Event()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_init_script("if (window.top === window) { localStorage.setItem('token', " + json.dumps(self.token) + "); localStorage.setItem('user','delivery-admin'); }")
            async def stalled(route):
                if "maintenance_status" not in route.request.url:
                    await route.continue_()
                    return
                await gate.wait()
                try:
                    await route.continue_()
                except Exception:
                    pass
            await context.route("**/scopes/*/action*", stalled)
            page = await context.new_page()
            await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
            await self.dismiss_host_notice(page)
            frame = page.frame_locator("iframe")
            try:
                await frame.locator("#startup").wait_for(state="hidden", timeout=10000)
                await frame.locator("#maintenance-view").click()
                await until(lambda: self.maintenance_failed(frame), timeout=22)
                self.passed("real_browser_first_screen_and_optional_maintenance_timeout")
            finally:
                gate.set()
                await context.close()
                await browser.close()
        await self.action(scope, "maintenance_resume")

    async def browser_authorization(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                "localStorage.setItem('token'," + json.dumps(self.token) + ");localStorage.setItem('user','delivery-admin');"
            )
            page = await context.new_page()
            await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
            await self.dismiss_host_notice(page)
            frame = page.frame_locator("iframe")
            await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
            await frame.locator("#tab-setup").click()
            await frame.get_by_role("button", name="确认账号授权", exact=True).click()
            await frame.locator("#authorization-dialog").wait_for(state="visible")
            assert str(BOT) in await frame.locator("#authorization-description").inner_text()
            snapshot = await self.bootstrap()
            await self.http("POST", self.prefix + "/accounts/authorize", payload={
                "action": "prepare", "platform_instance_id": "delivery-onebot",
                "user_id": str(ALICE), "config_revision": snapshot["config_revision"],
            })
            await frame.locator("#confirm-authorization").click()
            await frame.locator("#authorization-dialog").wait_for(state="hidden")
            assert "失效" in await frame.locator("#feedback").inner_text()
            await frame.get_by_role("button", name="确认账号授权", exact=True).click()
            await frame.locator("#authorization-dialog").wait_for(state="visible")
            await frame.locator("#confirm-authorization").click()
            await frame.locator("#authorization-dialog").wait_for(state="hidden")
            assert await page.frames[-1].evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            await page.screenshot(path=str(ROOT / "authorization-mobile.png"), full_page=True)
            await context.close()
            await browser.close()
        self.passed("real_mobile_browser_explicit_identity_authorization")
        self.passed("real_invalidated_confirmation_closes_dialog_and_new_authorization_recovers")

    async def fresh_authorization_lifecycle(self):
        await self.config(
            api_url=self.settings["memory_url"], api_key=self.settings["memory_key"],
            service_id="stage1-fresh-authorization",
            allowed_accounts=[{
                "__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(ALICE),
            }],
        )
        state = await self.bootstrap()
        assert len(state["scopes"]) == 1 and state["scopes"][0]["pending"]
        assert state["scopes"][0]["pending_reason"] == "authorization"
        before = len(self.tasks())
        await self.bot.chat(ALICE, "Before authorization I prefer synthetic red apples. Reply briefly.")
        assert len(self.tasks()) == before
        await self.browser_authorization()
        state = await self.usable()
        scope = state["scopes"][0]
        await asyncio.sleep(1.05)
        await self.bot.chat(ALICE, "The new authorized test marker is Birch-17. Reply briefly.")
        await self.drained()
        assert len(self.tasks()) > before
        first_bank = scope["bank"]
        await self.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
        after = await self.usable()
        assert after["scopes"][0]["bank"] == first_bank
        await self.http("PATCH", "/api/v1/plugins/" + NAME + "/enabled", payload={"enabled": False})
        before = len(self.tasks())
        await self.bot.chat(ALICE, "Plugin disabled marker is not memory. Reply briefly.")
        assert len(self.tasks()) == before
        await self.http("PATCH", "/api/v1/plugins/" + NAME + "/enabled", payload={"enabled": True})
        after = await self.usable()
        assert after["scopes"][0]["bank"] == first_bank
        self.passed("real_fresh_authorization_reload_disable_enable_preserve_bank_and_no_backfill")
        await self.config(allowed_accounts=[])
        historical = (await self.bootstrap())["scopes"][0]
        assert historical["historical"]
        await self.action(scope["scope_id"], "maintenance_begin", discard_pending=True)
        await self.action(scope["scope_id"], "maintenance_status")
        prepared = await self.action(scope["scope_id"], "forget_prepare")
        await self.action(scope["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])
        latest = (await self.bootstrap())["scopes"][0]
        assert latest["historical"] and latest["mode"] == "paused" and latest["bank"] != first_bank
        self.passed("real_revoked_historical_admin_maintenance_and_forget_without_reauthorization")

    async def maintenance_failed(self, frame):
        return "获取暂停进度超时" in await frame.locator("#maintenance-detail").inner_text()

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.bot = OneBot(session, self.settings)
            try:
                login = await self.http("POST", "/api/v1/auth/login", payload={"username": "astrbot", "password": self.settings["initial_password"]}, auth=False)
                self.token = login["token"]
                login = await self.http("POST", "/api/v1/auth/setup", payload={"username": self.settings["admin_name"], "password": self.settings["admin_password"],
                                                                                "confirm_password": self.settings["admin_password"]})
                self.token = login["token"]
                await self.bot.connect()
                core_path = ROOT / "verification/data/cmd_config.json"
                core_hash = hashlib.sha256(core_path.read_bytes()).hexdigest()
                await self.install("released-install.zip")
                configuration = await self.config(api_url=self.settings["memory_url"], api_key=self.settings["memory_key"], service_id="delivery-existing-service",
                                                  allowed_accounts=[{"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(user)} for user in (ALICE, BOB)])
                await self.usable()
                await self.bot.chat(ALICE, "Remember this: I prefer jasmine tea, and my notebook code is Cedar-42. Reply briefly.")
                await self.drained()
                old = await self.bootstrap()
                alice = next(row for row in old["scopes"] if row["user_id"] == str(ALICE))
                bank = alice["bank"]
                rows = await self.memories(bank)
                assert rows["total"] > 0 and "jasmine" in json.dumps(rows).lower()
                self.passed("released_plugin_actual_onebot_pipeline_and_hindsight_retain")
                await self.http("POST", self.prefix + "/settings/save", payload={
                    "config_revision": old["config_revision"], "api_url": self.settings["memory_url"], "api_key": "",
                    "allowed_accounts": [{"platform_instance_id": "delivery-onebot", "user_id": str(user)} for user in (ALICE, BOB)],
                })
                assert "__template_key" not in (await self.config())["allowed_accounts"][0]

                await self.install("candidate-upgrade.zip")
                current = await self.config()
                assert current["recall_timeout"] == configuration["recall_timeout"] == 4
                assert all(account["__template_key"] == "account" for account in current["allowed_accounts"])
                state = await self.usable()
                alice = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
                assert alice["bank"] == bank
                assert all(row["state"] == "done" for row in self.tasks())
                self.passed("real_upgrade_from_public_v0_1_0_preserves_config_bank_and_outbox")

                snapshot = await self.bootstrap()
                await self.http("POST", self.prefix + "/settings/save", payload={
                    "config_revision": snapshot["config_revision"], "api_url": self.settings["memory_url"], "api_key": "",
                    "allowed_accounts": [{"platform_instance_id": "delivery-onebot", "user_id": str(user)} for user in (ALICE, BOB)],
                })
                await self.config(recall_timeout=10)
                self.passed("real_plugin_settings_save_then_native_timeout_config_save")
                await self.usable()
                await self.bot.chat(ALICE, "/new")
                answer = await self.bot.chat(ALICE, "Which tea do I prefer, and what is my notebook code? Answer briefly.")
                if TIMEOUT in self.latest_wire():
                    initial = await self.bootstrap()
                    recall = next(row for row in initial["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]
                    assert recall["outcome"] == "error" and "timeout" in recall["code"]
                    self.report["measurements"]["natural_initial_recall_timeout_ms"] = recall["elapsed_ms"]
                    self.passed("real_initial_timeout_is_explicit_not_no_match")
                    # One bounded recovery attempt, in another fresh conversation.
                    await self.bot.chat(ALICE, "/new")
                    answer = await self.bot.chat(ALICE, "Which tea do I prefer, and what is my notebook code? Answer briefly.")
                assert "jasmine" in answer.lower() and "Cedar-42" in answer
                assert SUCCESS in self.latest_wire()
                await self.drained()
                conversations = database(self.history, "SELECT content FROM conversations")
                persisted = json.dumps(conversations, ensure_ascii=False)
                for notice in (SUCCESS, EMPTY, TIMEOUT, FAILED):
                    assert notice not in persisted
                    assert notice not in json.dumps(jsonl("retain-trace"), ensure_ascii=False)
                self.passed("actual_new_conversation_recall_reply_and_temporary_context_isolation")

                answer = await self.bot.chat(BOB, "What tea do I prefer? Answer briefly.")
                assert EMPTY in self.latest_wire() and "Cedar-42" not in self.latest_wire()
                await self.drained()
                state = await self.bootstrap()
                bob = next(row for row in state["scopes"] if row["user_id"] == str(BOB))
                assert bob["bank"] != bank
                outsider_rows = len(self.tasks())
                await self.bot.chat(OUTSIDER, "Hello. Reply with a short greeting.")
                assert "Hindsight" not in self.latest_wire()
                await asyncio.sleep(1)
                assert len(self.tasks()) == outsider_rows
                await self.bot.send(ALICE, "This group message must not be retained.", group=True)
                await asyncio.sleep(1)
                assert len(self.tasks()) == outsider_rows
                self.passed("actual_allowed_user_banks_unauthorized_user_and_group_isolation")

                await self.action(alice["scope_id"], "mode", mode="readonly")
                await self.control("delay_recall", seconds=6)
                start = time.monotonic()
                await self.bot.chat(ALICE, "Which tea do I prefer? Answer briefly.")
                state = await self.bootstrap()
                recall = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]
                assert recall["outcome"] == "success" and recall["elapsed_ms"] >= 6000
                self.report["measurements"]["delayed_recall_ms"] = recall["elapsed_ms"]
                self.passed("configured_10_second_budget_accepts_real_recall_delayed_beyond_4_seconds")

                await self.control("delay_recall", seconds=12)
                for failures in (1, 2):
                    await self.bot.chat(ALICE, "Which tea do I prefer? Answer briefly.")
                    assert TIMEOUT in self.latest_wire()
                    state = await self.bootstrap()
                    recall = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]
                    assert recall["outcome"] == "error" and recall["consecutive_failures"] == failures
                    assert 9500 <= recall["elapsed_ms"] < 12000
                await self.control("delay_recall", seconds=0)
                self.passed("real_total_deadline_agent_notice_and_consecutive_failure_visibility")
                await self.browser(failure_status=True)

                await self.config(api_key="invalid-live-test-key")
                async def rejected():
                    value = await self.bootstrap()
                    return value if value["status"]["connected"] is False else None
                rejected_state = await until(rejected, timeout=30)
                assert "密钥" in rejected_state["status"]["label"]
                await self.bot.chat(ALICE, "Which tea do I prefer? Answer briefly.")
                assert FAILED in self.latest_wire() and TIMEOUT not in self.latest_wire()
                rejected_state = await self.bootstrap()
                assert next(row for row in rejected_state["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]["code"] == "http_401"
                await self.config(api_key=self.settings["memory_key"])
                await self.usable()
                self.passed("real_initial_auth_probe_distinguishes_bad_key_from_recall_timeout")
                await self.config(service_id="")
                missing_state = await self.bootstrap()
                assert missing_state["config"]["missing_fields"] == ["service_id"]
                assert "记忆服务识别码" in missing_state["status"]["label"]
                await self.config(service_id="delivery-existing-service")
                await self.usable()
                self.passed("real_partial_configuration_names_missing_service_id")

                await self.action(alice["scope_id"], "mode", mode="active")
                await self.control("stop_hindsight")
                await self.bot.chat(ALICE, "My offline note code is Fern-73. Reply briefly.")
                assert FAILED in self.latest_wire()
                assert any(row["state"] not in {"done", "discarded"} for row in self.tasks())
                await self.control("start_hindsight")
                await self.backend_ready()
                await self.usable()
                await self.drained()
                await self.bot.chat(ALICE, "/new")
                answer = await self.bot.chat(ALICE, "What is my offline note code? Answer briefly.")
                assert "Fern-73" in answer
                recovered = await self.bootstrap()
                cold_recall = next(row for row in recovered["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]
                self.report["measurements"]["recall_after_real_service_restart_ms"] = cold_recall["elapsed_ms"]
                await self.drained()
                self.passed("real_service_stop_connection_notice_durable_capture_restart_and_drain")

                before = len(self.tasks())
                await self.bot.close()
                await self.control("restart_astrbot")
                await asyncio.sleep(4)
                self.bot = OneBot(session, self.settings)
                await self.bot.connect()
                await self.usable()
                await asyncio.sleep(2)
                assert len(self.tasks()) == before and all(row["state"] == "done" for row in self.tasks())
                self.passed("real_host_restart_preserves_queue_and_does_not_reupload_completed_tasks")

                await self.action(alice["scope_id"], "mode", mode="readonly")
                await self.control("rrf_hindsight")
                await self.backend_ready()
                await self.usable()
                await self.bot.chat(ALICE, "/new")
                answer = await self.bot.chat(ALICE, "Which tea do I prefer? Answer briefly.")
                assert "jasmine" in answer.lower() and SUCCESS in self.latest_wire()
                self.passed("real_hindsight_recall_without_optional_rerank")

                await self.browser_maintenance(alice["scope_id"])
                listing = await self.bot.chat(ALICE, "/hindsight view")
                assert "本人记忆" in listing and "记忆ID" in listing
                record = (await self.memories(bank))["items"][0]
                detail = await self.bot.chat(ALICE, "/hindsight show " + record["id"])
                assert record["id"] in detail
                exported = await self.bot.chat(ALICE, "/hindsight export")
                assert "已发送记忆 JSON 文件" in exported and self.bot.files
                artifact = self.bot.files[-1]
                assert artifact["format"] == "astrbot-hindsight-memory-export" and artifact["bank_id"] == bank
                assert artifact["total"] == len(artifact["memories"]) and artifact["documents"]
                self.passed("actual_onebot_memory_list_detail_and_bank_scoped_json_file_export")

                await self.action(alice["scope_id"], "maintenance_begin")
                async def maintenance_ready():
                    response = await self.action(alice["scope_id"], "maintenance_status")
                    return response["maintenance"]["ready"]
                await until(maintenance_ready, timeout=90)
                count = len(self.tasks())
                await self.bot.chat(ALICE, "Hello during maintenance. Reply briefly.")
                assert "Hindsight" not in self.latest_wire() and len(self.tasks()) == count
                await self.action(alice["scope_id"], "maintenance_resume")
                self.passed("real_maintenance_drain_gate_and_resume")

                # Snapshot pending business work, then retain the newer erasure
                # journal while restoring that old snapshot in the same test root.
                await self.action(alice["scope_id"], "mode", mode="active")
                await self.control("stop_hindsight")
                await self.bot.chat(ALICE, "The pending restore test marker is Maple-99. Reply briefly.")
                await asyncio.sleep(1)
                pending = [row for row in self.tasks() if row["state"] not in {"done", "discarded"}]
                assert pending
                response = await self.bot.chat(ALICE, "/hindsight cancel " + str(pending[0]["id"]))
                assert str(pending[0]["id"]) in response
                row = database(self.runtime, "SELECT cancel_intent FROM outbox WHERE id=?", (pending[0]["id"],))[0]
                assert row["cancel_intent"] == 1
                self.passed("actual_onebot_cancel_command_records_durable_cancel_intent")
                control = ROOT / "verification/data/plugin_data" / (NAME + "_control") / "control.sqlite3"
                backup = ROOT / "business-before-forget"
                process = await asyncio.create_subprocess_exec("python", "/candidate-source/scripts/backup.py", "--data-dir", str(self.runtime.parent),
                                                               "--control-dir", str(control.parent), "--output", str(backup),
                                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, _ = await process.communicate()
                assert process.returncode == 0

                prepared = await self.action(alice["scope_id"], "forget_prepare")
                await self.action(alice["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])
                await self.bot.close()
                await self.control("stop_astrbot")
                latest_control = hashlib.sha256(control.read_bytes()).hexdigest()
                manifest = json.loads((backup / "manifest.json").read_text())
                revision = int(database(control, "SELECT value FROM settings WHERE key='revision'")[0]["value"])
                assert revision > manifest["last_seen_control_revision"]
                shutil.copyfile(backup / "runtime.sqlite3", self.runtime)
                for suffix in ("-wal", "-shm"):
                    self.runtime.with_name(self.runtime.name + suffix).unlink(missing_ok=True)
                assert hashlib.sha256(control.read_bytes()).hexdigest() == latest_control
                await self.control("start_hindsight")
                await self.backend_ready()
                trace_boundary = len(jsonl("retain-trace"))
                await self.control("start_astrbot")
                await asyncio.sleep(4)
                self.bot = OneBot(session, self.settings)
                await self.bot.connect()
                await self.usable()
                async def erased():
                    state = await self.bootstrap()
                    scope = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
                    return scope if scope["bank"] != bank and not scope["summary"].get("cleanup") else None
                await until(erased, timeout=180)
                async def missing():
                    return (await self.memories(bank))["total"] == 0
                await until(missing, timeout=180)
                self.passed("real_forget_generation_rotation_and_old_bank_erasure")
                state = await self.bootstrap()
                alice = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
                assert alice["mode"] == "paused"
                retired = database(self.runtime, "SELECT state,payload FROM outbox WHERE bank=?", (bank,))
                assert retired and all(row["state"] == "discarded" and not row["payload"] for row in retired)
                assert not any(bank in row["path"] for row in jsonl("retain-trace")[trace_boundary:])
                self.passed("real_business_backup_restore_keeps_latest_journal_and_does_not_resurrect_erased_work")

                assert hashlib.sha256(core_path.read_bytes()).hexdigest() == core_hash
                await self.http("DELETE", "/api/v1/plugins/by-id?plugin_id=" + NAME, payload={"delete_config": True, "delete_data": False})
                await self.install("candidate-install.zip")
                blank = await self.config()
                assert blank["recall_timeout"] == 10
                initial = await self.bootstrap()
                assert initial["config"]["missing_fields"] == ["api_url", "api_key", "service_id"]
                self.passed("fresh_distribution_default_10_and_no_host_config_mutation")
                await self.fresh_authorization_lifecycle()
                await self.http("DELETE", "/api/v1/plugins/by-id?plugin_id=" + NAME)
                self.passed("actual_dashboard_uninstall")
                self.report["complete"] = True
                self.save()
            finally:
                await self.bot.close()


async def main():
    acceptance = Acceptance()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report["failure_type"] = type(error).__name__
        acceptance.report["failure"] = str(error)[:300]
        acceptance.save()
        if getattr(acceptance, "bot", None):
            (ROOT / "onebot-actions.json").write_text(json.dumps(acceptance.bot.actions, indent=2))
        raise


if __name__ == "__main__":
    asyncio.run(main())
