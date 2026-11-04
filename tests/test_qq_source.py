"""QQ provenance, raw text boundaries and cached-login invalidation."""

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from plugins.astrbot_plugin_hindsight_bridge.core import qq_source as qq
from plugins.astrbot_plugin_hindsight_bridge.core import source
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.materials import Message, retain_payload
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.worker import Worker
from test_reliability import Service
from test_runtime_diagnostics import Configuration, Plain, bridge_class


class At:
    def __init__(self, qq):
        self.qq = qq


class C2C:
    def __init__(self, data):
        self.raw_data = data
        self.author = SimpleNamespace(user_openid=data.get("author", {}).get("user_openid"))
        self.id = data.get("id")
        self.timestamp = data.get("timestamp")
        self.content = data.get("content")


class Client:
    def __init__(self, adapter):
        self.platform = adapter
        self.robot = SimpleNamespace(id="native-robot-id-not-appid")
        self.api, self.http = object(), object()

    def is_closed(self):
        return False


class Adapter:
    host_type = "qq_official"
    client_type = Client

    def __init__(self, appid="1001"):
        self.appid = appid
        self.config = {"id": "qq", "type": self.host_type, "appid": appid}
        self.client = self.client_type(self)

    def meta(self):
        return SimpleNamespace(id="qq", name=self.host_type)

    def get_client(self):
        return self.client


class WebhookClient(Client):
    def __init__(self, adapter):
        super().__init__(adapter)
        del self.robot


class WebhookHelper:
    def __init__(self, adapter):
        self.appid, self.client = adapter.appid, adapter.client
        self.api, self.http = self.client.api, self.client.http
        self.user = {"id": "native-webhook-id-not-appid"}


class WebhookAdapter(Adapter):
    host_type = "qq_official_webhook"
    client_type = WebhookClient

    def __init__(self, appid="1001"):
        super().__init__(appid)
        self.webhook_helper = WebhookHelper(self)


def types_for(host_type):
    if host_type == "qq_official":
        return C2C, At, Adapter, Client, None
    if host_type == "qq_official_webhook":
        return C2C, At, WebhookAdapter, WebhookClient, WebhookHelper


def replace_login(adapter):
    if adapter.host_type == "qq_official":
        adapter.client.robot = SimpleNamespace(id="reentered-native-login")
    else:
        adapter.webhook_helper.user = {"id": "reentered-native-login"}


