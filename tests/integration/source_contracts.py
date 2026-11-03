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
from plugins.astrbot_plugin_hindsight_bridge.core.source import (
    native_header, native_snapshot, source_identity,
)


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
    incoming = native_snapshot(old, Plain)
    assert incoming and incoming.bot_id == "1001"
    assert source_identity(old, incoming, host)
    command_event = await event(host, "/hindsight pause", mid=2)
    command_event.message_str = "hindsight pause"
    command_event.is_at_or_wake_command = True
    header = native_header(command_event, Plain, ["/"])
    assert header and header.text == "/hindsight pause"
    assert native_snapshot(command_event, Plain) is None
    other_host = adapter("fixture")
    assert source_identity(old, incoming, other_host) is None
    with tempfile.TemporaryDirectory() as temporary:
        queue = Outbox(Path(temporary) / "runtime", "fixture", "http://memory.test", credential_fingerprint="fixture")
        try:
            queue.sync_permissions([("fixture", "2001")])
            grant = queue.access.authorization("fixture", "2001")
            queue.activate(incoming, grant["revision"])
            assert not queue.accepts(incoming)
            after_grant_event = await event(host, "new authorized", mid=4)
            after_grant = native_snapshot(after_grant_event, Plain)
            assert after_grant.received_at == after_grant_event.created_at
            capture = queue.capture(after_grant, "conversation")
            frozen = dict(queue.get(capture.task_id))
            current = native_snapshot(await event(host, "new", bot=1002, mid=3), Plain)
            queue.access.observe_bot("fixture", "aiocqhttp", "1002")
            grant = queue.access.authorization("fixture", "2001")
            queue.activate(current, grant["revision"])
            # First Bridge observation of an old host event cannot assign it the new bot.
            old_again = native_snapshot(old, Plain)
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
                older_command = await event(host, "/hindsight pause", mid=21)
                older_command.message_obj.raw_message["time"] = int(baseline) + 3
            with patch.object(time, "time", return_value=baseline + 0.3):
                newer = await event(host, "/hindsight on", mid=22)
                newer.message_obj.raw_message["time"] = int(baseline) + 2
            with patch.object(time, "time", return_value=baseline - 0.1):
                header = native_header(newer, Plain)
                old_text = native_snapshot(older, Plain)
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
                assert not queue.accepts(old_text)
                try:
                    queue.access.command(
                        old_command, old_command.scope(queue.deployment_id),
                        queue.scope(header), "mode", ("paused",),
                    )
                except QueueError as error:
                    assert str(error) == "command_expired"
                else:
                    raise AssertionError("clock rollback lost observed host queue high-water mark")
        finally:
            queue.close()
        queue = Outbox(
            Path(temporary) / "controls-runtime", "fixture", "http://memory.test",
            credential_fingerprint="fixture",
        )
        try:
            queue.sync_permissions([("fixture", "2001")])
            queue.activate(incoming, queue.access.authorization("fixture", "2001")["revision"])
            queued_command = native_header(await event(host, "/hindsight on", mid=10), Plain)
            queued_message = native_snapshot(await event(host, "queued text", mid=11), Plain)
            scope = queue.scope(queued_command)
            queue.set_mode(scope["scope_id"], "paused")
            for forget in (False, True):
                if forget:
                    queue.forget(scope["scope_id"])
                try:
                    queue.access.command(
                        queued_command, queued_command.scope(queue.deployment_id),
                        queue.scope_owner(scope["scope_id"]), "mode", ("active",),
                    )
                except QueueError as error:
                    assert str(error) == "command_expired"
                else:
                    raise AssertionError("host queued command crossed a later control boundary")
            queue.set_mode(scope["scope_id"], "active")
            assert not queue.accepts(queued_message)
        finally:
            queue.close()
    print(json.dumps({
        "astrbot": VERSION, "evidence_layer": "actual_host_objects_synthetic_payloads",
        "real_qq_login": False,
        "passed": [
            "actual_onebot_adapter_conversion_and_event_client",
            "host_public_created_at_and_pre_authorization_event_rejection",
            "actual_host_queued_commands_cannot_override_panel_pause_or_forget",
            "actual_host_queued_text_cannot_enter_resumed_new_generation",
            "host_removed_wake_prefix_keeps_original_command_id_time_text",
            "queued_event_from_replaced_client_rejected",
            "queued_event_with_original_recipient_rejected_after_rebinding",
            "frozen_old_bank_and_operation_preserved",
            "host_clock_rollback_preserves_observed_command_and_ingress_boundaries",
        ],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
