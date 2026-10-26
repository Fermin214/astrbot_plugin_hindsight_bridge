"""Panel configuration invariants without AstrBot or model services."""

import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import panel as module
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightError


class Config(dict):
    def save_config(self):
        pass


class StatePath:
    """Expose setup.json reads while keeping all persisted state in memory."""

    def __init__(self, panel):
        self.panel = panel

    def __truediv__(self, name):
        return self

    def exists(self):
        return self.panel.saved_state is not None

    def read_text(self, **kwargs):
        return json.dumps(self.panel.saved_state)


class InMemoryPanel(module.MemoryPanel):
    def directory(self):
        return StatePath(self)

    def store_state(self, value):
        self.saved_state = copy.deepcopy(value)


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = Config()
        self.config = Config(
            enabled=True, api_url="http://old-service:8888", api_key="old-secret",
            service_id="old-service-id", service_address_revision=4,
            timezone="UTC", allowed_accounts=[
                {"platform_instance_id": "qq", "user_id": "123"}
            ],
        )
        context = SimpleNamespace(get_config=lambda umo=None: self.profile if umo else {
            "platform": [{"id": "qq", "type": "aiocqhttp"}],
        })
        self.plugin = SimpleNamespace(
            context=context, config=self.config, client=None, outbox=None,
            stopping=False, status="已停用", confirmations={},
            refresh_permissions=lambda: None,
        )

        async def close_resources():
            self.plugin.client = None
            self.plugin.outbox = None

        async def initialize():
            if self.config["enabled"]:
                self.plugin.outbox = SimpleNamespace(
                    recovering=False, list_scopes=lambda: [],
                )

        self.plugin._close_resources = AsyncMock(side_effect=close_resources)
        self.plugin.initialize = AsyncMock(side_effect=initialize)
        self.panel = InMemoryPanel(self.plugin)
        self.panel.saved_state = None
        self.panel.providers = lambda: SimpleNamespace(
            options=lambda: {"chat": [], "embedding": [], "rerank": []},
            resolve_checked=AsyncMock(return_value={}),
        )
        self.markers = {}
        self.auth_calls = []
        self.marker_keys = []
        self.marker_queries = []
        self.bank_pages = None
        self.auth_gate = None
        owner = self

        class Client:
            bank_path = staticmethod(module.HindsightClient.bank_path)

            def __init__(self, url, key):
                self.url = str(module.httpx.URL(url)).rstrip("/")
                self.key = key

            async def ensure_auth(self, **kwargs):
                owner.auth_calls.append((self.url, self.key))
                if owner.auth_gate:
                    await owner.auth_gate(self)

            async def request(self, method, path):
                owner.marker_keys.append(self.key)
                owner.assertEqual(method, "GET")
                parsed = urlsplit(path)
                owner.assertEqual(parsed.path, "/v1/default/banks")
                query = parse_qs(parsed.query)
                bank = "astrbot-setup-" + owner.panel.state()["id"]
                owner.assertEqual(query["q"], [bank])
                owner.assertEqual(query["limit"], ["100"])
                offset = int(query["offset"][0])
                owner.marker_queries.append((query["q"][0], offset))
                if owner.bank_pages is not None:
                    return owner.bank_pages(offset, bank)
                marker = owner.markers.get(self.url)
                if isinstance(marker, Exception):
                    raise marker
                if marker is None:
                    return {"banks": [], "total": 0}
                return {"banks": [{"bank_id": bank, "name": marker}], "total": 1}

            async def close(self):
                pass

        self.client_patch = patch.object(module, "HindsightClient", Client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.payload = {
            "enabled": True, "api_url": self.config["api_url"], "api_key": "",
            "timezone": "UTC", "allowed_accounts": copy.deepcopy(self.config["allowed_accounts"]),
        }

    async def save(self, **updates):
        self.panel.body = AsyncMock(return_value={**self.payload, **updates})
        return await self.panel.save()

    async def test_old_revision_is_rejected_before_credentials_or_network(self):
        snapshot = await self.panel.bootstrap()
        revision = snapshot["config_revision"]
        self.assertNotIn("config_revision", snapshot["config"])
        response = await self.save(config_revision=revision, timezone="Asia/Shanghai")
        self.assertEqual(response["config_revision"], self.panel.config_revision())
        self.assertNotEqual(response["config_revision"], revision)
        calls = len(self.auth_calls)
        with self.assertRaisesRegex(module.PanelError, "配置已被更新"):
            await self.save(config_revision=revision)
        self.assertEqual(len(self.auth_calls), calls)
        self.assertEqual(self.config["timezone"], "Asia/Shanghai")

    async def test_new_url_gets_independent_service_id_and_switch_back_restores_id(self):
        await self.save(api_url="http://new-service:8888/", api_key="new-secret")
        new_id = self.config["service_id"]
        self.assertNotEqual(new_id, "old-service-id")
        self.assertEqual(
            self.panel.state()["connections"],
            {"http://old-service:8888": "old-service-id", "http://new-service:8888": new_id},
        )
        self.assertFalse((await self.panel.bootstrap())["config"]["managed"])
        await self.save(api_key="old-secret")
        self.assertEqual(self.config["service_id"], "old-service-id")
        await self.save(api_url="http://new-service:8888", api_key="new-secret")
        self.assertEqual(self.config["service_id"], new_id)
        self.assertEqual(self.config["service_address_revision"], 4)
        state = self.panel.state(create=True)
        self.assertEqual(state["connections"]["http://new-service:8888"], new_id)
        self.assertEqual(len(state["id"]), 12)
        self.assertTrue(all(state.get(key) for key in (
            "database_password", "api_key", "panel_key",
        )))

    async def test_save_lock_covers_inherited_credentials_and_network_check(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(client):
            if len(self.auth_calls) == 1:
                entered.set()
                await release.wait()

        self.auth_gate = gate
        payloads = {
            "first": {**self.payload, "api_key": "rotated-secret"},
            "second": dict(self.payload),
        }

        async def body():
            return payloads[asyncio.current_task().get_name()]

        self.panel.body = body
        first = asyncio.create_task(self.panel.save(), name="first")
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(self.panel.save(), name="second")
            await asyncio.sleep(0)
            self.assertEqual(len(self.auth_calls), 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
            self.assertEqual(self.auth_calls[1][1], "rotated-secret")
            self.assertEqual(self.config["api_key"], "rotated-secret")
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )

    async def test_candidate_can_change_until_deployed_marker_locks_embedding(self):
        fingerprints = ["a" * 64, "b" * 64]
        install_url = "http://managed-service:8888"

        def installation(root, state, env, **kwargs):
            return {
                "api_url": install_url, "command": "installation-command",
                "embedding_fingerprint": fingerprints.pop(0),
            }

        self.panel.body = AsyncMock(return_value={
            "chat_provider_id": "chat", "embedding_provider_id": "embed",
            "rerank_provider_id": "",
        })
        with patch.object(module, "installation", side_effect=installation):
            first = await self.panel.prepare()
            second = await self.panel.prepare()
        self.assertNotEqual(first["embedding_fingerprint"], second["embedding_fingerprint"])
        self.assertNotIn("embedding_identity", self.panel.state())
        self.assertEqual(self.panel.state()["candidate_fingerprint"], "b" * 64)
        self.assertEqual(second["config_revision"], self.panel.config_revision())
        self.markers[install_url] = "astrbot-setup-v1:" + "b" * 64
        before = self.panel.state()
        with patch.object(module, "installation", return_value={
            "api_url": install_url, "embedding_fingerprint": "c" * 64,
        }):
            with self.assertRaisesRegex(module.PanelError, "已部署服务.*不一致"):
                await self.panel.prepare()
        self.assertEqual(self.panel.state(), before)

    async def test_prepare_rejects_stale_revision_and_strips_valid_revision(self):
        catalog = self.panel.providers()
        self.panel.providers = lambda: catalog
        selection = {
            "chat_provider_id": "chat", "embedding_provider_id": "embed",
            "rerank_provider_id": "",
        }
        revision = self.panel.config_revision()
        submitted = {**selection, "config_revision": revision}
        self.panel.body = AsyncMock(return_value=submitted)
        with patch.object(module, "installation", return_value={
            "api_url": "http://managed-service:8888", "embedding_fingerprint": "b" * 64,
        }) as install:
            response = await self.panel.prepare()
            catalog.resolve_checked.assert_awaited_once_with(selection)
            self.assertEqual(submitted["config_revision"], revision)
            self.assertEqual(response["config_revision"], self.panel.config_revision())
            state = self.panel.state()
            with self.assertRaisesRegex(module.PanelError, "配置已被更新"):
                await self.panel.prepare()
            self.assertEqual(self.panel.state(), state)
            self.assertEqual(catalog.resolve_checked.await_count, 1)
            self.assertEqual(install.call_count, 1)

    async def test_managed_tracks_active_url_rather_than_candidate_existence(self):
        state = self.panel.state(create=True)
        state["api_url"] = "http://managed-service:8888"
        self.panel.store_state(state)
        self.assertFalse((await self.panel.bootstrap())["config"]["managed"])
        for url in ("", state["api_url"], state["api_url"] + "/"):
            self.config["api_url"] = url
            with self.subTest(url=url):
                self.assertTrue((await self.panel.bootstrap())["config"]["managed"])

    async def test_enable_requires_marker_matching_latest_candidate(self):
        state = self.panel.state(create=True)
        state.update(api_url="http://managed-service:8888", candidate_fingerprint="b" * 64)
        self.panel.store_state(state)
        before = dict(self.config)
        for marker in (
            None, HindsightError("connection_error"), "astrbot-setup-v1:" + "a" * 64,
        ):
            self.markers[state["api_url"]] = marker
            with self.subTest(marker_type=type(marker).__name__):
                with self.assertRaisesRegex(module.PanelError, "执行最新安装命令") as caught:
                    await self.save(api_url=state["api_url"], api_key="selected-secret")
                self.assertEqual(dict(self.config), before)
                self.assertNotIn("selected-secret", str(caught.exception))
        self.markers[state["api_url"]] = "astrbot-setup-v1:" + "b" * 64
        response = await self.save(api_url=state["api_url"], api_key="selected-secret")
        self.assertTrue(response["saved"])
        self.assertEqual(self.marker_keys[-1], "selected-secret")
        self.assertNotEqual(self.config["service_id"], "old-service-id")

    async def test_marker_search_is_filtered_paginated_and_exact(self):
        state = self.panel.state(create=True)
        expected = "astrbot-setup-v1:" + "b" * 64

        def pages(offset, bank):
            if offset == 0:
                return {
                    "banks": [
                        {"bank_id": bank + "-other", "name": expected},
                        {"bank_id": "unrelated-private-bank", "name": "private-name"},
                    ],
                    "total": 3,
                }
            self.assertEqual(offset, 2)
            return {"banks": [{"bank_id": bank, "name": expected}], "total": 3}

        self.bank_pages = pages
        marker = await self.panel.installation_marker(state, "http://managed-service:8888")
        self.assertEqual(marker, expected)
        self.assertEqual([offset for _, offset in self.marker_queries], [0, 2])
        snapshot = await self.panel.bootstrap()
        self.assertEqual(len(self.marker_queries), 2)
        for private_value in ("unrelated-private-bank", "private-name"):
            self.assertNotIn(private_value, json.dumps(snapshot))

    async def test_marker_query_stops_at_ten_thousand_and_rejects_broken_pages(self):
        state = self.panel.state(create=True)
        self.bank_pages = lambda offset, bank: {
            "banks": [
                {"bank_id": f"unrelated-{offset + index}", "name": "private-name"}
                for index in range(100)
            ],
            "total": 10001,
        }
        with self.assertRaisesRegex(module.PanelError, "一万条"):
            await self.panel.installation_marker(state, "http://managed-service:8888")
        self.assertEqual(len(self.marker_queries), 100)
        self.assertEqual(self.marker_queries[-1][1], 9900)
        for page in (
            {"banks": [], "total": 1},
            {"banks": [{"bank_id": 123, "name": "private-name"}], "total": 1},
            {"banks": [], "total": "private-name"},
            {"config": {}, "overrides": {}},
        ):
            self.bank_pages = lambda offset, bank, page=page: page
            with self.subTest(page_keys=tuple(page)):
                self.assertIsNone(
                    await self.panel.installation_marker(state, "http://managed-service:8888")
                )


if __name__ == "__main__":
    unittest.main()
