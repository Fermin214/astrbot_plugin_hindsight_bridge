"""Panel configuration invariants without AstrBot or model services."""

import asyncio
import copy
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import panel as module
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.permissions import pairs


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
        self.profile = Config(provider_settings={"datetime_system_prompt": True, "identifier": True})
        self.profile.save_config = unittest.mock.Mock()
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
            if self.config.get("api_url") and self.config.get("api_key"):
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
            "allowed_accounts": copy.deepcopy(self.config["allowed_accounts"]),
        }

    async def save(self, **updates):
        self.panel.body = AsyncMock(return_value={**self.payload, **updates})
        return await self.panel.save()

    async def test_old_revision_is_rejected_before_credentials_or_network(self):
        snapshot = await self.panel.bootstrap()
        revision = snapshot["config_revision"]
        self.assertNotIn("config_revision", snapshot["config"])
        response = await self.save(config_revision=revision, allowed_accounts=[])
        self.assertEqual(response["config_revision"], self.panel.config_revision())
        self.assertNotEqual(response["config_revision"], revision)
        calls = len(self.auth_calls)
        with self.assertRaisesRegex(module.PanelError, "配置已被更新"):
            await self.save(config_revision=revision)
        self.assertEqual(len(self.auth_calls), calls)
        self.assertNotIn("timezone", self.config)
        self.assertEqual(self.config["allowed_accounts"], [])

    async def test_saved_users_are_visible_without_a_running_service(self):
        snapshot = await self.panel.bootstrap()
        self.assertEqual(len(snapshot["scopes"]), 1)
        user = snapshot["scopes"][0]
        self.assertEqual((user["platform_instance_id"], user["user_id"]), ("qq", "123"))
        self.assertTrue(user["pending"])
        self.assertEqual(user["pending_reason"], "service")

    async def test_connected_user_can_set_mode_before_first_message(self):
        @asynccontextmanager
        async def resources():
            yield

        with tempfile.TemporaryDirectory() as temporary:
            queue = Outbox(
                Path(temporary) / "runtime", self.config["service_id"], self.config["api_url"],
                credential_fingerprint="test", control_directory=Path(temporary) / "control",
            )
            self.plugin.outbox = queue
            self.plugin.using_resources = resources
            self.plugin.refresh_permissions = lambda: queue.sync_permissions(pairs(self.config["allowed_accounts"]))
            bot = SimpleNamespace(get_login_info=AsyncMock(return_value={"user_id": 20001}))
            self.plugin.context.get_platform_inst = lambda platform: SimpleNamespace(get_client=lambda: bot)
            try:
                snapshot = await self.panel.bootstrap()
                scope = snapshot["scopes"][0]
                self.assertFalse(scope["pending"])
                self.assertEqual(scope["summary"]["counts"], {})
                self.assertEqual(queue.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
                queue.set_mode(scope["scope_id"], "paused")
                incoming = Incoming("aiocqhttp", "qq", "20001", "123", "first", 1, "hello")
                actual = queue.scope(incoming)
                self.assertEqual(actual["scope_id"], scope["scope_id"])
                self.assertEqual(actual["mode"], "paused")
                self.profile.save_config.assert_not_called()
            finally:
                queue.close()

    async def test_offline_robot_does_not_create_an_unverified_memory_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Outbox(
                Path(temporary) / "runtime", self.config["service_id"], self.config["api_url"],
                credential_fingerprint="test", control_directory=Path(temporary) / "control",
            )
            self.plugin.outbox = queue
            try:
                snapshot = await self.panel.bootstrap()
                self.assertTrue(snapshot["scopes"][0]["pending"])
                self.assertEqual(snapshot["scopes"][0]["pending_reason"], "platform")
                self.assertEqual(queue.list_scopes(), [])
            finally:
                queue.close()

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
            "database_password", "api_key", "management_key",
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

    async def test_installation_needs_no_models_and_stale_revision_is_rejected(self):
        catalog = self.panel.providers()
        self.panel.providers = lambda: catalog
        revision = self.panel.config_revision()
        self.panel.body = AsyncMock(return_value={"config_revision": revision})
        with patch.object(module, "installation", return_value={
            "api_url": "http://managed-service:8888", "management_url": "http://managed-service:8766",
            "command": "installation-command", "message": "installed",
        }) as install:
            response = await self.panel.prepare()
            catalog.resolve_checked.assert_not_awaited()
            self.assertEqual(response["config_revision"], self.panel.config_revision())
            self.assertNotIn("management_key", response)
            self.assertTrue(self.panel.state()["management_key"])
            with self.assertRaisesRegex(module.PanelError, "配置已被更新"):
                await self.panel.prepare()
            self.assertEqual(install.call_count, 1)

    async def test_save_changes_only_plugin_configuration(self):
        before = copy.deepcopy(self.profile)
        await self.save()
        self.assertEqual(self.profile, before)
        self.profile.save_config.assert_not_called()
        self.assertNotIn("enabled", self.config)
        self.assertNotIn("permission_config_version", self.config)
        self.assertTrue((await self.panel.bootstrap())["config"]["configured"])

    async def test_model_settings_are_applied_after_installation(self):
        self.panel.saved_state = {"id": "012345abcdef", "api_url": "http://managed-service:8888"}
        self.panel.managed_request = AsyncMock(return_value={"installed": True, "configured": True, "ready": True})
        catalog = self.panel.providers()
        catalog.resolve_checked.return_value = {"HINDSIGHT_API_LLM_MODEL": "selected"}
        self.panel.providers = lambda: catalog
        selection = {"chat_provider_id": "chat", "embedding_provider_id": "embed", "rerank_provider_id": ""}
        self.panel.body = AsyncMock(return_value=selection)
        response = await self.panel.save_models()
        self.assertTrue(response["saved"])
        catalog.resolve_checked.assert_awaited_once_with(selection)
        self.assertEqual(self.panel.managed_request.await_args_list[-1].args[1], {"HINDSIGHT_API_LLM_MODEL": "selected"})
        self.assertEqual(self.panel.state()["embedding_provider_id"], "embed")
        self.profile.save_config.assert_not_called()

    async def test_uninstall_requires_explicit_data_deletion_and_owned_installation(self):
        self.panel.body = AsyncMock(return_value={"delete_memory_data": False})
        with self.assertRaisesRegex(module.PanelError, "确认删除"):
            await self.panel.prepare_uninstall()
        self.panel.body = AsyncMock(return_value={"delete_memory_data": True})
        with self.assertRaisesRegex(ValueError, "找不到"):
            await self.panel.prepare_uninstall()
        self.panel.state(create=True)
        result = await self.panel.prepare_uninstall()
        self.assertTrue(result["command"])

    async def test_reinstall_does_not_need_the_uninstalled_service(self):
        state = self.panel.state(create=True)
        state.update(api_url=self.config["api_url"], management_url="http://old-service:8766")
        self.panel.store_state(state)
        self.panel.managed_request = AsyncMock(side_effect=module.PanelError("offline"))
        snapshot = await self.panel.bootstrap()
        self.assertTrue(snapshot["config"]["configured"])
        self.assertFalse(snapshot["managed_service"]["installed"])
        self.panel.managed_request.reset_mock()
        self.panel.body = AsyncMock(return_value={"config_revision": snapshot["config_revision"]})
        with patch.object(module, "installation", return_value={
            "api_url": state["api_url"], "management_url": state["management_url"],
            "command": "reinstall-command", "message": "",
        }):
            response = await self.panel.prepare()
        self.assertEqual(response["command"], "reinstall-command")
        self.assertEqual(self.panel.state()["id"], state["id"])
        self.panel.managed_request.assert_not_awaited()

    async def test_managed_tracks_active_url_rather_than_candidate_existence(self):
        state = self.panel.state(create=True)
        state["api_url"] = "http://managed-service:8888"
        self.panel.store_state(state)
        self.assertFalse((await self.panel.bootstrap())["config"]["managed"])
        for url in ("", state["api_url"], state["api_url"] + "/"):
            self.config["api_url"] = url
            with self.subTest(url=url):
                self.assertTrue((await self.panel.bootstrap())["config"]["managed"])

    async def test_incomplete_models_cannot_finish_setup(self):
        state = self.panel.state(create=True)
        state.update(api_url="http://managed-service:8888", management_url="http://managed-service:8766")
        self.panel.store_state(state)
        self.panel.managed_request = AsyncMock(return_value={"configured": False, "ready": False})
        before = copy.deepcopy(self.config)
        with self.assertRaisesRegex(module.PanelError, "先在设置中选择模型"):
            await self.save(api_url=state["api_url"])
        self.assertEqual(self.config, before)
        self.profile.save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
