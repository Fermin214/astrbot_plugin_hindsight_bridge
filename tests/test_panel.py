"""Panel configuration invariants without AstrBot or model services."""

import asyncio
import copy
import hashlib
import hmac
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import AsyncMock, Mock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import panel as module
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Scope
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
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

    def is_file(self):
        return False

    def read_text(self, **kwargs):
        return json.dumps(self.panel.saved_state)


class InMemoryPanel(module.MemoryPanel):
    def has_identity_data(self):
        return False

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
            refresh_permissions=lambda: None, recall_status=lambda scope: None,
            prepare_sources=AsyncMock(), source_accounts={},
            diagnostic=lambda code: code,
            error_code=lambda exc: exc.code if isinstance(exc, HindsightError) else "timeout",
        )

        async def close_resources():
            self.plugin.client = None
            self.plugin.outbox = None

        async def initialize():
            if self.config.get("api_url") and self.config.get("api_key"):
                self.plugin.outbox = SimpleNamespace(
                    recovering=False, list_scopes=lambda: [], connection_error=lambda: "",
                    sync_permissions=Mock(), connection_id=self.config["service_id"],
                )
                self.plugin.client = module.HindsightClient(self.config["api_url"], self.config["api_key"])

        @asynccontextmanager
        async def resources():
            yield

        self.plugin._close_resources = AsyncMock(side_effect=close_resources)
        self.plugin.initialize = AsyncMock(side_effect=initialize)
        self.plugin.using_resources = resources
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
                self.api_key = key

            async def ensure_auth(self, **kwargs):
                owner.auth_calls.append((self.url, self.key))
                if owner.auth_gate:
                    await owner.auth_gate(self)

            async def request(self, method, path, **kwargs):
                if path == "/v1/version":
                    return {"api_version": "1.2.0"}
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

    def configure_managed_connect(self):
        self.panel.saved_state = {"id": "012345abcdef", "api_url": "http://managed-service:8888", "api_key": "managed-key",
                                  "management_url": "http://managed-service:8766", "management_key": "management-key"}
        self.model_environment = {"HINDSIGHT_API_LLM_MODEL": "chat"}
        self.catalog = self.panel.providers()
        self.catalog.resolve_checked.return_value = self.model_environment
        self.panel.providers = lambda: self.catalog
        self.remote_fingerprint = ""
        self.apply_count = 0

        async def managed(state, environment=None):
            if environment is not None:
                self.apply_count += 1
                self.remote_fingerprint = hmac.new(state["api_key"].encode(), json.dumps(
                    environment, sort_keys=True, separators=(",", ":"),
                ).encode(), hashlib.sha256).hexdigest()
            return {"installed": True, "configured": bool(self.remote_fingerprint), "ready": bool(self.remote_fingerprint),
                    "configuration_fingerprint": self.remote_fingerprint, "applying": False}

        self.managed = managed
        self.panel.managed_request = AsyncMock(side_effect=managed)
        return {"mode": "managed", "installation_id": "012345abcdef", "request_id": "connect-1",
                "chat_provider_id": "chat-provider", "embedding_provider_id": "embedding-provider", "rerank_provider_id": ""}

    async def connect_managed(self, payload):
        self.panel.body = AsyncMock(return_value={**payload, "config_revision": self.panel.config_revision()})
        result = await self.panel.managed_connect()
        await self.panel.model_task
        return result, self.panel.saved_state["connection_job"]

    async def test_authenticated_network_change_keeps_managed_service_identity(self):
        payload = self.configure_managed_connect()
        self.config.update(api_url=self.panel.saved_state["api_url"], api_key="managed-key")
        self.panel.saved_state["connection_mode"] = "managed"

        async def moved(state, environment=None):
            state.update(api_url="http://172.17.0.5:8888", management_url="http://172.17.0.5:8766")
            return await self.managed(state, environment)

        self.panel.managed_request.side_effect = moved
        _, job = await self.connect_managed(payload)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(self.config["service_id"], "old-service-id")
        self.assertEqual(self.config["api_url"], "http://172.17.0.5:8888")

    async def test_network_identity_proof_precedes_credentials_and_rejects_wrong_service(self):
        import httpx

        state = {"id": "012345abcdef", "api_key": "api-secret", "management_key": "management-secret",
                 "api_url": "http://old:8888", "management_url": "http://old:8766"}
        endpoints = {"api_url": "http://172.17.0.5:8888", "management_url": "http://172.17.0.5:8766"}
        for correct in (False, True):
            with self.subTest(correct=correct):
                requests = []

                def serve(request):
                    requests.append(request)
                    if request.url.path.startswith("/identity/"):
                        self.assertNotIn("authorization", request.headers)
                        challenge = request.url.path.rsplit("/", 1)[1]
                        signature = hmac.new(
                            state["management_key"].encode(),
                            ("astrbot-memory-identity:" + state["id"] + ":" + challenge).encode(), hashlib.sha256,
                        ).hexdigest()
                        return httpx.Response(200, json={"installation_id": state["id"],
                                                        "signature": signature if correct else "0" * 64})
                    self.assertEqual(request.headers["authorization"], "Bearer management-secret")
                    return httpx.Response(200, json={"installation_id": state["id"], "protocol": 1})

                client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
                with patch.object(self.panel, "network_endpoints", return_value=endpoints), \
                        patch.object(module.httpx, "AsyncClient", return_value=client):
                    if correct:
                        await self.panel.managed_request(state)
                        self.assertEqual(state["api_url"], endpoints["api_url"])
                        self.assertEqual(len(requests), 2)
                    else:
                        with self.assertRaisesRegex(module.PanelError, "标识不匹配"):
                            await self.panel.managed_request(state)
                        self.assertEqual(len(requests), 1)

    async def test_managed_connect_from_existing_service_persists_connection_once_and_keeps_legacy_boundary(self):
        payload = self.configure_managed_connect()
        initial_revision = self.panel.config_revision()
        _, job = await self.connect_managed(payload)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["stage"], "complete")
        self.assertEqual(self.config["api_url"], "http://managed-service:8888")
        self.assertEqual(self.config["api_key"], "managed-key")
        identity = self.config["service_id"]
        self.assertEqual(self.panel.saved_state["connections"]["http://old-service:8888"], "old-service-id")
        self.panel.body = AsyncMock(return_value=payload)
        repeated = await self.panel.managed_connect()
        self.assertEqual(repeated["status"], "succeeded")
        self.assertEqual(self.config["service_id"], identity)
        self.assertEqual(self.apply_count, 1)
        self.assertNotIn("fingerprint", repeated)
        self.assertNotEqual(initial_revision, self.panel.config_revision())
        revision = self.panel.config_revision()
        self.panel.persist_connection_job(job, message="完成")
        self.assertEqual(revision, self.panel.config_revision())
        self.profile.save_config.assert_not_called()

    async def test_managed_connection_commit_failure_recovers_original_config_and_reuses_applied_models(self):
        payload = self.configure_managed_connect()
        previous = copy.deepcopy(dict(self.config))
        failures = 0

        def save_config():
            nonlocal failures
            if self.config["api_url"] == "http://managed-service:8888" and not failures:
                failures += 1
                raise OSError("disk write failed")

        self.config.save_config = save_config
        _, job = await self.connect_managed(payload)
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(dict(self.config), previous)
        self.assertEqual(job["stage"], "connecting")
        planned_identity = self.panel.saved_state["connections"]["http://managed-service:8888"]
        _, job = await self.connect_managed(payload)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(self.config["service_id"], planned_identity)
        self.assertEqual(self.apply_count, 1)
        self.assertEqual(self.catalog.resolve_checked.await_count, 1)

    async def test_reload_after_model_request_recovers_persistent_job_without_reapplying_models(self):
        payload = self.configure_managed_connect()
        entered = asyncio.Event()

        async def dropped(state, environment=None):
            result = await self.managed(state, environment)
            if environment is not None:
                entered.set()
                await asyncio.Event().wait()
            return result

        self.panel.managed_request = AsyncMock(side_effect=dropped)
        self.panel.body = AsyncMock(return_value={**payload, "config_revision": self.panel.config_revision()})
        await self.panel.managed_connect()
        await entered.wait()
        await self.panel.close()
        saved = copy.deepcopy(self.panel.saved_state)
        self.assertEqual(saved["connection_job"]["status"], "interrupted")
        self.assertEqual(self.config["api_url"], "http://old-service:8888")
        self.panel = InMemoryPanel(self.plugin)
        self.panel.saved_state = saved
        self.panel.providers = lambda: self.catalog
        self.panel.managed_request = AsyncMock(side_effect=self.managed)
        self.panel.body = AsyncMock(return_value={"mode": "managed", "installation_id": payload["installation_id"]})
        await self.panel.managed_status()
        await self.panel.model_task
        job = self.panel.saved_state["connection_job"]
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(self.apply_count, 1)
        self.assertEqual(self.catalog.resolve_checked.await_count, 1)

    async def test_explicit_managed_options_and_independent_access_save_do_not_manage_existing_service(self):
        payload = self.configure_managed_connect()
        self.panel.body = AsyncMock(return_value=payload)
        with self.assertRaisesRegex(module.PanelError, "已有服务仅提供"):
            await self.panel.save_models()
        options = await self.panel.managed_options()
        self.assertEqual(options["installation_id"], payload["installation_id"])
        self.assertNotIn("managed-key", json.dumps(options))
        self.panel.body = AsyncMock(return_value={"control_plane_url": "https://memory.example.com"})
        await self.panel.save_access()
        self.assertEqual(self.config["control_plane_url"], "https://memory.example.com")
        self.assertEqual(self.config["api_url"], "http://old-service:8888")
        self.panel.managed_request.assert_not_awaited()
        self.plugin.initialize.assert_not_awaited()

    async def test_reload_during_connection_commit_recovers_without_reapplying_or_reopening_closed_resources(self):
        for boundary in ("_close_resources", "initialize"):
            with self.subTest(boundary=boundary):
                self.setUp()
                payload = self.configure_managed_connect()
                entered = asyncio.Event()
                operation = getattr(self.plugin, boundary)
                original = operation.side_effect

                async def interrupted():
                    await original()
                    entered.set()
                    await asyncio.Event().wait()

                operation.side_effect = interrupted
                self.panel.body = AsyncMock(return_value={**payload, "config_revision": self.panel.config_revision()})
                await self.panel.managed_connect()
                await asyncio.wait_for(entered.wait(), 2)
                planned_identity = self.panel.saved_state["connections"]["http://managed-service:8888"]
                calls = self.plugin.initialize.await_count
                await self.panel.close()
                self.assertEqual(self.plugin.initialize.await_count, calls)
                self.assertEqual(self.panel.saved_state["connection_job"]["stage"], "connecting")
                saved = copy.deepcopy(self.panel.saved_state)
                operation.side_effect = original
                self.plugin.stopping = False
                self.panel = InMemoryPanel(self.plugin)
                self.panel.saved_state = saved
                self.panel.providers = lambda: self.catalog
                self.panel.managed_request = AsyncMock(side_effect=self.managed)
                self.panel.body = AsyncMock(return_value=payload)
                await self.panel.managed_status()
                await self.panel.model_task
                job = self.panel.saved_state["connection_job"]
                self.assertEqual(job["status"], "succeeded", job)
                self.assertEqual(self.config["service_id"], planned_identity)
                self.assertEqual(self.apply_count, 1)

    async def test_external_configuration_change_during_commit_is_not_overwritten_or_accepted(self):
        for boundary in ("_close_resources", "initialize"):
            with self.subTest(boundary=boundary):
                self.setUp()
                payload = self.configure_managed_connect()
                operation = getattr(self.plugin, boundary)
                original = operation.side_effect

                async def changed():
                    await original()
                    self.config["recall_timeout_seconds"] = 27

                operation.side_effect = changed
                _, job = await self.connect_managed(payload)
                self.assertEqual(job["status"], "failed", job)
                self.assertIn("配置已变化", job["message"])
                self.assertEqual(self.config["recall_timeout_seconds"], 27)
                self.assertEqual(self.config["api_url"], "http://old-service:8888")
                self.panel.body = AsyncMock(return_value=payload)
                with self.assertRaisesRegex(module.PanelError, "配置已变化"):
                    await self.panel.managed_connect()

    async def test_active_model_application_failure_reopens_resources_with_permission_refresh(self):
        payload = self.configure_managed_connect()
        await self.connect_managed(payload)
        self.plugin.permissions = frozenset({("qq", "123")})
        self.remote_fingerprint = "different-model-configuration"

        async def failed(state, environment=None):
            if environment is not None:
                raise module.PanelError("model validation failed")
            return await self.managed(state)

        self.panel.managed_request.side_effect = failed
        _, job = await self.connect_managed({**payload, "request_id": "connect-2"})
        self.assertEqual(job["status"], "failed", job)
        self.assertIsNotNone(self.plugin.outbox)
        self.assertIsNone(self.plugin.permissions)
        self.assertFalse(self.plugin.stopping)

    async def test_same_owned_address_can_use_existing_mode_without_changing_identity_or_granting_management(self):
        payload = self.configure_managed_connect()
        await self.connect_managed(payload)
        identity = self.config["service_id"]
        self.panel.body = AsyncMock(return_value={"api_url": self.config["api_url"], "mode": "existing"})
        self.panel.managed_request.reset_mock()
        await self.panel.save()
        self.panel.managed_request.assert_not_awaited()
        self.assertEqual(self.config["service_id"], identity)
        self.assertFalse(self.panel.managed_connection())
        self.panel.managed_request.reset_mock()
        await self.panel.check()
        self.panel.managed_request.assert_not_awaited()
        self.assertEqual(await self.panel.model_status(), {"status": "idle"})

    async def test_closed_panel_rejects_late_save_and_new_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()
        previous = copy.deepcopy(dict(self.config))

        async def gate(client):
            entered.set()
            await release.wait()

        self.auth_gate = gate
        saving = asyncio.create_task(self.save(api_key="new-secret"))
        try:
            await entered.wait()
            self.plugin.stopping = True
            await self.panel.close()
            release.set()
            with self.assertRaisesRegex(module.PanelError, "插件已停用"):
                await saving
            with self.assertRaisesRegex(module.PanelError, "插件已停用"):
                await self.save()
            self.assertEqual(dict(self.config), previous)
            self.plugin.initialize.assert_not_awaited()
            self.assertTrue(self.plugin.stopping)
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)

    async def test_offline_account_removal_updates_existing_queue_without_restarting(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Outbox(
                Path(temporary) / "runtime", self.config["service_id"], self.config["api_url"],
                credential_fingerprint="test", control_directory=Path(temporary) / "control",
            )
            self.plugin.outbox = queue
            self.plugin.refresh_permissions = lambda: queue.sync_permissions(pairs(self.config["allowed_accounts"]))
            self.plugin.refresh_permissions()
            scope = queue.scope(Incoming("aiocqhttp", "qq", "20001", "123", "", int(time.time()), ""))
            self.auth_gate = AsyncMock(side_effect=module.HindsightError("connection_error"))
            self.panel.managed_request = AsyncMock(side_effect=module.PanelError("offline"))
            try:
                result = await self.save(allowed_accounts=[])
                self.assertTrue(result["saved"])
                self.assertEqual(self.config["allowed_accounts"], [])
                self.assertFalse(queue.can_recall(scope))
                self.assertIs(self.plugin.outbox, queue)
                self.auth_gate.assert_not_awaited()
                self.panel.managed_request.assert_not_awaited()
                self.plugin.initialize.assert_not_awaited()
                self.plugin._close_resources.assert_not_awaited()
            finally:
                queue.close()

    async def test_verified_active_connection_clears_persistent_auth_pause(self):
        @asynccontextmanager
        async def resources():
            yield

        queue = SimpleNamespace(
            recovering=False, list_scopes=lambda: [],
            connection_error=unittest.mock.Mock(return_value="http_401"),
            pause_connection=unittest.mock.Mock(),
        )
        self.plugin.outbox = queue
        self.plugin.worker = SimpleNamespace(wake=asyncio.Event())
        self.plugin.using_resources = resources
        self.plugin.client = SimpleNamespace(
            url=self.config["api_url"], api_key=self.config["api_key"],
            ensure_auth=AsyncMock(),
        )
        await self.panel.bootstrap()
        self.plugin.client.ensure_auth.assert_awaited_once_with(force=True)
        queue.pause_connection.assert_called_once_with("")
        self.assertTrue(self.plugin.worker.wake.is_set())
        self.panel.connection_ready("http://different-service", self.config["api_key"])
        self.panel.connection_ready(self.config["api_url"], "different-key")
        queue.pause_connection.assert_called_once()

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

    async def test_settings_save_uses_native_astrbot_account_template_format(self):
        await self.save(allowed_accounts=[{"platform_instance_id": "qq", "user_id": "123"}])
        self.assertEqual(self.config["allowed_accounts"], [{
            "__template_key": "account", "platform_instance_id": "qq", "user_id": "123",
        }])
        self.assertEqual(pairs(self.config["allowed_accounts"]), frozenset({("qq", "123")}))

    async def test_connection_only_save_preserves_existing_users(self):
        self.panel.body = AsyncMock(return_value={
            "api_url": self.config["api_url"], "api_key": "",
            "control_plane_url": "https://memory.example.com",
            "config_revision": self.panel.config_revision(),
        })
        result = await self.panel.save()
        self.assertTrue(result["saved"])
        self.assertEqual(pairs(self.config["allowed_accounts"]), frozenset({("qq", "123")}))
        self.assertEqual(self.config["control_plane_url"], "https://memory.example.com")

    async def test_users_only_save_preserves_connection_and_management_address(self):
        self.config["control_plane_url"] = "https://memory.example.com/"
        self.panel.body = AsyncMock(return_value={
            "allowed_accounts": [], "config_revision": self.panel.config_revision(),
        })
        result = await self.panel.save()
        self.assertTrue(result["saved"])
        self.assertEqual(self.config["allowed_accounts"], [])
        self.assertEqual(self.config["api_url"], "http://old-service:8888")
        self.assertEqual(self.config["api_key"], "old-secret")
        self.assertEqual(self.config["control_plane_url"], "https://memory.example.com/")
        self.assertEqual(self.auth_calls, [])

    async def test_saved_users_are_visible_without_a_running_service(self):
        snapshot = await self.panel.bootstrap()
        self.assertEqual(len(snapshot["scopes"]), 1)
        user = snapshot["scopes"][0]
        self.assertEqual((user["platform_instance_id"], user["user_id"]), ("qq", "123"))
        self.assertTrue(user["pending"])
        self.assertEqual(user["pending_reason"], "service")

    async def test_missing_service_identity_is_explicit_and_existing_data_is_not_rebound(self):
        self.config["service_id"] = ""
        snapshot = await self.panel.bootstrap()
        self.assertEqual(snapshot["config"]["missing_fields"], ["service_id"])
        self.assertIn("记忆服务识别码", snapshot["status"]["label"])
        self.panel.has_identity_data = lambda: True
        before = copy.deepcopy(self.config)
        with self.assertRaisesRegex(module.PanelError, "恢复原插件配置"):
            await self.save()
        self.assertEqual(self.config, before)
        self.assertEqual(self.auth_calls, [])
        self.assertIsNone(self.panel.saved_state)

    async def test_invalid_timeout_is_rejected_before_network_and_config_save(self):
        self.config["recall_timeout"] = 31
        before = copy.deepcopy(self.config)
        with self.assertRaisesRegex(module.PanelError, "invalid_recall_timeout"):
            await self.save()
        self.assertEqual(self.config, before)
        self.assertEqual(self.auth_calls, [])

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
            adapter = SimpleNamespace(get_client=lambda: bot)
            self.plugin.context.get_platform_inst = lambda platform: adapter
            async def prepare():
                self.plugin.source_accounts["qq"] = {"bot_id": "20001"}
            self.plugin.prepare_sources.side_effect = prepare
            try:
                snapshot = await self.panel.bootstrap()
                scope = snapshot["scopes"][0]
                self.assertTrue(scope["pending"])
                self.assertEqual(scope["pending_reason"], "authorization")
                self.plugin.worker = SimpleNamespace(wake=asyncio.Event())
                request = SimpleNamespace(username="admin")
                payload = {
                    "action": "prepare", "platform_instance_id": "qq", "user_id": "123",
                    "config_revision": snapshot["config_revision"],
                }
                with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=request)}):
                    self.panel.body = AsyncMock(return_value=payload)
                    prepared = await self.panel.authorize()
                    self.panel.body = AsyncMock(return_value={
                        **payload, "action": "confirm", "confirmation": prepared["confirmation"],
                    })
                    await self.panel.authorize()
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
        self.config["api_url"] = ""
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
        self.config["api_url"] = ""
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
        state = self.panel.state(create=True)
        state["api_url"] = self.config["api_url"]
        state["api_key"] = self.config["api_key"]
        self.panel.store_state(state)
        self.panel.body = AsyncMock(return_value={"delete_memory_data": False})
        with self.assertRaisesRegex(module.PanelError, "确认删除"):
            await self.panel.prepare_uninstall()
        self.panel.saved_state = {}
        self.panel.body = AsyncMock(return_value={"delete_memory_data": True})
        with self.assertRaisesRegex(module.PanelError, "配套服务"):
            await self.panel.prepare_uninstall()
        self.panel.store_state(state)
        result = await self.panel.prepare_uninstall()
        self.assertTrue(result["command"])

    async def test_existing_connection_cannot_manage_a_retained_installation_or_reuse_its_model_job(self):
        state = self.panel.state(create=True)
        state.update(api_url="http://managed-service:8888", management_url="http://managed-service:8766")
        self.panel.store_state(state)
        before = copy.deepcopy(state)
        config = copy.deepcopy(self.config)
        self.panel.managed_request = AsyncMock()
        self.panel.model_selection = {
            "chat_provider_id": "chat", "embedding_provider_id": "embed", "rerank_provider_id": "",
        }
        self.panel.model_job = {"request_id": "previous", "status": "succeeded"}
        for handler, payload in (
            (self.panel.prepare, {"repair": True}),
            (self.panel.save_models, {}),
            (self.panel.save_models, {
                **self.panel.model_selection, "background": True, "request_id": "previous",
            }),
            (self.panel.prepare_uninstall, {"delete_memory_data": True}),
        ):
            with self.subTest(handler=handler.__name__, payload=payload):
                self.panel.body = AsyncMock(return_value={
                    "config_revision": self.panel.config_revision(), **payload,
                })
                with self.assertRaisesRegex(module.PanelError, "配套服务"):
                    await handler()
                self.assertEqual(self.panel.state(), before)
                self.assertEqual(self.config, config)
        snapshot = await self.panel.bootstrap()
        self.assertFalse(snapshot["config"]["managed"])
        self.assertFalse(snapshot["has_installation"])
        self.assertEqual(snapshot["installation_api_url"], "")
        self.assertEqual(snapshot["control_plane_key_command"], "")
        self.assertIsNone(snapshot["managed_service"])
        self.assertIsNone(snapshot["model_job"])
        self.assertEqual(await self.panel.model_status(), {"status": "idle"})
        self.assertIsNone(self.panel.model_task)
        self.panel.managed_request.assert_not_awaited()
        self.plugin._close_resources.assert_not_awaited()

    async def test_existing_credentials_at_a_recorded_address_do_not_grant_managed_access(self):
        state = self.panel.state(create=True)
        state.update(api_url=self.config["api_url"], management_url="http://old-service:8766")
        self.panel.store_state(state)
        self.panel.managed_request = AsyncMock()
        self.panel.body = AsyncMock(return_value={"delete_memory_data": True, "repair": True})
        for handler in (self.panel.save_models, self.panel.prepare, self.panel.prepare_uninstall):
            with self.subTest(handler=handler.__name__), self.assertRaisesRegex(module.PanelError, "配套服务"):
                await handler()
        snapshot = await self.panel.bootstrap()
        self.assertFalse(snapshot["config"]["managed"])
        self.assertIsNone(snapshot["managed_service"])
        self.assertEqual(snapshot["control_plane_key_command"], "")
        self.panel.managed_request.assert_not_awaited()
        self.assertEqual(self.panel.state(), state)
        self.panel.body = AsyncMock(return_value={
            "api_url": self.config["api_url"], "api_key": self.config["api_key"],
        })
        self.assertTrue((await self.panel.check())["ready"])
        await self.save()
        self.assertEqual(self.config["service_id"], "old-service-id")
        self.assertEqual(self.panel.state()["api_key"], state["api_key"])
        self.panel.managed_request.assert_not_awaited()

    async def test_reinstall_does_not_need_the_uninstalled_service(self):
        state = self.panel.state(create=True)
        state.update(api_url=self.config["api_url"], api_key=self.config["api_key"],
                     management_url="http://old-service:8766")
        self.panel.store_state(state)
        self.panel.managed_request = AsyncMock(side_effect=module.PanelError("offline"))
        snapshot = await self.panel.bootstrap()
        self.assertTrue(snapshot["config"]["configured"])
        self.assertFalse(snapshot["managed_service"]["installed"])
        self.panel.managed_request.reset_mock()
        self.panel.body = AsyncMock(return_value={"config_revision": snapshot["config_revision"], "repair": True})
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
        state["api_key"] = self.config["api_key"]
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
        with self.assertRaisesRegex(module.PanelError, "保存模型"):
            await self.save(api_url=state["api_url"])
        self.assertEqual(self.config, before)
        self.profile.save_config.assert_not_called()

    async def test_control_plane_address_is_independent_and_can_be_cleared(self):
        await self.save(control_plane_url="https://memory.example.com/admin/")
        self.assertEqual(self.config["api_url"], self.payload["api_url"])
        snapshot = await self.panel.bootstrap()
        self.assertEqual(snapshot["config"]["control_plane_url"], "https://memory.example.com/admin/")
        await self.save()
        self.assertEqual(self.config["control_plane_url"], "https://memory.example.com/admin/")
        await self.save(control_plane_url="")
        self.assertEqual(self.config["control_plane_url"], "")

    async def test_control_plane_rejects_credentials_and_non_http_before_network(self):
        for value in (
            "javascript:alert(1)", "ftp://memory.example.com", "//memory.example.com",
            "http:///missing", "https://admin:secret@memory.example.com",
            "https://@memory.example.com", "https://memory.example.com/?api_key=secret",
            "https://memory.example.com/#token", "https://memory.example.com\\@other",
            "https://mem\nory.example.com", "https://memory.example.com/old-secret",
            42,
        ):
            with self.subTest(value=value), self.assertRaises(module.PanelError):
                await self.save(control_plane_url=value)
        self.assertEqual(self.auth_calls, [])
        self.assertNotIn("control_plane_url", self.config)
        with self.assertRaises(module.PanelError):
            await self.save(api_key="new-secret", control_plane_url="https://memory.example.com/new-secret")

    async def test_bootstrap_hides_invalid_address_and_service_secrets(self):
        self.panel.saved_state = {
            "id": "012345abcdef", "api_url": self.config["api_url"],
            "api_key": self.config["api_key"], "management_key": "manager-secret",
            "control_plane_key": "cp-secret", "database_password": "db-secret",
        }
        self.config["control_plane_url"] = "https://memory.example.com/?key=cp-secret"
        snapshot = await self.panel.bootstrap()
        self.assertEqual(snapshot["config"]["control_plane_url"], "")
        self.assertIn("astrbot-memory-012345abcdef/.env", snapshot["control_plane_key_command"])
        serialized = json.dumps(snapshot)
        for secret in ("old-secret", "setup-secret", "manager-secret", "cp-secret", "db-secret"):
            self.assertNotIn(secret, serialized)

    async def test_installer_control_plane_hint_is_optional_and_safe(self):
        self.config["api_url"] = ""
        for hint in (None, "http://127.0.0.1:9999"):
            self.panel.body = AsyncMock(return_value={})
            result = {
                "api_url": "http://managed:8888", "management_url": "http://managed:8766",
                "command": "installation-command", "message": "",
            }
            if hint is not None:
                result["control_plane_url"] = hint
            with patch.object(module, "installation", return_value=result):
                response = await self.panel.prepare()
            self.assertEqual(response["control_plane_url"], hint or "")
            self.assertNotIn("management_key", response)
            if hint:
                self.assertEqual(self.panel.state()["control_plane_url"], hint)

    def management_resources(self):
        scope = {
            "scope_id": "scope-1", "owner_id": "owner-1", "bank": "bank-1",
            "revision": 7, "allowed": True, "mode": "active", "generation": 0,
            "identity_json": Scope("deployment", "aiocqhttp", "qq", "20001", "123").identity,
        }
        summary = {"counts": {}, "last_error": "", "maintenance": {"phase": "draining"}}
        queue = SimpleNamespace(
            recovering=False, connection_id="connection-1",
            scope_owner=Mock(side_effect=lambda identifier: scope if identifier == "scope-1" else None),
            control=SimpleNamespace(owner=Mock(return_value={"bank": "bank-1", "revision": 7})),
            can_recall=Mock(return_value=False), summary=Mock(return_value=summary),
            set_mode=Mock(), request_retry=Mock(return_value=2), forget=Mock(),
            management=Mock(return_value="management-capability"),
        )
        maintenance = SimpleNamespace(
            begin=Mock(return_value={"phase": "draining"}),
            status=AsyncMock(return_value={"phase": "ready", "ready": True}),
            resume=AsyncMock(), check=AsyncMock(return_value=True),
        )
        self.plugin.outbox = queue
        self.plugin.worker = SimpleNamespace(maintenance=maintenance, wake=asyncio.Event())

        @asynccontextmanager
        async def resources():
            yield

        self.plugin.using_resources = resources
        self.panel.bot_id = AsyncMock(return_value="20001")
        return scope, queue, maintenance

    async def action(self, action, *, method="POST", scope_id="scope-1", **payload):
        self.panel.body = AsyncMock(return_value={"action": action, **payload})
        request = SimpleNamespace(method=method, query={"action": action, **payload}, username="admin")
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=request)}):
            return await self.panel.action(scope_id)

    async def test_maintenance_can_begin_poll_and_resume_without_recall_permission(self):
        _, queue, maintenance = self.management_resources()
        result = await self.action("maintenance_begin", discard_pending=True)
        maintenance.begin.assert_called_once_with("scope-1", discard_pending=True, management="management-capability")
        self.assertEqual(result["maintenance"], {"phase": "draining"})
        self.assertTrue(self.plugin.worker.wake.is_set())
        self.plugin.worker.wake.clear()
        result = await self.action("maintenance_status", method="GET")
        maintenance.status.assert_awaited_once_with("scope-1", management="management-capability")
        self.assertEqual(result["maintenance"], {"phase": "ready", "ready": True})
        self.assertTrue(self.plugin.worker.wake.is_set())
        maintenance.status.return_value = {"phase": "inactive", "ready": False}
        result = await self.action("maintenance_resume")
        maintenance.resume.assert_awaited_once_with("scope-1", management="management-capability")
        self.assertEqual(result["maintenance"], {"phase": "inactive", "ready": False})
        queue.can_recall.assert_not_called()

    async def test_maintenance_defaults_to_drain_and_requires_boolean_discard(self):
        _, _, maintenance = self.management_resources()
        await self.action("maintenance_begin")
        maintenance.begin.assert_called_once_with("scope-1", discard_pending=False, management="management-capability")
        for value in ("false", 1, None):
            with self.subTest(value=value), self.assertRaises(module.PanelError):
                await self.action("maintenance_begin", discard_pending=value)
        self.assertEqual(maintenance.begin.call_count, 1)

    async def test_management_scope_preserves_permission_and_mapping_checks(self):
        scope, queue, maintenance = self.management_resources()
        for change in ("recovering", "missing", "bank", "revision", "owner"):
            queue.recovering = change == "recovering"
            queue.scope_owner.side_effect = None
            queue.scope_owner.return_value = None if change == "missing" else scope
            queue.control.owner.return_value = None if change == "owner" else {
                "bank": "other" if change == "bank" else "bank-1",
                "revision": 8 if change == "revision" else 7,
            }
            with self.subTest(change=change), self.assertRaises(module.PanelError):
                await self.action("maintenance_resume")
        maintenance.resume.assert_not_awaited()

    async def test_active_mode_cannot_bypass_maintenance_or_missing_bank(self):
        _, queue, maintenance = self.management_resources()
        for summary in ({"maintenance": {"phase": "ready"}}, {"bank_missing": True}):
            queue.summary.return_value = summary
            with self.subTest(summary=summary), self.assertRaisesRegex(module.PanelError, "解除读写暂停"):
                await self.action("mode", mode="active")
        queue.set_mode.assert_not_called()
        maintenance.check.assert_not_awaited()
        queue.summary.side_effect = [{"maintenance": None}, {"bank_missing": True}]
        with self.assertRaises(module.PanelError):
            await self.action("mode", mode="active")
        queue.set_mode.assert_not_called()
        queue.summary.side_effect = None
        queue.summary.return_value = {"maintenance": None, "bank_missing": False}
        await self.action("mode", mode="active")
        queue.set_mode.assert_called_once_with("scope-1", "active")

    async def test_bank_check_requires_actual_original_bank_and_preserves_modes(self):
        scope, queue, _ = self.management_resources()
        scope["allowed"] = False
        queue.control.set_bank_state = Mock()
        queue.apply_control = Mock()
        self.plugin.client = SimpleNamespace(ensure_auth=AsyncMock(), bank_exists=AsyncMock(return_value=False))
        with self.assertRaisesRegex(module.PanelError, "原记忆库仍不存在"):
            await self.action("bank_check")
        queue.control.set_bank_state.assert_not_called()
        self.plugin.client.bank_exists.return_value = True
        await self.action("bank_check")
        queue.control.set_bank_state.assert_called_once_with("connection-1", "bank-1", "established")
        queue.set_mode.assert_not_called()
        queue.apply_control.assert_called_once_with()

    async def test_bank_check_rejects_bank_rotation_during_remote_read(self):
        scope, queue, _ = self.management_resources()
        queue.scope_owner.side_effect = lambda identifier: dict(scope)
        queue.control.set_bank_state = Mock()
        queue.apply_control = Mock()
        async def rotate(bank):
            scope["bank"] = "new-bank"
            queue.control.owner.return_value["bank"] = "new-bank"
            return True
        self.plugin.client = SimpleNamespace(ensure_auth=AsyncMock(), bank_exists=AsyncMock(side_effect=rotate))
        with self.assertRaisesRegex(module.PanelError, "记忆范围已变化"):
            await self.action("bank_check")
        queue.control.set_bank_state.assert_not_called()
        queue.apply_control.assert_not_called()

    async def test_get_actions_are_read_only_and_content_actions_are_removed(self):
        _, _, maintenance = self.management_resources()
        for action in ("maintenance_begin", "maintenance_resume", "bank_check", "mode", "retry"):
            with self.subTest(action=action), self.assertRaises(module.PanelError):
                await self.action(action, method="GET")
        for action in ("correct",):
            with self.subTest(action=action), self.assertRaises(module.PanelError):
                await self.action(action)
        maintenance.begin.assert_not_called()
        maintenance.resume.assert_not_awaited()

    async def test_bank_rotation_requires_explicit_confirmation_bound_to_mapping(self):
        scope, queue, _ = self.management_resources()
        result = await self.action("forget_prepare")
        queue.forget.assert_not_called()
        await self.action("forget_confirm", confirmation=result["confirmation"])
        queue.forget.assert_called_once_with("scope-1", management="management-capability")
        with self.assertRaises(module.PanelError):
            await self.action("forget_confirm", confirmation=result["confirmation"])
        result = await self.action("forget_prepare")
        scope["bank"] = "bank-2"
        queue.control.owner.return_value = {"bank": "bank-2", "revision": 7}
        with self.assertRaisesRegex(module.PanelError, "确认已失效"):
            await self.action("forget_confirm", confirmation=result["confirmation"])
        self.assertEqual(queue.forget.call_count, 1)

    async def test_registered_routes_only_expose_management_and_setup(self):
        register = Mock()
        self.plugin.context.register_web_api = register
        module.MemoryPanel(self.plugin)
        routes = {
            (call.args[0].split("/")[-1], method)
            for call in register.call_args_list for method in call.args[2]
        }
        self.assertIn(("action", "GET"), routes)
        self.assertIn(("action", "POST"), routes)
        for name in ("memories", "detail", "search", "export"):
            self.assertNotIn((name, "GET"), routes)
            self.assertNotIn((name, "POST"), routes)

    async def test_real_maintenance_missing_bank_recovers_only_by_explicit_rotation(self):
        @asynccontextmanager
        async def resources():
            yield

        with tempfile.TemporaryDirectory() as temporary:
            queue = Outbox(
                Path(temporary) / "runtime", self.config["service_id"], self.config["api_url"],
                credential_fingerprint="test", control_directory=Path(temporary) / "control",
            )
            client = SimpleNamespace(
                ensure_auth=AsyncMock(), list_operations=AsyncMock(return_value=[]),
                bank_exists=AsyncMock(return_value=False), create_bank=AsyncMock(),
            )
            self.plugin.outbox = queue
            self.plugin.using_resources = resources
            self.plugin.refresh_permissions = lambda: queue.sync_permissions(pairs(self.config["allowed_accounts"]))
            self.plugin.worker = SimpleNamespace(maintenance=Maintenance(queue, client), wake=asyncio.Event())
            try:
                self.plugin.refresh_permissions()
                scope = queue.scope(Incoming("aiocqhttp", "qq", "20001", "123", "", 1, ""))
                identifier, bank = scope["scope_id"], scope["bank"]
                queue.control.set_bank_state(queue.connection_id, bank, "established")
                await self.action("maintenance_begin", scope_id=identifier)
                self.assertFalse(queue.can_recall(queue.scope_owner(identifier), manual=True))
                status = await self.action("maintenance_status", method="GET", scope_id=identifier)
                self.assertTrue(status["maintenance"]["ready"])
                with self.assertRaisesRegex(module.QueueError, "bank_missing"):
                    await self.action("maintenance_resume", scope_id=identifier)
                self.assertEqual(queue.scope_owner(identifier)["bank"], bank)
                self.assertTrue(queue.summary(identifier)["bank_missing"])
                client.create_bank.assert_not_awaited()
                confirmation = await self.action("forget_prepare", scope_id=identifier)
                await self.action("forget_confirm", scope_id=identifier, confirmation=confirmation["confirmation"])
                self.assertNotEqual(queue.scope_owner(identifier)["bank"], bank)
                self.assertEqual(queue.scope_owner(identifier)["mode"], "paused")
                client.create_bank.assert_not_awaited()
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