class QQSourceTests(unittest.TestCase):
    adapter_type = Adapter
    host_type = "qq_official"

    def setUp(self):
        self.clock = patch.object(time, "time", return_value=100.25)
        self.clock.start()
        self.types = patch.object(qq, "native_types", side_effect=types_for)
        self.types.start()
        self.support = patch.dict(source.PLATFORMS, {
            self.host_type: replace(source.PLATFORMS[self.host_type], supported=True),
        })
        self.support.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.types.stop)
        self.addCleanup(self.support.stop)
        self.adapter = self.adapter_type()
        self.binding = self.bind(self.adapter)

    @staticmethod
    def bind(adapter):
        return {"adapter": adapter, "client": adapter.client, "bot_id": adapter.appid,
                "login": qq.native_login(adapter), "invalid_login": False}

    def event(self, text="  QQ original text  ", **updates):
        data = {
            "author": {"user_openid": "OpenID_A", "bot": False},
            "id": "opaque-message-id", "content": text,
            "timestamp": "1970-01-01T00:01:41.250000Z", "message_type": 0,
        }
        data.update(updates)
        raw = C2C(data)
        raw._api = self.adapter.client.api
        return SimpleNamespace(
            bot=self.adapter.client, message_obj=SimpleNamespace(raw_message=raw, message_id=raw.id),
            get_platform_name=lambda: self.host_type, get_platform_id=lambda: "qq",
            get_self_id=lambda: "unknown_selfid", get_sender_id=lambda: "OpenID_A",
            is_private_chat=lambda: True, get_group_id=lambda: "",
            created_at=100.25, get_message_str=lambda: text.strip(),
            get_messages=lambda: [At("qq_official"), Plain(text.strip())],
        )

    def header(self, event, wake_prefixes=()):
        return source.native_header(event, Plain, wake_prefixes,
                                    adapter=self.adapter, binding=self.binding)

    def test_original_text_openid_appid_fractional_time_and_provenance(self):
        event = self.event()
        incoming = self.header(event)
        self.assertEqual(incoming.bot_id, "1001")
        self.assertEqual(incoming.user_id, "OpenID_A")
        self.assertEqual(incoming.text, "  QQ original text  ")
        self.assertEqual(incoming.timestamp, 101.25)
        self.assertEqual(incoming.received_at, 100.25)
        self.assertEqual(incoming.iso_timestamp, "1970-01-01T00:01:41.250000+00:00")
        identity = source.source_identity(event, incoming, self.adapter, binding=self.binding)
        self.assertEqual((identity.host_type, identity.logical_type), (self.host_type, "qq_official"))
        material = retain_payload(incoming.scope("deployment"), 0, Message(
            "event", incoming.text, incoming.timestamp,
        ), (), "conversation", "operation", "UTC")["items"][0]
        self.assertEqual(material["metadata"]["time_source"], "qq_official.timestamp")
        self.assertEqual(material["timestamp"], incoming.iso_timestamp)

    def test_rfc3339_offsets_and_invalid_precision_never_fall_back_to_host_time(self):
        self.assertEqual(qq.timestamp("1970-01-01T08:01:41.25+08:00"), 101.25)
        for value in (None, "", 101, "not-a-date", "1970-01-01T00:01:41",
                      "1970-01-01 00:01:41Z", "1970-01-01T00:01:41+00:60",
                      "1970-02-30T00:01:41Z", "1970-01-01T00:01:41.1234567Z",
                      "1970-01-01T00:10:00Z"):
            with self.subTest(value=value):
                self.assertIsNone(self.header(self.event(timestamp=value)))

    def test_commands_keep_raw_prefix_after_native_wake_stripping(self):
        event = self.event("/hindsight status")
        event.get_message_str = lambda: "hindsight status"
        self.assertIsNone(self.header(event, ["/"]))
        event.is_at_or_wake_command = True
        self.assertEqual(self.header(event, ["/"]).text, "/hindsight status")
        self.assertIsNone(self.header(event, ["!"]))

    def test_sdk_type_and_original_identifiers_are_required(self):
        for change in ("raw_type", "raw_api", "openid", "id", "raw_id", "host_id", "raw_timestamp",
                       "raw_content", "bot_author", "host_group", "host_private"):
            with self.subTest(change=change):
                event = self.event()
                raw = event.message_obj.raw_message
                if change == "raw_type":
                    event.message_obj.raw_message = SimpleNamespace(**vars(raw))
                elif change == "raw_api":
                    raw._api = object()
                elif change == "openid":
                    raw.raw_data["author"]["user_openid"] = "someone-else"
                elif change == "id":
                    raw.raw_data.pop("id")
                elif change == "raw_id":
                    raw.id = "other-message"
                elif change == "host_id":
                    event.message_obj.message_id = "other-message"
                elif change == "raw_timestamp":
                    raw.timestamp = "1970-01-01T00:01:42Z"
                elif change == "raw_content":
                    raw.content = "different"
                elif change == "bot_author":
                    raw.raw_data["author"]["bot"] = True
                elif change == "host_group":
                    event.get_group_id = lambda: "group"
                elif change == "host_private":
                    event.is_private_chat = lambda: False
                self.assertIsNone(self.header(event))

    def test_raw_media_reference_and_unknown_extensions_cannot_downgrade_to_plain(self):
        for fields in (
            {"attachments": [{"content_type": "image/png"}]},
            {"attachments": {}}, {"attachments": False},
            {"msg_elements": [{"type": 103}]}, {"msg_elements": ""},
            {"message_reference": {"message_id": "quoted"}}, {"message_reference": {}},
            {"message_type": 103}, {"message_type": False}, {"message_type": None},
            {"unknown_content_extension": "hidden"}, {"channel_id": "channel"},
            {"group_openid": "group"},
        ):
            with self.subTest(fields=fields):
                self.assertIsNone(self.header(self.event(**fields)))

    def test_only_verified_at_plain_shape_is_allowed_and_face_translation_rejects(self):
        for components in (
            [Plain("QQ original text")],
            [At("unknown"), Plain("QQ original text")],
            [At("qq_official"), At("qq_official"), Plain("QQ original text")],
            [At("qq_official"), Plain("QQ original "), Plain("text")],
            [At("qq_official"), Plain("transformed face text")],
        ):
            with self.subTest(types=[type(item).__name__ for item in components]):
                event = self.event()
                event.get_messages = lambda: components
                self.assertIsNone(self.header(event))

    def test_old_producer_and_changed_native_login_are_rejected_before_first_validation(self):
        old = self.event()
        self.adapter = self.adapter_type("1002")
        self.binding = self.bind(self.adapter)
        self.assertIsNone(self.header(old))
        event = self.event()
        replace_login(self.adapter)
        self.assertIsNone(self.header(event))
        self.binding = self.bind(self.adapter)
        self.adapter.client.api = object()
        self.assertIsNone(self.header(event))
        self.binding = self.bind(self.adapter)
        self.adapter.appid = "1003"
        self.assertIsNone(self.header(event))

    def test_missing_prepared_login_or_disabled_support_never_assigns_current_appid(self):
        event = self.event()
        self.binding = None
        self.assertIsNone(self.header(event))
        self.binding = self.bind(self.adapter)
        with patch.dict(source.PLATFORMS, {
            self.host_type: replace(source.PLATFORMS[self.host_type], supported=False),
        }):
            self.assertIsNone(self.header(event))

    def test_official_message_dedup_preserves_first_frozen_request_and_rejects_conflict(self):
        event = self.event()
        event.created_at = 100.5
        incoming = self.header(event)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = Outbox(root / "runtime", "service", "http://memory.test",
                           credential_fingerprint="test", control_directory=root / "control")
            try:
                queue.sync_permissions([("qq", "OpenID_A")])
                grant = queue.access.authorization("qq", "OpenID_A")
                queue.activate(incoming, grant["revision"])
                first = queue.capture(incoming, "conversation")
                frozen = dict(queue.get(first.task_id))
                replay = queue.capture(incoming, "another-conversation")
                self.assertFalse(replay.created)
                self.assertEqual(replay.task_id, first.task_id)
                with self.assertRaisesRegex(QueueError, "message_identity_content_conflict"):
                    queue.capture(replace(incoming, text="conflicting text"), "conversation")
                self.assertEqual(queue.get(first.task_id)["payload"], frozen["payload"])
                self.assertEqual(queue.db.execute("SELECT occurred_at FROM events").fetchone()[0], 101.25)
            finally:
                queue.close()


