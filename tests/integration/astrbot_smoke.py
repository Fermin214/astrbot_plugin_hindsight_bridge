"""Real AstrBot manager/request/history smoke; only the memory HTTP service is a fixture."""

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiocqhttp import Event

ROOT = Path(os.environ["ASTRBOT_ROOT"])
if not ROOT.is_absolute() or (ROOT.exists() and any(ROOT.iterdir())):
    raise RuntimeError("ASTRBOT_ROOT must be a dedicated, empty test directory")
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
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.context import Context
from astrbot.core.star.star_manager import PluginManager
from astrbot.core.utils.metrics import Metric

NAME = "astrbot_plugin_hindsight_bridge"
passed = []
banks = set()
operations = {}
payloads = []


async def service(request):
    if request.headers.get("Authorization") != "Bearer delivery-key":
        return web.json_response({}, status=401)
    path = request.path
    if path == "/v1/default/banks":
        return web.json_response({"banks": []})
    bank = path.split("/")[4]
    if request.method == "PUT":
        banks.add(bank)
        return web.json_response({})
    if path.endswith("/config"):
        return web.json_response({}, status=200 if bank in banks else 404)
    if "/operations/" in path:
        oid = path.rsplit("/", 1)[1]
        return web.json_response({"operation_id": oid, "status": operations.get(oid, "not_found"), "child_operations": None})
    if path.endswith("/memories") and request.method == "POST":
        body = await request.json()
        payloads.append(body)
        operations[body["operation_id"]] = "completed"
        return web.json_response({"success": True, "async": True, "operation_id": body["operation_id"]})
    if path.endswith("/recall"):
        return web.json_response({"results": [{"id": "fixture-fact", "text": "A remembered preference", "type": "world"}]})
    if path.endswith("/mental-models/mm-background"):
        return web.json_response({
            "id": "mm-background", "bank_id": bank, "is_knowledge_page": True,
            "content": "Native background smoke marker.", "is_stale": None,
            "last_refreshed_at": "2026-11-04T00:00:00+00:00",
        })
    raise AssertionError((request.method, path))


