"""Second-layer evidence with actual host objects, synthetic protocol payloads."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import patch

from aiocqhttp import Event
from astrbot.core.config.default import VERSION
from astrbot.core.message.components import Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter


root = Path(__file__).resolve().parents[2]
for name, path in (("plugins", root.parent), ("plugins.astrbot_plugin_hindsight_bridge", root)):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package

from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.source import native_header, prepare_bot_identity
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options
from plugins.astrbot_plugin_hindsight_bridge.main import HindsightBridge


def adapter(instance):
    return AiocqhttpAdapter({
        "id": instance, "type": "aiocqhttp", "ws_reverse_host": "127.0.0.1",
        "ws_reverse_port": 0,
    }, {}, asyncio.Queue())


async def event(host, text, *, bot=1001, mid=1):
    raw = Event({
        "time": int(time.time()) + 1, "self_id": bot, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "user_id": 2001,
        "message_id": mid, "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0, "sender": {"user_id": 2001, "nickname": "fixture"},
    })
    return host.create_event(await host.convert_message(raw))


async def main():
    assert VERSION == os.environ.get("ASTRBOT_TEST_VERSION", "4.29.1"), VERSION
    host = adapter("fixture")
    old = await event(host, "hello")
    incoming = native_header(old, Plain, adapter=host)
    assert incoming and incoming.bot_id == "1001"
    assert native_header(old, Plain, adapter=host) == incoming
    raw = old.message_obj.raw_message
    old.message_obj.raw_message = dict(raw)
    assert native_header(old, Plain, adapter=host) is None
    old.message_obj.raw_message = raw
    context = types.SimpleNamespace(
        get_platform_inst=lambda instance: host,
        get_config=lambda: {"platform": [host.config], "wake_prefix": ["/"]},
    )
    with patch.dict(host.get_client()._wsr_api_clients, {"1001": object(), "1002": object()}):
        assert await prepare_bot_identity(context, "fixture") == ""
    command_event = await event(host, "/hb pause", mid=2)
    command_event.message_str = "hb pause"
    command_event.is_at_or_wake_command = True
    header = native_header(command_event, Plain, ["/"])
    assert header and header.text == "/hb pause"
    other_host = adapter("fixture")
    assert native_header(old, Plain, adapter=other_host) is None
    with tempfile.TemporaryDirectory() as temporary:
        queue = Outbox(Path(temporary) / "runtime", "fixture", "http://memory.test", credential_fingerprint="fixture")
        try:
            queue.sync_permissions([("fixture", "2001")])
            grant = queue.access.authorization("fixture", "2001")
            queue.activate(incoming, grant["revision"])
            assert not queue.accepts(incoming)
            bridge = object.__new__(HindsightBridge)
            bridge.context, bridge.outbox, bridge.stopping = context, queue, False
            bridge.config = {"allowed_accounts": [{"platform_instance_id": "fixture", "user_id": "2001"}]}
            bridge.source_accounts = {"fixture": {"bot_id": "1001"}}
            bridge.skip_outcomes = {}
            for kind, expected in (("image", "media_or_non_text"), ("reply", "quoted_or_reply")):
                sample = await event(host, "skipped text")
                sample.message_obj.raw_message["message"].append({"type": kind, "data": {}})
                assert native_header(sample, Plain, adapter=host) is None
                bridge.record_skip(sample, queue)
                assert bridge.skip_status("fixture", "2001")["code"] == expected
            assert queue.summary(incoming.scope(queue.deployment_id).id)["counts"] == {}
            from astrbot.api.message_components import Reply

            sample = await event(host, "read existing memories", mid=40)
            sample.message_obj.raw_message["message"].insert(0, {"type": "reply", "data": {"id": "9"}})
            sample.message_obj.message.insert(0, Reply(id="9", chain=[Plain("quoted content")]))
            assert native_header(sample, Plain, adapter=host) is None
            with patch.object(host.get_client(), "call_action") as platform_io:
                readonly = native_header(sample, Plain, adapter=host, allow_readonly=True)
            platform_io.assert_not_called()
            assert readonly and readonly.capture_allowed is False and readonly.text == "read existing memories"
            assert bridge.snapshot(sample) is None
            scope = queue.scope(incoming)
            save_reference_options(queue, scope, **{
                **reference_options(queue, scope), "recall_on_quoted_or_link": True,
            })
            assert bridge.snapshot(sample) == readonly
            sample.message_obj.raw_message["message"].append({"type": "image", "data": {"file": "synthetic"}})
            assert native_header(sample, Plain, adapter=host, allow_readonly=True) is None
            after_grant_event = await event(host, "new authorized", mid=4)
            after_grant = bridge.snapshot(after_grant_event)
            assert after_grant.received_at == after_grant_event.created_at
            command_event = await event(host, "/hb pause", mid=5)
            command_event.message_str = "hb pause"
            command_event.is_at_or_wake_command = True
            assert bridge.authorized_source(command_event).text == "/hb pause"
            assert bridge.snapshot(command_event) is None
            capture = queue.capture(after_grant, "conversation")
            duplicate = queue.capture(bridge.snapshot(after_grant_event), "conversation")
            assert not duplicate.created and duplicate.task_id == capture.task_id
            frozen = dict(queue.get(capture.task_id))
            current = bridge.source_header(await event(host, "new", bot=1002, mid=3))
            queue.access.observe_bot("fixture", "aiocqhttp", "1002")
            grant = queue.access.authorization("fixture", "2001")
            queue.activate(current, grant["revision"])
            bridge.source_accounts["fixture"]["bot_id"] = "1002"
            assert bridge.snapshot(await event(host, "new", bot=1002, mid=6))
            assert bridge.snapshot(old) is None
            old_again = bridge.source_header(old)
            assert old_again == incoming
            assert not queue.accepts(old_again)
            assert not queue.current(frozen)
            assert queue.current(frozen, writing=False)
            assert queue.get(capture.task_id)["payload"] == frozen["payload"]
        finally:
            queue.close()
        baseline = time.time()
        with patch.object(time, "time", return_value=baseline):
            queue = Outbox(
                Path(temporary) / "rollback-runtime", "fixture", "http://memory.test",
                credential_fingerprint="fixture",
            )
            queue.sync_permissions([("fixture", "2001")])
            queue.activate(incoming, queue.access.authorization("fixture", "2001")["revision"])
        try:
            with patch.object(time, "time", return_value=baseline + 0.2):
                older = await event(host, "queued before control", mid=20)
                older.message_obj.raw_message["time"] = int(baseline) + 3
                older_command = await event(host, "/hb pause", mid=21)
                older_command.message_obj.raw_message["time"] = int(baseline) + 3
            with patch.object(time, "time", return_value=baseline + 0.3):
                newer = await event(host, "/hb on", mid=22)
                newer.message_obj.raw_message["time"] = int(baseline) + 2
            with patch.object(time, "time", return_value=baseline - 0.1):
                header = native_header(newer, Plain)
                old_text = native_header(older, Plain, adapter=host)
                old_command = native_header(older_command, Plain)
                assert header and old_text and old_command
                scope = queue.scope(header)
                command = queue.access.command(
                    header, header.scope(queue.deployment_id), scope, "mode", ("active",),
                )
                queue.access.run(command, scope, lambda: queue.set_mode(
                    scope["scope_id"], "active", journal_only=True,
                ))
                queue.apply_control()
                next_scope = queue.scope(header)
                next_command = queue.access.command(
                    old_command, old_command.scope(queue.deployment_id), next_scope, "mode", ("paused",),
                )
                queue.access.run(next_command, next_scope, lambda: queue.set_mode(
                    next_scope["scope_id"], "paused", journal_only=True,
                ))
                queue.apply_control()
                assert queue.scope(header)["mode"] == "paused"
        finally:
            queue.close()
        queue = Outbox(
            Path(temporary) / "controls-runtime", "fixture", "http://memory.test",
            credential_fingerprint="fixture",
        )
        try:
            queue.sync_permissions([("fixture", "2001")])
            queue.activate(incoming, queue.access.authorization("fixture", "2001")["revision"])
            queued_command = native_header(await event(host, "/hb on", mid=10), Plain)
            queued_message = native_header(await event(host, "queued text", mid=11), Plain, adapter=host)
            scope = queue.scope(queued_command)
            prepared = queue.access.command(
                queued_command, queued_command.scope(queue.deployment_id), scope, "mode", ("active",),
            )
            queue.forget(scope["scope_id"])
            try:
                queue.access.run(prepared, scope, lambda: None)
            except QueueError as error:
                assert str(error) == "command_expired"
            else:
                raise AssertionError("prepared command crossed erasure generation")
            queue.set_mode(scope["scope_id"], "active")
            assert not queue.accepts(queued_message)
        finally:
            queue.close()
    print(json.dumps({
        "astrbot": VERSION, "evidence_layer": "actual_host_objects_synthetic_payloads",
        "real_qq_login": False,
        "passed": [
            "actual_onebot_adapter_conversion_and_event_client",
            "native_raw_event_required_and_ambiguous_api_login_rejected",
            "host_public_created_at_and_pre_authorization_event_rejection",
            "actual_host_command_prepared_before_erasure_cannot_change_new_generation",
            "actual_host_queued_text_cannot_enter_resumed_new_generation",
            "host_removed_wake_prefix_keeps_original_command_id_time_text",
            "queued_event_from_replaced_client_rejected",
            "queued_event_with_original_recipient_rejected_after_rebinding",
            "frozen_old_bank_and_operation_preserved",
            "host_clock_rollback_does_not_reject_new_command_message_keys",
        ],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
