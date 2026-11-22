"""Actual OneBot socket -> AstrBot pipeline -> Bridge -> Hindsight -> models.

Only the OneBot peer is simulated. Browser tests use the actual dashboard,
plugin iframe, SDK and authenticated extension APIs. All messages are synthetic.
"""

import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
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
TIMEOUT = "记忆检索超时，历史记忆未载入（不影响新消息保存）。"
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

    async def send(self, user, text, *, group=False, message_id=None, timestamp=None, metadata=None):
        type(self).sequence += 1
        self.message_id = type(self).sequence
        while not self.replies.empty():
            self.replies.get_nowait()
        event = {
            "time": int(time.time()) if timestamp is None else timestamp,
            "self_id": self.bot_id, "post_type": "message", "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend", "user_id": user,
            "message_id": message_id or self.message_id, "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0, "sender": {"user_id": user, "nickname": "Synthetic user", "role": "member"},
        }
        if group:
            event["group_id"] = 880001
        if metadata:
            assert not event.keys() & metadata.keys()
            event.update(metadata)
        await self.socket.send_json(event)
        return event["message_id"]

    async def chat(self, user, text, *, timestamp=None, metadata=None):
        await self.send(user, text, timestamp=timestamp, metadata=metadata)
        async with asyncio.timeout(100):
            while True:
                reply = await self.replies.get()
                content = reply.get("message", [])
                result = content if isinstance(content, str) else "".join(
                    part.get("data", {}).get("text", "") for part in content if part.get("type") == "text")
                if result:
                    return result

    async def close(self):
        try:
            if self.reader:
                self.reader.cancel()
                try:
                    await self.reader
                except asyncio.CancelledError:
                    pass
        finally:
            if self.socket:
                await self.socket.close()


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
            "chat_model": self.settings["chat_model"], "embedding_model": self.settings.get("embedding_model"),
            "embedding_dimensions": self.settings.get("embedding_dimensions"),
            "rerank_model": self.settings.get("rerank_model"), "message_peer": "local OneBot V11 protocol simulator, no QQ login",
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
                raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {response.status}, {str(value.get('message') or value.get('error') or 'API error')[:500]}")
            return value.get("data", value)

    async def control(self, action, **values):
        async with self.session.post(self.settings["gateway_url"] + "/control", json={"action": action, **values},
                                     headers={"X-Test-Token": self.settings["control_token"]}, timeout=aiohttp.ClientTimeout(total=120)) as response:
            assert response.status == 200
            return await response.json()

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

    async def command_prefix(self):
        plugin = await self.http("GET", "/api/v1/plugins/by-id?plugin_id=" + NAME)
        version = plugin["version"].removeprefix("v").split(".")
        return "/hb" if tuple(int(part) for part in version[:2]) >= (0, 5) else "/hindsight"

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

    async def confirm(self, user):
        state = await self.bootstrap()
        prepared = await self.http("POST", self.prefix + "/accounts/authorize", payload={
            "action": "prepare", "platform_instance_id": "delivery-onebot", "user_id": str(user),
            "config_revision": state["config_revision"],
        })
        await self.http("POST", self.prefix + "/accounts/authorize", payload={
            "action": "confirm", "platform_instance_id": "delivery-onebot", "user_id": str(user),
            "config_revision": state["config_revision"], "confirmation": prepared["confirmation"],
        })
        await self.wait_for_authorization(user)

    async def wait_for_authorization(self, user):
        scope = next(row for row in (await self.bootstrap())["scopes"]
                     if row["user_id"] == str(user) and not row["pending"] and not row["historical"])
        configuration = await self.config()
        control_directory = Path(configuration.get("control_state_dir") or self.runtime.parent.parent / (NAME + "_control"))
        boundary = database(control_directory / "control.sqlite3", "SELECT accept_from FROM authorizations WHERE connection_id=? AND source_id=?", (configuration["service_id"], scope["scope_id"]))[0]["accept_from"]
        boundary = math.ceil(boundary)
        await until(lambda: int(time.time()) >= boundary, timeout=3, label="native platform authorization boundary")

    async def action(self, scope, action, **extra):
        return await self.http("POST", self.prefix + "/scopes/" + scope + "/action", payload={"action": action, **extra})

    async def memories(self, bank):
        async with self.session.get(self.settings["hindsight_url"] + "/v1/default/banks/" + bank + "/memories/list?limit=100",
                                    headers={"Authorization": "Bearer " + self.settings["memory_key"]}) as response:
            if response.status == 404:
                return {"items": [], "total": 0}
            assert response.status == 200
            return await response.json()

    async def observation_sources(self, scope):
        bank = scope["bank"]
        route = self.prefix + "/scopes/" + scope["scope_id"]
        source_budget = {"max_tokens": 2048, "max_tokens_per_observation": 256}

        async def native(method, suffix):
            async with self.session.request(
                method, self.settings["hindsight_url"] + "/v1/default/banks/" + bank + suffix,
                headers={"Authorization": "Bearer " + self.settings["memory_key"]},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                value = await response.json()
                assert response.status == 200, (suffix, response.status, value)
                return value

        operation = await native("POST", "/consolidate")

        async def completed():
            value = await native("GET", "/operations/" + operation["operation_id"])
            assert value["status"] != "failed", value
            return value if value["status"] == "completed" else None

        await until(completed, timeout=300, label="native observation consolidation")
        observations = await native("GET", "/memories/list?type=observation&limit=100")
        assert observations["total"] > 0 and observations["items"], "Native consolidation produced no observation sample"
        observation_ids = {item["id"] for item in observations["items"]}
        original = (await self.http("GET", route + "/references"))["selection"]

        async def settings(selection):
            stored = await self.http("GET", route + "/references")
            await self.http("POST", route + "/references", payload={
                **selection, "revision": stored["selection"]["revision"],
                "scope_revision": stored["scope_revision"], "config_revision": stored["config_revision"],
            })

        await settings({**original, "types": ["observation"], "detail_limit": 12, "exclude_assistant": False})
        try:
            traces = len(jsonl("recall-response-trace"))
            requests = len(self.requests())
            await self.bot.chat(ALICE, "Summarize my jasmine tea preference and notebook code Cedar-42 from long-term memory. Answer briefly.")
            turns = (await self.http("GET", route + "/usage"))["turns"]
            turn = (await self.http("GET", route + "/usage/" + turns[0]["id"]))["turn"]
            assert turn["message_id"] == str(self.bot.message_id), turn
            delivery = turn["deliveries"][0]
            assert delivery["kind"] == "request" and delivery["text"]
            request = self.requests()[requests]
            content = next(message["content"] for message in reversed(request["messages"]) if message["role"] == "user")
            parts = [content] if isinstance(content, str) else [part["text"] for part in content if "text" in part]
            assert delivery["text"] in parts
            items = [item for item in delivery["items"] if item["type"] == "observation"]
            assert items, "Bridge did not deliver a real observation in its persisted request snapshot"
            recalled = next(trace for trace in jsonl("recall-response-trace")[traces:] if (
                trace["path"] == "/v1/default/banks/" + bank + "/memories/recall"
                and trace["request"]["types"] == ["observation"]
            ))
            assert recalled["request"]["include"]["source_facts"] == source_budget
            sources = recalled["response"].get("source_facts") or {}
            native_items = {item["id"]: item for item in recalled["response"]["results"]}
            retained = {
                item["document_id"]: item["metadata"]["role"]
                for trace in jsonl("retain-trace") if trace["path"] == "/v1/default/banks/" + bank + "/memories"
                for item in trace["body"]["items"]
            }
            samples = []
            for item in items:
                assert item["id"] in observation_ids and item["id"] in native_items
                assert item["text"] == native_items[item["id"]]["text"]
                assert item["text"].replace("\n", "\n  ") in delivery["text"]
                if item["source_kind"] not in {"user", "assistant", "mixed"}:
                    continue
                linked = item.get("source_fact_ids") or []
                assert linked and linked == native_items[item["id"]]["source_fact_ids"]
                roles, documents = set(), []
                for identity in linked:
                    assert identity in sources, "Known observation source is absent from the actual source_facts response"
                    source = sources[identity]
                    assert source["id"] == identity and source["type"] in {"world", "experience"}
                    document = source["document_id"]
                    assert re.fullmatch(r"[au]w1-(?:0|[1-9][0-9]*)-[0-9a-f]{64}", document), source
                    role = "assistant" if document.startswith("aw1-") else "user"
                    assert retained.get(document) == role, (document, source)
                    assert (source.get("metadata") or {}).get("role", role) == role
                    roles.add(role)
                    documents.append(document)
                expected = "mixed" if len(roles) == 2 else next(iter(roles))
                assert item["source_kind"] == expected, item
                samples.append({"id": item["id"], "source_kind": expected, "source_document_ids": documents})
            assert samples, "No persisted observation has known attribution backed by real retained source facts"
            self.report["measurements"]["native_observation_sources"] = {
                "operation_id": operation["operation_id"], "native_observation_count": observations["total"],
                "source_facts_budget": source_budget, "source_facts_returned": len(sources),
                "source_facts_truncated": recalled["response"].get("source_facts_truncated"), "samples": samples,
            }
            self.passed("real_native_consolidation_observation_snapshot_attribution_matches_retained_sources_with_2048_256_budget")
        finally:
            await settings(original)
        await self.drained()

    def tasks(self):
        if not self.runtime.exists():
            return []
        return database(self.runtime, "SELECT id,state,operation_id,last_error FROM outbox")

    async def drained(self, *, excluding=()):
        settled = None
        async def check():
            nonlocal settled
            rows = [row for row in self.tasks() if row["id"] not in excluding]
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

    async def blocked_maintenance_resume(self, scope_id):
        async with self.session.post(
            self.dashboard + self.prefix + "/scopes/" + scope_id + "/action",
            json={"action": "maintenance_resume"}, headers={"Authorization": "Bearer " + self.token},
        ) as response:
            value = await response.json()
            assert response.status >= 400 or value.get("status") == "error"
            assert "还有记录未处理完" in str(value.get("message") or value.get("error"))

    async def extended_message_and_operations(self, scope):
        marker = "Willow-Extension-180"
        bank, scope_id = scope["bank"], scope["scope_id"]
        started = time.time()
        future = int(started) + 180
        maintenance = False
        await self.control("operation_override", mode="parent", bank=bank)
        try:
            await self.bot.chat(
                ALICE, "Remember the separate compatibility marker " + marker + ". Reply briefly.",
                timestamp=future, metadata={"gateway_extension_v050": {"delivery_attempt": 1}},
            )

            def retained():
                return next((row for row in jsonl("retain-trace") if (
                    row["path"] == "/v1/default/banks/" + bank + "/memories"
                    and marker in row["body"]["items"][0]["content"]
                    and row["body"]["items"][0]["document_id"].startswith("uw1-")
                    and datetime.fromisoformat(row["body"]["items"][0]["timestamp"]).timestamp() >= future
                )), None)

            submitted = await until(retained, timeout=100, label="extended future message retain")
            operation_id = submitted["body"]["operation_id"]
            await self.control("operation_override", mode="parent", bank=bank, operation_id=operation_id)
            task = next(row for row in self.tasks() if row["operation_id"] == operation_id)

            def checkpoint(mode):
                return next((row for row in reversed(jsonl("operation-extension-trace")) if (
                    row["at"] >= started and row["operation_id"] == operation_id
                    and row["mode"] == mode and row["native"]["status"] == "completed"
                    and row["path"].endswith("/operations/" + operation_id)
                )), None)

            native = await until(lambda: checkpoint("parent"), timeout=180, label="native operation completed")

            async def held(status_id):
                current = next(row for row in self.tasks() if row["id"] == task["id"])
                assert current["operation_id"] == operation_id and current["state"] != "cancelled"
                nodes = database(self.runtime, "SELECT operation_id,status FROM operations WHERE task_id=?", (task["id"],))
                return current if current["state"] == "tracking" and any(
                    row["operation_id"] == status_id and row["status"] == "native_checkpoint_v050" for row in nodes
                ) else None

            await until(lambda: held(operation_id), timeout=30)
            await self.drained(excluding={task["id"]})
            await self.action(scope_id, "maintenance_begin")
            maintenance = True
            state = (await self.action(scope_id, "maintenance_status"))["maintenance"]
            assert not state["ready"] and state["phase"] == "draining" and state["pending"] > 0
            await self.blocked_maintenance_resume(scope_id)
            self.passed("real_native_unknown_parent_status_preserves_original_tracking_and_blocks_maintenance")

            children = native["native"]["child_operations"]
            if children:
                child_id = children[0]["operation_id"]
                await self.control("operation_override", mode="child", bank=bank,
                                   operation_id=operation_id, child_id=child_id)
                await until(lambda: held(child_id), timeout=30)
                await self.blocked_maintenance_resume(scope_id)
                self.passed("real_native_unknown_child_status_preserves_original_tracking_and_blocks_maintenance")
            else:
                self.report["not_covered"].append("native unknown child status: retain operation returned no child_operations")
                self.save()

            await self.control("operation_override")
            await self.drained()

            async def ready():
                return (await self.action(scope_id, "maintenance_status"))["maintenance"]["ready"]

            await until(ready, timeout=180, label="native operations finish without injected status")
            await self.control("operation_override", mode="parent", bank=bank, operation_id=operation_id)
            state = (await self.action(scope_id, "maintenance_status"))["maintenance"]
            assert not state["ready"] and state["remote_active"] > 0 and state["pending"] == 0
            await self.blocked_maintenance_resume(scope_id)
            self.passed("real_native_unknown_remote_operation_blocks_maintenance_with_empty_local_queue")
        finally:
            await self.control("operation_override")
            if maintenance:
                await self.drained()
                await self.action(scope_id, "maintenance_resume")
                boundary = database(self.runtime, "SELECT accept_from FROM scopes WHERE scope_id=?", (scope_id,))[0]["accept_from"]
                await until(lambda: int(time.time()) >= boundary, timeout=3, label="native platform maintenance reception boundary")
        rows = await self.memories(bank)
        assert marker in json.dumps(rows)
        assert next(row for row in self.tasks() if row["id"] == task["id"])["state"] == "done"
        self.passed("real_astrbot_unknown_outer_metadata_and_180_second_future_message_reaches_hindsight")

    async def daily_auth_polls(self, baseline):
        def polls():
            rows = [row for row in jsonl("memory-http-trace") if (
                row["at"] >= baseline["at"] + 45 and row["path"] == "/v1/default/banks"
                and row["method"] == "GET" and row["status"] == 200 and row["real_key"]
            )]
            if len(rows) >= 2 and rows[-1]["at"] - rows[0]["at"] >= 55:
                return rows
            return None

        observed = await until(polls, timeout=max(1, 140 - (time.time() - baseline["at"])),
                               label="two actual 60-second health polls")
        final = await self.control("collect_hindsight_logs")
        traffic = [row for row in jsonl("memory-http-trace") if baseline["at"] <= row["at"] <= final["at"]]
        assert not any(row["status"] == 401 for row in traffic)
        assert all(row["real_key"] for row in traffic if row["path"] == "/v1/default/banks")
        assert final["unauthorized"] == baseline["unauthorized"]
        self.report["measurements"]["daily_auth"] = {
            "window_started_at": baseline["at"], "window_ended_at": final["at"],
            "actual_health_poll_at": [row["at"] for row in observed],
            "http_401_delta": 0, "native_log_401_delta": final["unauthorized"] - baseline["unauthorized"],
        }
        self.passed("real_two_60_second_health_polls_use_real_key_without_http_or_hindsight_log_401")

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
        await self.wait_for_authorization(ALICE)
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
                for user in (ALICE, BOB):
                    await self.confirm(user)
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
                saved_accounts = (await self.config())["allowed_accounts"]
                assert [{key: row[key] for key in ("platform_instance_id", "user_id")} for row in saved_accounts] == [
                    {"platform_instance_id": "delivery-onebot", "user_id": str(user)} for user in (ALICE, BOB)
                ]

                usage_route = self.prefix + "/scopes/" + alice["scope_id"] + "/usage"
                old_turns = (await self.http("GET", usage_route))["turns"]
                assert old_turns
                old_turn_id = old_turns[0]["id"]
                old_snapshot = (await self.http("GET", usage_route + "/" + old_turn_id))["turn"]
                await self.control("stop_hindsight")
                await self.bot.chat(ALICE, "Remember the upgrade backlog marker Birch-50. Reply briefly.")
                await asyncio.sleep(1)
                backlog = [row["id"] for row in self.tasks() if row["state"] not in {"done", "discarded"}]
                assert backlog
                candidate_auth_started = time.time()
                await self.install("candidate-upgrade.zip")
                await self.control("start_hindsight")
                await self.backend_ready()
                current = await self.config()
                assert current["recall_timeout"] == configuration["recall_timeout"]
                assert all(account["__template_key"] == "account" for account in current["allowed_accounts"])
                state = await self.usable()
                alice = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
                assert alice["bank"] == bank
                await self.drained()
                assert all(row["state"] == "done" for row in self.tasks())
                assert all(any(row["id"] == identity for row in self.tasks()) for identity in backlog)
                assert (await self.http("GET", usage_route + "/" + old_turn_id))["turn"] == old_snapshot
                self.passed("real_upgrade_from_public_v0_4_0_preserves_backlog_config_bank_and_frozen_usage_snapshot")

                snapshot = await self.bootstrap()
                await self.http("POST", self.prefix + "/settings/save", payload={
                    "config_revision": snapshot["config_revision"], "api_url": self.settings["memory_url"], "api_key": "",
                    "allowed_accounts": [{"platform_instance_id": "delivery-onebot", "user_id": str(user)} for user in (ALICE, BOB)],
                })
                await self.config(recall_timeout=10)
                self.passed("real_plugin_settings_save_then_native_timeout_config_save")
                await self.usable()
                daily_auth = await self.control("collect_hindsight_logs")
                initial_probes = [row for row in jsonl("memory-http-trace") if (
                    candidate_auth_started <= row["at"] <= daily_auth["at"]
                    and row["path"] == "/v1/default/banks" and not row["real_key"]
                )]
                assert len(initial_probes) == 1 and initial_probes[0]["status"] in {401, 403}
                self.report["measurements"]["candidate_initial_negative_auth_probe_count"] = len(initial_probes)
                self.passed("real_candidate_initial_connection_checks_negative_auth_once")
                await self.extended_message_and_operations(alice)
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

                await self.observation_sources(alice)
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
                await self.daily_auth_polls(daily_auth)

                invalid_auth_started = time.time()
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
                assert any(row["status"] == 401 and row["at"] >= invalid_auth_started
                           for row in jsonl("memory-http-trace"))
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
                record = (await self.memories(bank))["items"][0]
                async with self.session.get(self.settings["hindsight_url"] + "/v1/default/banks/" + bank + "/memories/" + record["id"],
                                            headers={"Authorization": "Bearer " + self.settings["memory_key"]}) as response:
                    assert response.status == 200
                    detail = await response.json()
                assert detail["id"] == record["id"] and detail["text"]
                self.passed("official_bank_scoped_memory_list_and_detail")

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
