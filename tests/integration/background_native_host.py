"""Real AstrBot and Hindsight, synthetic OneBot/model peers; no account login."""

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from package_fixture import candidate_manifest, install_candidate, verify_installed_candidate

from aiocqhttp import Event

ROOT = Path(os.environ["ASTRBOT_ROOT"])
if not ROOT.is_absolute() or (ROOT.exists() and any(ROOT.iterdir())):
    raise RuntimeError("ASTRBOT_ROOT must be dedicated and empty")
for folder in (ROOT / "data", ROOT / "data/plugins", ROOT / "data/config"):
    folder.mkdir(parents=True, exist_ok=True)
for file in (ROOT / "data/__init__.py", ROOT / "data/plugins/__init__.py"):
    file.touch()
sys.path.insert(0, str(ROOT))

from astrbot.core import astrbot_config, db_helper
from astrbot.core.agent.message import bind_checkpoint_messages, dump_messages_with_checkpoints
from astrbot.core.config.default import VERSION
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.message.components import Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.context import Context
from astrbot.core.star.star_manager import PluginManager
from astrbot.core.utils.metrics import Metric

NAME = "astrbot_plugin_hindsight_bridge"


def text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_values(item)


async def event(adapter, mid):
    raw = Event({
        "post_type": "message", "message_type": "private", "sub_type": "friend", "self_id": 456,
        "user_id": 123, "message_id": mid, "time": int(time.time()) + 1,
        "message": [{"type": "text", "data": {"text": "An unrelated new conversation."}}],
        "raw_message": "An unrelated new conversation.", "sender": {"user_id": 123}, "font": 0,
    })
    return adapter.create_event(await adapter.convert_message(raw))


