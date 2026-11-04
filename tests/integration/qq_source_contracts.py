"""Actual AstrBot/SDK objects with synthetic QQ payloads and mocked login I/O."""

import asyncio
import copy
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import AsyncMock, patch

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core.config.default import VERSION
from astrbot.core.message.components import File, Plain
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    PatchedC2CMessage, QQOfficialPlatformAdapter,
)
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_adapter import (
    QQOfficialWebhookPlatformAdapter,
)
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import QQOfficialWebhook
from astrbot.core.utils.metrics import Metric
from botpy.message import DirectMessage


ROOT = Path(__file__).resolve().parents[2]
HOST_TYPE = os.environ.get("QQ_TRANSPORT", "qq_official")
assert HOST_TYPE in {"qq_official", "qq_official_webhook"}
for name, path in (("plugins", ROOT.parent), ("plugins.astrbot_plugin_hindsight_bridge", ROOT)):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package

from plugins.astrbot_plugin_hindsight_bridge.core.materials import Message, retain_payload
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.qq_source import native_login
from plugins.astrbot_plugin_hindsight_bridge.core.source import native_header, source_identity


class Fixture:
    def __init__(self, instance="qq-fixture", appid="1000001"):
        self.queue = asyncio.Queue()
        adapter = QQOfficialPlatformAdapter if HOST_TYPE == "qq_official" else QQOfficialWebhookPlatformAdapter
        self.host = adapter({
            "id": instance, "type": HOST_TYPE, "appid": appid, "enable": True,
            "secret": "synthetic-offline-secret", "enable_group_c2c": True,
            "enable_guild_direct_message": False,
        }, {}, self.queue)
        self.client = self.host.get_client()
        self.binding = None

    async def login(self, appid="1000001"):
        identity = {"id": "9000" + appid, "username": "synthetic-bot", "avatar": ""}
        if HOST_TYPE == "qq_official":
            with (
                patch.object(self.client.http, "check_session", AsyncMock()),
                patch.object(self.client.http, "request", AsyncMock(return_value=identity)),
                patch.object(self.client.api, "get_ws_url", AsyncMock(return_value={
                    "url": "wss://unused.invalid", "shards": 1,
                    "session_start_limit": {"max_concurrency": 1, "remaining": 1},
                })),
                patch.object(self.client, "_bot_init", AsyncMock()),
            ):
                await self.client.start(appid, "synthetic-offline-secret")
        else:
            helper = QQOfficialWebhook(
                {**self.host.config, "appid": appid}, self.queue, self.client,
            )
            self.host.webhook_helper = helper
            with (
                patch.object(helper.http, "check_session", AsyncMock()),
                patch.object(helper.http, "request", AsyncMock(return_value=identity)),
            ):
                await helper.initialize()
        login = native_login(self.host)
        assert login is not None
        self.binding = {
            "adapter": self.host, "client": self.client, "bot_id": login.appid,
            "login": login, "invalid_login": False,
        }

    async def event(self, text="  synthetic original text  ", *, data=None, direct=False):
        payload = {
            "id": "synthetic-c2c-message", "content": text,
            "timestamp": datetime.fromtimestamp(time.time() + 2, timezone.utc).isoformat(),
            "author": {"user_openid": "synthetic-openid", "bot": False},
            "message_type": 0, "message_scene": {},
            **(data or {}),
        }
        # The host imports time() directly; control the receive clock at construction.
        with patch.object(sys.modules[AstrMessageEvent.__module__], "time", time.time):
            if direct:
                raw = DirectMessage(self.client.api, "synthetic-event", payload)
                await self.client.on_direct_message_create(raw)
            else:
                raw = PatchedC2CMessage(self.client.api, "synthetic-event", payload)
                await self.client.on_c2c_message_create(raw)
        event = self.queue.get_nowait()
        assert event.bot is self.client
        return event

    def header(self, event):
        return native_header(
            event, Plain, ["/"], adapter=self.host, binding=self.binding,
        )


def rejected_command(queue, incoming, expected, *, mode="active"):
    try:
        queue.access.command(
            incoming, incoming.scope(queue.deployment_id),
            queue.scope_owner(incoming.scope(queue.deployment_id).id), "mode", (mode,),
        )
    except QueueError as error:
        assert str(error) == expected, str(error)
    else:
        raise AssertionError("queued or replayed command crossed a control boundary")