async def wait_for(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(0.05)


async def message(adapter, message_id=1):
    raw = Event({
        "post_type": "message", "message_type": "private", "sub_type": "friend", "self_id": 456,
        "user_id": 123, "message_id": message_id, "time": int(time.time()) + 1,
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello", "sender": {"user_id": 123, "nickname": "Delivery fixture"}, "font": 0,
    })
    return adapter.create_event(await adapter.convert_message(raw))


async def main():
    assert VERSION == os.environ.get("ASTRBOT_TEST_VERSION", "4.29.1"), VERSION
    await db_helper.initialize()
    conversations = ConversationManager(db_helper)
    providers = SimpleNamespace(provider_insts=[], embedding_provider_insts=[], rerank_provider_insts=[],
                                tts_provider_insts=[], stt_provider_insts=[], inst_map={}, llm_tools=FunctionToolManager())
    adapter = AiocqhttpAdapter({
        "id": "delivery-qq", "type": "aiocqhttp", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0,
    }, {}, asyncio.Queue())
    adapter.bot.get_login_info = AsyncMock(return_value={"user_id": 456})
    astrbot_config["platform"] = [{"id": "delivery-qq", "type": "aiocqhttp"}]
    context = Context(asyncio.Queue(), astrbot_config, db_helper, providers,
                      SimpleNamespace(platform_insts=[adapter], get_insts=lambda: [adapter]), conversations,
                      None, None, SimpleNamespace(get_conf=lambda _: astrbot_config), None, None)
    manager = PluginManager(context, astrbot_config)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", service)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        with patch.object(Metric, "upload", AsyncMock()):
            from package_fixture import install_candidate, verify_installed_candidate
            await install_candidate(manager, "candidate-install.zip")
            metadata = context.get_registered_star(NAME)
            assert metadata and metadata.star_cls and not manager.failed_plugin_dict
            plugin = metadata.star_cls
            candidate_sha256 = verify_installed_candidate(manager)
            assert metadata.config["recall_timeout"] == 10
            assert plugin.outbox is None
            assert len([row for row in context.registered_web_apis if NAME in row[0]]) == 10
            passed.append("official_archive_install_and_empty_config")
            metadata.config.update(api_url=f"http://127.0.0.1:{port}", api_key="delivery-key", service_id="delivery-service",
                                   allowed_accounts=[{"platform_instance_id": "delivery-qq", "user_id": "123"}])
            metadata.config.save_config()
            result = await manager.reload(NAME)
            assert result[0], result
            assert plugin.panel.closed and plugin.client is None and plugin.connection_task is None
            plugin = context.get_registered_star(NAME).star_cls
            assert plugin.outbox and plugin.client, plugin.status
            await wait_for(lambda: not plugin.outbox.recovering and plugin.client.connection["connected"] is True)
            passed.append("official_reload_and_background_auth")
            from data.plugins.astrbot_plugin_hindsight_bridge.core.source import native_header
            from data.plugins.astrbot_plugin_hindsight_bridge.core.background import save_reference

            authorization_event = await message(adapter)
            header = native_header(authorization_event, Plain)
            grant = plugin.outbox.access.authorization("delivery-qq", "123")
            plugin.outbox.activate(header, grant["revision"])
            event = await message(adapter, 2)
            cid = await conversations.new_conversation(event.unified_msg_origin)
            conversation = await conversations.get_conversation(event.unified_msg_origin, cid)
            request = ProviderRequest(prompt="hello", conversation=conversation)
            await plugin.observe_incoming(event)
            assert event in plugin.incoming
            await plugin.add_memory(event, request)
            assert len(request.extra_user_content_parts) == 1
            assert "Hindsight" in request.extra_user_content_parts[0].text
            wire = await request.assemble_context()
            assert "Hindsight" in json.dumps(wire)
            stored = dump_messages_with_checkpoints(bind_checkpoint_messages([wire]))
            assert "Hindsight" not in json.dumps(stored), stored
            await conversations.update_conversation(event.unified_msg_origin, cid, history=stored)
            persisted = await conversations.get_conversation(event.unified_msg_origin, cid)
            assert "Hindsight" not in persisted.history
            passed.append("real_onebot_event_and_temporary_provider_content")
            await wait_for(lambda: plugin.outbox.db.execute("SELECT COUNT(*) FROM outbox WHERE state='done'").fetchone()[0] == 1)
            assert payloads and "Hindsight" not in json.dumps(payloads)
            passed.append("durable_worker_over_real_http")
            scope = plugin.outbox.list_scopes()[0]
            save_reference(plugin.outbox, scope, revision=0, page_id="kp-background", model_id="mm-background", enabled=True)
            event = await message(adapter, 3)
            cid = await conversations.new_conversation(event.unified_msg_origin)
            conversation = await conversations.get_conversation(event.unified_msg_origin, cid)
            request = ProviderRequest(prompt="hello", conversation=conversation)
            await plugin.observe_incoming(event)
            await plugin.add_memory(event, request)
            wire = await request.assemble_context()
            assert "Native background smoke marker." in json.dumps(wire)
            stored = dump_messages_with_checkpoints(bind_checkpoint_messages([wire]))
            assert "Native background smoke marker." not in json.dumps(stored)
            await conversations.update_conversation(event.unified_msg_origin, cid, history=stored)
            await wait_for(lambda: plugin.outbox.db.execute("SELECT COUNT(*) FROM outbox WHERE state='done'").fetchone()[0] == 2)
            assert "Native background smoke marker." not in json.dumps(payloads)
            passed.append("selected_background_is_real_host_temporary_content_not_history_or_retain")
            old_bank = plugin.outbox.list_scopes()[0]["bank"]
            snapshot = await plugin.panel.bootstrap()
            assert snapshot["scopes"][0]["summary"]["recall"]["outcome"] == "success"
            old = plugin
            metadata = context.get_registered_star(NAME)
            metadata.config["recall_timeout"] = 4
            metadata.config.save_config()
            await install_candidate(manager, "candidate-upgrade.zip")
            assert verify_installed_candidate(manager) == candidate_sha256
            plugin = context.get_registered_star(NAME).star_cls
            assert old.panel.closed and old.client is None
            assert plugin.config["recall_timeout"] == 4
            assert plugin.outbox.list_scopes()[0]["bank"] == old_bank
            from data.plugins.astrbot_plugin_hindsight_bridge.core.background import reference

            assert reference(plugin.outbox, plugin.outbox.list_scopes()[0])["enabled"]
            passed.append("official_archive_upgrade_preserves_config_and_bank")
            await manager.uninstall_plugin(NAME)
            assert context.get_registered_star(NAME) is None
            assert plugin.client is None and plugin.outbox is None and plugin.connection_task is None
            passed.append("official_uninstall_closes_owned_resources")
    finally:
        current = context.get_registered_star(NAME)
        if current and current.star_cls:
            await current.star_cls.terminate()
        await server.cleanup()
        await db_helper.engine.dispose()
    inventory = json.loads(Path("/delivery/package-inventory.json").read_text())
    report = {"astrbot": VERSION, "memory_backend": "local HTTP fixture, not real Hindsight",
              "candidate_sha256": candidate_sha256,
              "platform_peer": "synthetic OneBot payloads and login fixture; no real QQ account",
              "candidate_tree_sha256": inventory["tree_sha256"], "passed": passed}
    Path("/delivery/astrbot-smoke-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


asyncio.run(main())