async def main():
    assert VERSION == os.environ["ASTRBOT_TEST_VERSION"]
    await db_helper.initialize()
    conversations = ConversationManager(db_helper)
    providers = SimpleNamespace(provider_insts=[], embedding_provider_insts=[], rerank_provider_insts=[],
                                tts_provider_insts=[], stt_provider_insts=[], inst_map={}, llm_tools=FunctionToolManager())
    adapter = AiocqhttpAdapter({
        "id": "background-fixture", "type": "aiocqhttp", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0,
    }, {}, asyncio.Queue())
    adapter.bot.get_login_info = AsyncMock(return_value={"user_id": 456})
    astrbot_config["platform"] = [{"id": "background-fixture", "type": "aiocqhttp"}]
    context = Context(asyncio.Queue(), astrbot_config, db_helper, providers,
                      SimpleNamespace(platform_insts=[adapter], get_insts=lambda: [adapter]), conversations,
                      None, None, SimpleNamespace(get_conf=lambda _: astrbot_config), None, None)
    manager = PluginManager(context, astrbot_config)
    checks = []
    plugin = None
    try:
        with patch.object(Metric, "upload", AsyncMock()):
            assert candidate_manifest()["package_kind"] == "distribution"
            await install_candidate(manager, "candidate-install.zip")
            metadata = context.get_registered_star(NAME)
            assert metadata and not manager.failed_plugin_dict
            candidate_sha256 = verify_installed_candidate(manager)
            metadata.config.update(
                api_url=os.environ["BACKGROUND_API_URL"], api_key=os.environ["BACKGROUND_API_KEY"],
                service_id=os.environ["BACKGROUND_OWNER"], recall_timeout=10,
                allowed_accounts=[{"platform_instance_id": "background-fixture", "user_id": "123"}],
            )
            metadata.config.save_config()
            assert (await manager.reload(NAME))[0]
            plugin = context.get_registered_star(NAME).star_cls
            async with asyncio.timeout(30):
                while plugin.outbox.recovering:
                    await asyncio.sleep(0.1)
            from data.plugins.astrbot_plugin_hindsight_bridge.core.source import native_header
            from data.plugins.astrbot_plugin_hindsight_bridge.core.background import reference, save_reference

            header = native_header(await event(adapter, 1), Plain)
            plugin.outbox.activate(header, plugin.outbox.access.authorization("background-fixture", "123")["revision"])
            scope = plugin.outbox.scope(header)
            plugin.outbox.set_mode(scope["scope_id"], "readonly")
            scope = plugin.outbox.scope_owner(scope["scope_id"])
            bank = scope["bank"]
            await plugin.client.create_bank(bank)
            await plugin.client.request("PATCH", plugin.client.bank_path(bank) + "/config", body={"updates": {
                "retain_extraction_mode": "chunks", "enable_observations": False,
            }})
            retained = await plugin.client.request("POST", plugin.client.bank_path(bank) + "/memories", body={
                "items": [{"content": "Native generated background marker.", "document_id": "native-fixture-source"}],
                "async": True,
            })
            async with asyncio.timeout(60):
                while (await plugin.client.operation(bank, retained["operation_id"])).status != "completed":
                    await asyncio.sleep(0.5)
            created = await plugin.client.request("POST", plugin.client.bank_path(bank) + "/knowledge-base/pages", body={
                "name": "Background", "source_query": "Return a brief background.", "max_tokens": 256,
                "trigger": {"refresh_after_consolidation": False, "fact_types": ["world"]},
            })
            async with asyncio.timeout(60):
                while (await plugin.client.operation(bank, created["operation_id"])).status != "completed":
                    await asyncio.sleep(0.5)
            page = await plugin.client.background_page(bank, created["mental_model_id"])
            save_reference(plugin.outbox, scope, revision=0, page_id=created["page_id"],
                           model_id=created["mental_model_id"], enabled=True)
            incoming = await event(adapter, 2)
            cid = await conversations.new_conversation(incoming.unified_msg_origin)
            conversation = await conversations.get_conversation(incoming.unified_msg_origin, cid)
            request = ProviderRequest(prompt="An unrelated new conversation.", conversation=conversation)
            await plugin.observe_incoming(incoming)
            await plugin.add_memory(incoming, request)
            wire = await request.assemble_context()
            assert any(page["content"] in text for text in text_values(wire))
            assert "Native generated background marker." in json.dumps(wire)
            stored = dump_messages_with_checkpoints(bind_checkpoint_messages([wire]))
            assert "Native generated background marker." not in json.dumps(stored)
            await conversations.update_conversation(incoming.unified_msg_origin, cid, history=stored)
            assert plugin.outbox.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
            checks.append("installed_real_host_uses_real_native_page_in_temp_request_without_capture_or_history")
            replies = [result async for result in plugin.memory_background(await event(adapter, 3))]
            assert any("Native generated background marker." in part.text
                       for result in replies for part in result.chain if isinstance(part, Plain))
            checks.append("personal_read_uses_real_native_page")
            selection = reference(plugin.outbox, scope)
            closed = save_reference(plugin.outbox, scope, revision=selection["revision"],
                                    page_id=created["page_id"], model_id=created["mental_model_id"], enabled=False)
            close_event = await event(adapter, 20)
            close_cid = await conversations.new_conversation(close_event.unified_msg_origin)
            close_conversation = await conversations.get_conversation(close_event.unified_msg_origin, close_cid)
            close_request = ProviderRequest(prompt="An unrelated new conversation.", conversation=close_conversation)
            await plugin.observe_incoming(close_event)
            with (
                patch.object(plugin.client, "recall", wraps=plugin.client.recall) as recall_read,
                patch.object(plugin.client, "background_page", wraps=plugin.client.background_page) as page_read,
            ):
                await plugin.add_memory(close_event, close_request)
                recall_read.assert_awaited_once()
                page_read.assert_not_awaited()
            assert len(close_request.extra_user_content_parts) == 1
            close_wire = await close_request.assemble_context()
            assert not any("Hindsight \u80cc\u666f\u53c2\u8003" in text for text in text_values(close_wire))
            assert (await plugin.client.background_page(bank, created["mental_model_id"]))["content"] == page["content"]
            save_reference(plugin.outbox, scope, revision=closed["revision"], page_id=created["page_id"],
                           model_id=created["mental_model_id"], enabled=True)
            checks.append("closing_stops_native_background_injection_without_deleting_page")
            old = plugin
            await install_candidate(manager, "candidate-upgrade.zip")
            assert verify_installed_candidate(manager) == candidate_sha256
            plugin = context.get_registered_star(NAME).star_cls
            async with asyncio.timeout(30):
                while plugin.outbox.recovering:
                    await asyncio.sleep(0.1)
            assert old.client is None and old.outbox is None
            scope = plugin.outbox.scope_owner(scope["scope_id"])
            assert scope["bank"] == bank and reference(plugin.outbox, scope)["enabled"]
            checks.append("native_upgrade_preserves_background_reference_and_bank")
            plugin.worker.maintenance.begin(scope["scope_id"])
            state = await plugin.worker.maintenance.status(scope["scope_id"])
            assert state["ready"], state
            blocked = [str(result) async for result in plugin.memory_background(await event(adapter, 4))]
            assert "Native generated background marker." not in json.dumps(blocked)
            await plugin.worker.maintenance.resume(scope["scope_id"])
            checks.append("maintenance_drains_native_refresh_and_blocks_personal_read")
            scope = plugin.outbox.scope_owner(scope["scope_id"])
            plugin.outbox.forget(scope["scope_id"])
            async with asyncio.timeout(60):
                while not await plugin.worker.maintenance.check(force=True):
                    await asyncio.sleep(0.5)
            latest = plugin.outbox.scope_owner(scope["scope_id"])
            assert latest["bank"] != bank and latest["mode"] == "paused"
            assert not reference(plugin.outbox, latest)["page_id"]
            assert not await plugin.client.bank_exists(bank)
            assert not await plugin.client.bank_exists(latest["bank"])
            persisted = await conversations.get_conversation(incoming.unified_msg_origin, cid)
            assert persisted.history
            checks.append("real_forget_deletes_native_bank_pages_and_clears_selection_without_deleting_host_history")
            await manager.reload(NAME)
            plugin = context.get_registered_star(NAME).star_cls
            async with asyncio.timeout(60):
                while plugin.outbox.recovering:
                    await asyncio.sleep(0.5)
            latest = plugin.outbox.scope_owner(scope["scope_id"])
            assert not reference(plugin.outbox, latest)["page_id"]
            assert not await plugin.client.bank_exists(bank)
            checks.append("reload_cannot_revive_erased_selection_or_old_bank")
            await manager.uninstall_plugin(NAME)
            plugin = None
    finally:
        if plugin:
            await plugin.terminate()
        await db_helper.engine.dispose()
    report = {"astrbot": VERSION, "hindsight": "1.1.0", "candidate_sha256": candidate_sha256, "passed": checks,
              "platform_peer": "synthetic actual OneBot SDK objects, no real QQ login",
              "model_peer": "synthetic OpenAI-compatible fixture, not a production model", "complete": True}
    Path("/delivery/background-native-host-report-" + VERSION + ".json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


asyncio.run(main())