async def control_contracts(fixture, directory):
    baseline = time.time()
    queue = Outbox(
        Path(directory) / "control-runtime", "fixture-deployment", "http://memory.test",
        credential_fingerprint="fixture",
    )
    pairs = [("qq-fixture", "synthetic-openid")]

    async def incoming(offset, text, mid):
        with patch.object(time, "time", return_value=baseline + offset):
            result = fixture.header(await fixture.event(text, data={"id": mid}))
            assert result is not None
            return result

    try:
        initial = await incoming(0, "authorization probe", "grant")
        with patch.object(time, "time", return_value=baseline):
            queue.sync_permissions(pairs)
            queue.activate(initial, queue.access.authorization(*pairs[0])["revision"])
        accepted = await incoming(1, "immutable accepted-period source", "frozen")
        with patch.object(time, "time", return_value=baseline + 1):
            captured = queue.capture(accepted, "original-conversation")
        frozen = dict(queue.get(captured.task_id))
        queued = await incoming(2, "queued before revocation", "queued-before-revoke")
        with patch.object(time, "time", return_value=baseline + 3):
            queue.sync_permissions([])
            assert not queue.accepts(queued)
            assert not queue.current(frozen)
        with patch.object(time, "time", return_value=baseline + 4):
            queue.sync_permissions(pairs)
            assert not queue.accepts(queued)
            queue.activate(initial, queue.access.authorization(*pairs[0])["revision"])
            assert not queue.accepts(queued)
            assert queue.current(frozen)
            assert dict(queue.get(captured.task_id)) == frozen
        fresh = await incoming(5, "newly authorized source", "fresh-after-regrant")
        assert queue.accepts(fresh)

        applied = await incoming(6, "/hindsight on", "already-applied-on")
        source = applied.scope(queue.deployment_id)
        with patch.object(time, "time", return_value=baseline + 6):
            scope = queue.scope(applied)
            command = queue.access.command(applied, source, scope, "mode", ("active",))
            queue.access.run(command, scope, lambda: queue.set_mode(
                scope["scope_id"], "active", journal_only=True,
            ))
            queue.apply_control()
        delayed = await incoming(7, "/hindsight on", "first-seen-late-on")
        with patch.object(time, "time", return_value=baseline + 8):
            queue.set_mode(source.id, "paused")
        with patch.object(time, "time", return_value=baseline + 9):
            rejected_command(queue, applied, "command_already_applied")
            conflict = await incoming(9, "/hindsight readonly", "already-applied-on")
            rejected_command(queue, conflict, "command_identity_conflict", mode="readonly")
            rejected_command(queue, delayed, "command_expired")
            assert queue.scope_owner(source.id)["mode"] == "paused"
            assert queue.control.db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 1
        with patch.object(time, "time", return_value=baseline + 10):
            old_bank = queue.scope_owner(source.id)["bank"]
            queue.forget(source.id)
            assert queue.scope_owner(source.id)["bank"] != old_bank
            rejected_command(queue, delayed, "command_expired")
            rejected_command(queue, applied, "command_already_applied")
            assert queue.scope_owner(source.id)["mode"] == "paused"
    finally:
        queue.close()
    return [
        "native_queued_event_rejected_after_regrant_while_frozen_task_can_resume",
        "native_replayed_or_conflicting_command_cannot_override_newer_pause",
        "native_first_seen_late_command_cannot_cross_pause_or_forget_generation",
    ]


async def user_isolation_contract(fixture, directory):
    queue = Outbox(
        Path(directory) / "user-runtime", "fixture-deployment", "http://memory.test",
        credential_fingerprint="fixture",
    )
    baseline = time.time()
    try:
        with patch.object(time, "time", return_value=baseline):
            queue.sync_permissions([("qq-fixture", "synthetic-openid")])
            first = fixture.header(await fixture.event("first isolated code Fir-31"))
            second = fixture.header(await fixture.event(
                "second isolated code Oak-82", data={
                    "author": {"user_openid": "second-synthetic-openid", "bot": False},
                },
            ))
            assert first and second
            queue.activate(first, queue.access.authorization("qq-fixture", first.user_id)["revision"])
            assert not queue.accepts(second)
            queue.sync_permissions([
                ("qq-fixture", first.user_id), ("qq-fixture", second.user_id),
            ])
            queue.activate(second, queue.access.authorization("qq-fixture", second.user_id)["revision"])
        with patch.object(time, "time", return_value=baseline + 1):
            first = fixture.header(await fixture.event("first isolated code Fir-31"))
            second = fixture.header(await fixture.event(
                "second isolated code Oak-82", data={
                    "author": {"user_openid": "second-synthetic-openid", "bot": False},
                },
            ))
            assert first and second and first.message_id == second.message_id
            assert queue.accepts(first) and queue.accepts(second)
            one = queue.capture(first, "same-conversation-label")
            two = queue.capture(second, "same-conversation-label")
        a, b = queue.get(one.task_id), queue.get(two.task_id)
        assert a["scope_id"] != b["scope_id"] and a["bank"] != b["bank"]
        assert a["document_id"] != b["document_id"] and a["operation_id"] != b["operation_id"]
        assert "Oak-82" not in a["payload"] and "Fir-31" not in b["payload"]
        assert not one.history and not two.history
    finally:
        queue.close()
    return "native_distinct_openids_isolate_same_message_id_and_conversation_label"