class QQWebhookSourceTests(QQSourceTests):
    adapter_type = WebhookAdapter
    host_type = "qq_official_webhook"

    def test_native_helper_authentication_and_producer_binding_are_required(self):
        for change in ("helper_missing", "helper_type", "helper_client", "helper_appid",
                       "helper_api", "helper_http", "user_missing", "user_type", "user_id"):
            with self.subTest(change=change):
                self.adapter = self.adapter_type()
                self.binding = self.bind(self.adapter)
                event = self.event()
                helper = self.adapter.webhook_helper
                if change == "helper_missing":
                    self.adapter.webhook_helper = None
                elif change == "helper_type":
                    self.adapter.webhook_helper = SimpleNamespace(**vars(helper))
                elif change == "helper_client":
                    helper.client = object()
                elif change == "helper_appid":
                    helper.appid = "1002"
                elif change == "helper_api":
                    helper.api = object()
                elif change == "helper_http":
                    helper.http = object()
                elif change == "user_missing":
                    del helper.user
                elif change == "user_type":
                    helper.user = SimpleNamespace(id="native-id")
                elif change == "user_id":
                    helper.user = {"id": ""}
                self.assertIsNone(self.header(event))

    def test_in_place_authenticated_user_mutation_invalidates_cached_login(self):
        event = self.event()
        self.adapter.webhook_helper.user["id"] = "different-native-login"
        self.assertIsNone(self.header(event))

    def test_helper_replacement_and_raw_api_before_first_observation_reject_old_event(self):
        old_event = self.event()
        self.adapter.client.api, self.adapter.client.http = object(), object()
        self.adapter.webhook_helper = WebhookHelper(self.adapter)
        self.binding = self.bind(self.adapter)
        self.assertIsNone(self.header(old_event))
        self.assertIsNotNone(self.header(self.event()))