async def outbound_contracts(fixture, directory):
    event = await fixture.event("native output anchor")
    sent = []

    async def post(**payload):
        sent.append(copy.deepcopy(payload))
        return {"id": "synthetic-outbound-" + str(len(sent))}

    def text(payload):
        return payload.get("content") or (payload.get("markdown") or {}).get("content") or ""

    passed = []
    with (
        patch.object(event, "post_c2c_message", AsyncMock(side_effect=post)),
        patch.object(Metric, "upload", AsyncMock()),
    ):
        long_text = "BEGIN-" + "synthetic long detail " * 500 + "-END"
        await event.send(MessageChain([Plain(long_text)]))
        assert len(sent) == 1 and text(sent[0]) == long_text
        assert sent[0]["openid"] == "synthetic-openid"
        passed.append("native_long_text_reaches_mocked_c2c_api_without_truncation")

        data = b'{"synthetic_export": ["Fir-31", "Oak-82"]}'
        export = Path(directory) / "synthetic-export.json"
        export.write_bytes(data)
        export.chmod(0o600)

        async def upload(file_source, file_type, *, file_name, openid):
            assert Path(file_source) == export and export.read_bytes() == data
            assert file_type == 4 and file_name == export.name
            assert openid == "synthetic-openid"
            return {"file_info": "synthetic-upload-reference"}

        sent.clear()
        with patch.object(event, "upload_group_and_c2c_media", AsyncMock(side_effect=upload)) as upload_call:
            await event.send(MessageChain([File(name=export.name, file=str(export))]))
            assert upload_call.await_count == 1
        assert len(sent) == 1 and sent[0]["msg_type"] == 7
        assert sent[0]["media"] == {"file_info": "synthetic-upload-reference"}
        passed.append("native_local_json_file_uses_upload_and_media_send")

        async def chunks():
            for chunk in ("stream alpha", " beta", " gamma"):
                yield MessageChain([Plain(chunk)])

        sent.clear()
        await event.send_streaming(chunks())
        assert "".join(text(payload) for payload in sent).rstrip("\n") == "stream alpha beta gamma"
        assert sent[-1]["stream"]["state"] == 10
        passed.append("native_stream_deltas_preserved_and_final_segment_closed")

        async def tool_break():
            yield MessageChain([Plain("tool preamble")])
            yield MessageChain(type="break")
            yield MessageChain([Plain("final answer")])

        sent.clear()
        await event.send_streaming(tool_break())
        assert sum(payload["stream"]["state"] == 10 for payload in sent) == 2
        assert "".join(text(payload).rstrip("\n") for payload in sent) == "tool preamblefinal answer"
        passed.append("native_stream_tool_break_closes_both_segments")
        await asyncio.sleep(0)
    return passed


async def main():
    assert VERSION == os.environ["ASTRBOT_TEST_VERSION"], VERSION
    fixture = Fixture()
    await fixture.login()
    event = await fixture.event(data={"timestamp": "2026-11-03T12:34:56.123456+08:00"})
    original = fixture.header(event)
    assert original and original.bot_id == "1000001"
    assert event.get_self_id() == "unknown_selfid"
    assert original.text == "  synthetic original text  "
    assert original.timestamp == datetime.fromisoformat("2026-11-03T12:34:56.123456+08:00").timestamp()
    identity = source_identity(event, original, fixture.host, binding=fixture.binding)
    assert identity and identity.producer is fixture.client
    assert identity.logical_type == "qq_official" and identity.host_type == HOST_TYPE
    material = retain_payload(
        original.scope("fixture-deployment"), 0,
        Message(original.message_id, original.text, original.timestamp),
        (), "conversation", "operation", "UTC",
    )["items"][0]
    assert material["metadata"]["time_source"] == "qq_official.timestamp"
    assert material["timestamp"] == "2026-11-03T04:34:56.123456+00:00"
    passed = ["actual_native_c2c_callback_original_fields_and_fractional_material_time"]

    command = await fixture.event("/hindsight pause")
    command.message_str = "hindsight pause"
    command.is_at_or_wake_command = True
    assert fixture.header(command).text == "/hindsight pause"
    passed.append("host_wake_prefix_change_keeps_original_command")

    attachment = await fixture.event(data={"attachments": [{"content_type": "image/png"}]})
    assert [type(part).__name__ for part in attachment.get_messages()] == ["At", "Plain"]
    assert fixture.header(attachment) is None
    passed.append("raw_attachment_rejected_when_host_omits_media_component")

    quote = await fixture.event(data={"message_reference": {"message_id": "quoted-id"}})
    assert [type(part).__name__ for part in quote.get_messages()] == ["Reply", "At", "Plain"]
    assert fixture.header(quote) is None
    passed.append("actual_host_quote_conversion_rejected")

    quote_elements = await fixture.event(data={
        "message_type": 103,
        "msg_elements": [{
            "content": "synthetic quoted body", "message_type": 0,
            "msg_idx": "synthetic-quote-index",
        }],
        "parallel_message": {"msg_nodes": [{
            "content": "synthetic current body", "message_type": 0,
        }]},
    })
    assert fixture.header(quote_elements) is None
    passed.append("observed_quote_type_and_parallel_elements_shape_rejected")

    direct = await fixture.event(direct=True)
    assert direct.is_private_chat() and fixture.header(direct) is None
    passed.append("channel_direct_message_not_misclassified_as_c2c")

    extra = await fixture.event(data={"unverified_extension": {"content": "synthetic"}})
    assert fixture.header(extra) is None
    passed.append("unverified_raw_extension_rejected")

    current = Fixture()
    await current.login()
    assert current.header(event) is None
    old_binding = fixture.binding
    await fixture.login()
    fixture.binding = old_binding
    assert fixture.header(event) is None
    passed.append("old_producer_and_same_client_login_change_rejected")

    raw_api = await current.event()
    raw_api.message_obj.raw_message._api = object()
    assert current.header(raw_api) is None
    passed.append("raw_sdk_api_must_match_authenticated_producer")

    if HOST_TYPE == "qq_official_webhook":
        # A new helper replaces API/HTTP even when a caller reuses the client.
        queued = await current.event()
        await current.login()
        assert current.header(queued) is None
        assert current.header(await current.event()) is not None
        passed.append("helper_relogin_old_api_rejected_before_first_observation")

        changed = await current.event()
        current.host.webhook_helper.user["id"] = "different-authenticated-bot"
        assert current.header(changed) is None
        await current.login()
        current.host.webhook_helper.appid = "1000002"
        assert current.header(await current.event()) is None
        await current.login()
        passed.append("helper_authenticated_id_and_appid_mutations_rejected")

    with tempfile.TemporaryDirectory() as temporary:
        queue = Outbox(
            Path(temporary) / "runtime", "fixture-deployment", "http://memory.test",
            credential_fingerprint="fixture",
        )
        try:
            queue.sync_permissions([("qq-fixture", "synthetic-openid")])
            incoming = current.header(await current.event())
            queue.activate(incoming, queue.access.authorization("qq-fixture", "synthetic-openid")["revision"])
            assert not queue.accepts(incoming)
            with patch.object(time, "time", return_value=time.time() + 3):
                incoming = current.header(await current.event(data={"id": "after-authorization"}))
            assert incoming and queue.accepts(incoming)
            task = queue.capture(incoming, "fixture-conversation")
            frozen = dict(queue.get(task.task_id))
            reloaded = Fixture(appid="1000002")
            await reloaded.login("1000002")
            new_incoming = reloaded.header(await reloaded.event())
            assert new_incoming and new_incoming.bot_id == "1000002"
            queue.access.observe_bot("qq-fixture", "qq_official", "1000002")
            grant = queue.access.authorization("qq-fixture", "synthetic-openid")
            queue.activate(new_incoming, grant["revision"])
            assert not queue.accepts(incoming)
            assert not queue.current(frozen)
            assert queue.current(frozen, writing=False)
            assert queue.get(task.task_id)["payload"] == frozen["payload"]
            assert queue.get(task.task_id)["operation_id"] == frozen["operation_id"]
            passed.append("late_authorization_and_rebinding_preserve_old_frozen_operation")
            other = Fixture(instance="other-instance")
            await other.login()
            other_incoming = other.header(await other.event())
            assert other_incoming.scope(queue.deployment_id).id != incoming.scope(queue.deployment_id).id
            passed.append("separate_native_instance_does_not_share_scope")
        finally:
            queue.close()
        passed.extend(await control_contracts(current, temporary))
        passed.append(await user_isolation_contract(current, temporary))
        passed.extend(await outbound_contracts(current, temporary))
    print(json.dumps({
        "astrbot": VERSION, "qq_botpy": importlib.metadata.version("qq-botpy"),
        "transport": HOST_TYPE,
        "evidence_layer": "actual_host_sdk_objects_synthetic_payloads_and_mocked_login_io",
        "real_qq_login": False, "real_platform_delivery": False,
        "host_receive_clock": "controlled_at_native_event_construction",
        "outbound_upload_and_send_io": "mocked_no_platform_api_called",
        "native_send_metrics_io": "mocked_no_host_database_or_telemetry_write",
        "passed": passed,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