class QQLoginHookTests(unittest.IsolatedAsyncioTestCase):
    adapter_type = Adapter

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.types = patch.object(qq, "native_types", side_effect=types_for)
        self.types.start()
        self.addCleanup(self.types.stop)
        self.adapter = self.adapter_type()
        self.platforms = [dict(self.adapter.config)]
        self.context = SimpleNamespace(
            get_config=lambda *args: {"platform": self.platforms, "wake_prefix": ["/"]},
            get_platform_inst=lambda instance: self.adapter,
        )
        config = Configuration(allowed_accounts=[{"platform_instance_id": "qq", "user_id": "OpenID_A"}])
        root = Path(self.tmp.name)
        self.plugin = bridge_class(root, Mock())(self.context, config)
        self.plugin.outbox = Outbox(root / "runtime", "service", "http://memory.test",
                                   credential_fingerprint="test", control_directory=root / "control")
        self.addCleanup(self.plugin.outbox.close)
        self.plugin.refresh_permissions()
        await self.plugin.prepare_sources()

    async def test_native_reload_allows_same_appid_with_new_producer(self):
        first = self.plugin.source_accounts["qq"]
        self.adapter = self.adapter_type()
        await self.plugin.prepare_sources()
        current = self.plugin.source_accounts["qq"]
        self.assertEqual(current["bot_id"], "1001")
        self.assertIsNot(current["client"], first["client"])
        self.assertFalse(current["invalid_login"])

    async def test_same_client_login_change_stays_invalid_until_native_adapter_replacement(self):
        replace_login(self.adapter)
        self.plugin.refresh_permissions()
        self.assertTrue(self.plugin.source_accounts["qq"]["invalid_login"])
        await self.plugin.prepare_sources()
        self.assertEqual(self.plugin.source_accounts["qq"]["bot_id"], "")
        self.assertTrue(self.plugin.source_accounts["qq"]["invalid_login"])
        await self.plugin.prepare_sources()
        self.assertEqual(self.plugin.source_accounts["qq"]["bot_id"], "")
        self.adapter = self.adapter_type()
        await self.plugin.prepare_sources()
        self.assertEqual(self.plugin.source_accounts["qq"]["bot_id"], "1001")

    async def test_async_management_refuses_config_appid_mismatch(self):
        self.platforms[0]["appid"] = "1002"
        self.assertEqual(await source.prepare_bot_identity(self.context, "qq"), "")


class QQWebhookLoginHookTests(QQLoginHookTests):
    adapter_type = WebhookAdapter

    async def test_in_place_helper_login_change_stays_invalid_until_adapter_replacement(self):
        self.adapter.webhook_helper.user["id"] = "different-native-login"
        self.plugin.refresh_permissions()
        self.assertTrue(self.plugin.source_accounts["qq"]["invalid_login"])
        await self.plugin.prepare_sources()
        self.assertEqual(self.plugin.source_accounts["qq"]["bot_id"], "")
        self.adapter = self.adapter_type()
        await self.plugin.prepare_sources()
        self.assertEqual(self.plugin.source_accounts["qq"]["bot_id"], "1001")


class QQRetryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def retry_case(self, adapter_type, change, boundary="retry"):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(time, "time", return_value=100.25),
            patch.object(qq, "native_types", side_effect=types_for),
        ):
            root = Path(directory)
            adapter = adapter_type()
            platforms = [dict(adapter.config)]
            context = SimpleNamespace(
                get_config=lambda *args: {"platform": platforms},
                get_platform_inst=lambda instance: adapter,
            )
            config = Configuration(
                allowed_accounts=[{"platform_instance_id": "qq", "user_id": "OpenID_A"}],
            )
            plugin = bridge_class(root, Mock())(context, config)
            queue = plugin.outbox = Outbox(
                root / "runtime", "service", "http://memory.test",
                credential_fingerprint="test", control_directory=root / "control",
            )
            service = Service()
            started, resume = asyncio.Event(), asyncio.Event()
            pending = None

            async def request(request):
                retry = request.method == "POST" and request.url.path.endswith("/retry")
                first = retry and not service.retries
                auth = (
                    boundary == "authentication" and service.retries == ["child-0"]
                    and request.url.path == "/v1/default/banks"
                    and request.headers.get("Authorization") == "Bearer valid-key"
                )
                if first and boundary == "retry" or auth:
                    started.set()
                    await resume.wait()
                result = await service(request)
                if first and boundary == "authentication":
                    client.auth_until = 0
                return result

            client = HindsightClient(
                "http://memory.test", "valid-key", transport=httpx.MockTransport(request),
            )
            try:
                plugin.refresh_permissions()
                await plugin.prepare_sources()
                incoming = Incoming(
                    "qq_official", "qq", "1001", "OpenID_A", "retry-boundary",
                    101.25, "original frozen text", received_at=100.5,
                )
                queue.activate(incoming, queue.access.authorization("qq", "OpenID_A")["revision"])
                task = queue.capture(incoming, "conversation")
                queue.release(task.task_id)
                frozen = dict(queue.get(task.task_id))
                service.tree(frozen["operation_id"], "failed", "failed", "failed", "cancelled")
                worker = Worker(
                    queue, client, Mock(), poll_interval=0.01,
                    prepare_sources=plugin.prepare_sources,
                    refresh_permissions=plugin.refresh_permissions,
                )
                await worker.process(queue.claim(queue.due()))
                self.assertEqual(queue.get(task.task_id)["state"], "failed")
                queue.request_retry(task.scope_id)
                worker.start_due()
                pending = next(iter(worker.running.values()))
                await asyncio.wait_for(started.wait(), 1)
                if change == "login":
                    replace_login(adapter)
                elif change in {"appid", "same-appid"}:
                    adapter = adapter_type("1002" if change == "appid" else "1001")
                    platforms[:] = [dict(adapter.config)]
                elif change == "revoke":
                    config["allowed_accounts"] = []
                elif change == "config":
                    platforms[0]["appid"] = "1002"
                resume.set()
                await asyncio.wait_for(pending, 1)
                blocked = change not in {"unchanged", "same-appid"}
                self.assertEqual(service.retries, ["child-0"] if blocked else ["child-0", "child-1"])
                self.assertEqual(service.submissions, [])
                current = queue.get(task.task_id)
                for key in ("bank", "generation", "payload", "operation_id", "document_id"):
                    self.assertEqual(current[key], frozen[key], key)
                self.assertTrue(queue.current(current, writing=False))
                self.assertEqual(queue.current(current), not blocked)
                self.assertEqual(current["retry_requested"], 0)
            finally:
                resume.set()
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await client.close()
                queue.close()

    async def test_each_child_retry_rechecks_source_after_previous_request(self):
        for adapter in (Adapter, WebhookAdapter):
            for change in ("login", "appid", "revoke", "config"):
                with self.subTest(transport=adapter.host_type, change=change):
                    await self.retry_case(adapter, change)

    async def test_each_child_retry_rechecks_source_after_authentication(self):
        for adapter in (Adapter, WebhookAdapter):
            for change in ("login", "appid", "revoke", "config"):
                with self.subTest(transport=adapter.host_type, change=change):
                    await self.retry_case(adapter, change, "authentication")

    async def test_unchanged_source_retries_all_failed_children(self):
        for adapter in (Adapter, WebhookAdapter):
            with self.subTest(transport=adapter.host_type):
                await self.retry_case(adapter, "unchanged")

    async def test_same_appid_native_reload_preserves_retry_authorization(self):
        for adapter in (Adapter, WebhookAdapter):
            with self.subTest(transport=adapter.host_type):
                await self.retry_case(adapter, "same-appid")

    async def test_retry_guard_remains_after_auth_even_if_its_cache_expires(self):
        service = Service()
        service.tree("retry-operation", "failed")
        guarded = False

        async def request(request):
            if request.url.path == "/v1/default/banks":
                self.assertFalse(guarded, "authentication ran after the final source check")
            return await service(request)

        client = HindsightClient(
            "http://memory.test", "valid-key", transport=httpx.MockTransport(request),
        )
        try:
            async def guard():
                nonlocal guarded
                guarded = True
                client.auth_until = 0

            await client.retry("original-bank", "retry-operation", before_send=guard)
            self.assertTrue(guarded)
            self.assertEqual(service.retries, ["retry-operation"])
        finally:
            await client.close()
