"""AstrBot hooks, personal memory controls, and durable erasure recovery."""

import asyncio
import secrets
import sqlite3
import tempfile
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .core.catalog import export_bytes, render_memory, render_page
from .core.client import HindsightClient, HindsightError
from .core.dialogue import (
    appended_history,
    final_reply,
    history_boundary,
    history_messages,
    persistent_messages,
)
from .core.identity import Incoming, sha256
from .core.materials import encoding, memory_block, recall_query
from .core.outbox import Outbox, QueueError
from .core.permissions import pairs
from .core.panel import MemoryPanel
from .core.source import local_runner, native_snapshot
from .core.worker import Worker


class HindsightBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.outbox = self.client = self.worker = self.task = None
        self.incoming = weakref.WeakKeyDictionary()
        self.handled = weakref.WeakSet()
        self.turns = weakref.WeakKeyDictionary()
        self.history_tasks = set()
        self.status = "未配置"
        self.last_warning = 0.0
        self.stopping = False
        self.permissions = None
        self.permission_revision = 0
        self.hooks = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.confirmations = {}
        self.panel = MemoryPanel(self)

    @staticmethod
    def diagnostic(code):
        explanations = {
            "http_401": "API Key 无效，请检查服务与插件凭据",
            "http_403": "服务拒绝访问，请检查 API Key 权限",
            "server_auth_not_enforced": "后端未启用认证，请配置服务 API Key",
            "connection_error": "无法连接后端，请检查地址、网络和容器服务名",
            "timeout": "后端响应超时",
            "recall_timeout": "检索超过时限，聊天继续",
            "queue_capacity_reached": "本地队列已满，请恢复连接并处理积压",
            "accepted_operation_missing": "已接收任务的远端记录缺失，请核对后端恢复点",
            "payload_digest_conflict": "冻结材料摘要不一致，请检查业务库",
            "scope_not_active": "请先用 /hindsight on 开启采集",
            "scope_not_available": "记忆范围正在恢复或已变更，请稍后重试",
            "message_before_accept_from": "消息早于采集时间下限",
            "invalid_correction": "更正格式无效；新内容最多 1000 字符",
            "invalid_capture": "内容为空或超出长度限制",
            "assistant_history_changed": "已保存的助手正文发生变化，请检查会话编辑记录",
            "conversation_history_not_saved": "助手正文尚未保存到会话历史，下一条消息继续核对",
            "http_404": "本人范围内没有这条记忆",
            "export_too_large": "导出超出容量限制，请由管理员使用后端数据导出",
            "export_limit_exceeded": "记忆超过导出数量上限，请由管理员使用后端数据导出",
            "export_changed": "导出期间记忆发生变化，请稍后重试",
            "catalog_changed": "查看期间记忆发生变化，请重新查看该页",
            "export_source_missing": "记忆来源不完整，请检查后端数据与保留策略",
            "invalid_identifier": "记忆 ID 无效，请从 view 列表复制完整 ID",
            "local_OperationalError": "本地数据库不可写，请检查磁盘空间与目录权限",
        }
        if not code:
            return "无"
        return explanations.get(code, "处理失败，请检查服务与数据目录") + f"（{code}）"

    async def initialize(self):
        url = str(self.config.get("api_url", "")).strip()
        key = str(self.config.get("api_key", "")).strip()
        service = str(self.config.get("service_id", "")).strip()
        if not url or not key or not service:
            self.status = "请在设置中连接记忆服务"
            return
        try:
            tokenizer = str(self.config.get("query_tokenizer", "o200k_base"))
            zone = str(self.config.get("timezone") or "UTC")
            ZoneInfo(zone)
            await asyncio.to_thread(encoding, tokenizer)
            self.client = HindsightClient(url, key, tokenizer=tokenizer)
            self.outbox = Outbox(
                Path(StarTools.get_data_dir("astrbot_plugin_hindsight_bridge")),
                service,
                self.client.url,
                credential_fingerprint=sha256(key),
                address_revision=int(self.config.get("service_address_revision", 1)),
                zone=zone,
                max_tasks=int(self.config.get("queue_max_tasks", 10000)),
                max_bytes=int(self.config.get("queue_max_mib", 64)) * 1024 * 1024,
                control_directory=self.control_directory(),
                agent_id=str(self.config.get("agent_id", "default")),
            )
            self.refresh_permissions()
            self.outbox.recovering = True
            self.worker = Worker(
                self.outbox,
                self.client,
                logger,
                refresh_permissions=self.refresh_permissions,
            )
            self.task = asyncio.create_task(self.worker.run(), name="hindsight-outbox")
            self.status = "已配置"
        except Exception as exc:  # noqa: BLE001 -- plugin initialization must preserve chat
            self.status = "初始化失败：" + (
                str(exc) if isinstance(exc, QueueError) else type(exc).__name__
            )
            logger.error("Hindsight: %s", self.status)
            await self._close_resources()

    def control_directory(self):
        path = str(self.config.get("control_state_dir", "")).strip()
        if not path:
            return Path(
                StarTools.get_data_dir("astrbot_plugin_hindsight_bridge_control")
            )
        directory = Path(path).expanduser()
        if not directory.is_absolute():
            raise QueueError("control_directory_must_be_absolute")
        return directory

    def refresh_permissions(self):
        try:
            accounts = pairs(self.config.get("allowed_accounts", []))
        except QueueError as exc:
            self.warning(str(exc))
            accounts = frozenset()
        if accounts != self.permissions:
            self.outbox.sync_permissions(accounts)
            self.permissions = accounts
            self.permission_revision += 1

    def allowed(self, event):
        try:
            accounts = pairs(self.config.get("allowed_accounts", []))
        except ValueError:
            return False
        return (
            not self.stopping
            and event.is_private_chat()
            and not event.get_group_id()
            and event.get_platform_name() == "aiocqhttp"
            and (event.get_platform_id(), event.get_sender_id()) in accounts
        )

    def snapshot(self, event):
        if not self.allowed(event):
            return None
        prefixes = ("/", *self.config.get("command_prefixes", []))
        return native_snapshot(event, Plain, prefixes)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def observe_incoming(self, event: AstrMessageEvent):
        if self.outbox is not None and event not in self.incoming:
            incoming = self.snapshot(event)
            if incoming:
                self.incoming[event] = incoming

    def warning(self, label):
        self.status = label
        if time.monotonic() - self.last_warning > 60:
            logger.warning("Hindsight: %s", label)
            self.last_warning = time.monotonic()

    async def recall_text(self, bank, current, history=(), *, timestamp, scope=None):
        timeout = max(0.2, min(4.0, float(self.config.get("recall_timeout", 4))))
        budget = max(32, min(400, int(self.config.get("recall_query_tokens", 380))))
        result_tokens = max(
            256, min(1200, int(self.config.get("recall_max_tokens", 1200)))
        )
        limit = max(256, min(4000, int(self.config.get("context_max_chars", 4000))))
        async with asyncio.timeout(timeout):
            query = recall_query(
                current,
                history,
                budget=budget,
                name=self.client.tokenizer,
            )
            results = await self.client.recall(
                bank,
                query,
                result_tokens,
                timestamp=timestamp,
            )
            return memory_block(
                results,
                limit=limit,
                corrections=self.outbox.corrections(scope) if scope else (),
            )

    async def conversation_history(self, event, conversation_id):
        manager = self.context.conversation_manager
        if not hasattr(manager, "get_conversation"):
            return None
        try:
            async with asyncio.timeout(0.5):
                conversation = await manager.get_conversation(
                    event.unified_msg_origin,
                    conversation_id,
                    create_if_not_exists=False,
                )
        except Exception as exc:  # noqa: BLE001 -- user capture remains independent
            self.warning("history_" + type(exc).__name__)
            return None
        if conversation is None:
            return []
        if (
            str(conversation.cid) != conversation_id
            or conversation.user_id != event.unified_msg_origin
        ):
            return None
        return history_messages(conversation)

    def record_history(self, anchor, rows, expected=None):
        if not anchor or anchor["reply_task_id"]:
            return False
        added = appended_history(rows, anchor["history_before"])
        if not added:
            return False
        text = final_reply(added, anchor["text"])
        if not text or (expected is not None and text != expected):
            return False
        captured = self.outbox.capture_assistant(anchor["task_id"], text)
        if captured.created:
            self.outbox.release(captured.task_id)
            self.worker.wake.set()
        return True

    @filter.on_agent_done()
    async def remember_assistant(self, event, run_context, response):
        turn = self.turns.pop(event, None)
        if (
            turn is None
            or self.stopping
            or response is None
            or response.role != "assistant"
            or len(self.history_tasks) >= 32
        ):
            return
        rows = persistent_messages(run_context.messages)
        expected = final_reply(rows, turn["incoming"].text)
        if not expected:
            return
        task = asyncio.create_task(
            self.persist_history(event, turn, expected), name="hindsight-history"
        )
        self.history_tasks.add(task)
        task.add_done_callback(self.history_tasks.discard)

    async def persist_history(self, event, turn, expected):
        try:
            async with self.using_resources(), asyncio.timeout(120):
                while self.allowed(event):
                    self.refresh_permissions()
                    scope = self.outbox.scope(turn["incoming"])
                    if (
                        not self.outbox.can_recall(scope)
                        or scope["mode"] != "active"
                        or scope["revision"] != turn["revision"]
                        or scope["bank"] != turn["bank"]
                        or self.permission_revision != turn["permission_revision"]
                    ):
                        return
                    rows = await self.conversation_history(event, turn["conversation"])
                    self.refresh_permissions()
                    if (
                        not self.allowed(event)
                        or not self.outbox.can_recall(scope)
                        or self.permission_revision != turn["permission_revision"]
                    ):
                        return
                    anchor = self.outbox.history_anchor(
                        scope["scope_id"], turn["conversation"]
                    )
                    if not anchor or anchor["task_id"] != turn["task_id"]:
                        return
                    if anchor["reply_task_id"] or self.record_history(
                        anchor, rows, expected
                    ):
                        return
                    await asyncio.sleep(
                        0.1 if time.monotonic() - turn["started"] < 2 else 1
                    )
        except TimeoutError:
            self.warning("conversation_history_not_saved")
        except QueueError as exc:
            if str(exc) != "scope_not_active":
                self.warning(str(exc))
        except Exception as exc:  # noqa: BLE001 -- history collection must preserve chat
            self.warning("history_" + type(exc).__name__)

    @filter.on_llm_request()
    async def add_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.outbox is None or self.client is None or event in self.handled:
            return
        incoming = self.incoming.pop(event, None)
        if (
            incoming is None
            or not self.allowed(event)
            or self.snapshot(event) != incoming
        ):
            return
        self.handled.add(event)
        if not local_runner(self.context.get_config(event.unified_msg_origin)):
            self.status = "执行器尚未适配"
            return
        self.hooks += 1
        self.idle.clear()
        capture = None
        try:
            self.refresh_permissions()
            permission_revision = self.permission_revision
            scope = self.outbox.scope(incoming)
            if not self.outbox.can_recall(scope):
                return
            async with asyncio.timeout(2):
                conversation = (
                    str(req.conversation.cid)
                    if req.conversation
                    else await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin
                    )
                )
                rows = (
                    await self.conversation_history(event, str(conversation))
                    if conversation
                    else []
                )
            if not conversation:
                self.warning("conversation_not_available")
                return
            self.refresh_permissions()
            if (
                not self.allowed(event)
                or not self.outbox.can_recall(scope)
                or self.permission_revision != permission_revision
            ):
                return
            # Commit original source, frozen request and operation ID before any recall I/O.
            if scope["mode"] == "active":
                self.record_history(
                    self.outbox.history_anchor(scope["scope_id"], str(conversation)),
                    rows,
                )
                capture = self.outbox.capture(
                    incoming, str(conversation), history_before=history_boundary(rows)
                )
                if not capture.created:
                    return
                if rows is not None:
                    self.turns[event] = {
                        "incoming": incoming,
                        "conversation": str(conversation),
                        "revision": scope["revision"],
                        "permission_revision": permission_revision,
                        "bank": scope["bank"],
                        "task_id": capture.task_id,
                        "started": time.monotonic(),
                    }
            memory = await self.recall_text(
                scope["bank"],
                incoming.text,
                [
                    ("用户" if m.role == "user" else "助手", m.text)
                    for m in capture.history
                ]
                if capture
                else (),
                timestamp=incoming.iso_timestamp,
                scope=scope,
            )
            self.refresh_permissions()
            if (
                memory
                and self.allowed(event)
                and self.outbox.can_recall(scope)
                and self.permission_revision == permission_revision
            ):
                req.extra_user_content_parts.append(
                    TextPart(text=memory).mark_as_temp()
                )
            self.status = "运行中"
        except HindsightError as exc:
            if exc.authentication:
                self.outbox.pause_connection(exc.code)
            self.warning(exc.code)
        except TimeoutError:
            self.warning("recall_timeout")
        except QueueError as exc:
            self.warning(str(exc))
        except Exception as exc:  # noqa: BLE001 -- local storage failures must preserve chat
            self.warning("local_" + type(exc).__name__)
        finally:
            if capture and capture.created and self.outbox is not None:
                try:
                    self.outbox.release(capture.task_id)
                    self.worker.wake.set()
                except Exception as exc:  # noqa: BLE001 -- the request remains durable
                    self.warning("queue_release_" + type(exc).__name__)
            self.hooks -= 1
            if not self.hooks:
                self.idle.set()

    @filter.command_group("hindsight")
    def hindsight(self):
        """个人记忆查看、导出、更正、模式、任务控制与完整遗忘。"""

    def command_scope(self, event):
        if self.outbox is None or not self.allowed(event):
            return None
        values = (
            event.get_platform_name(),
            event.get_platform_id(),
            event.get_self_id(),
            event.get_sender_id(),
        )
        if not all(isinstance(v, str) and v for v in values):
            return None
        raw = getattr(event.message_obj, "raw_message", None)
        if (
            not isinstance(raw, dict)
            or raw.get("post_type") != "message"
            or raw.get("message_type") != "private"
            or str(raw.get("self_id")) != values[2]
            or str(raw.get("user_id")) != values[3]
            or str(raw.get("message_id")) != str(event.message_obj.message_id)
            or values[2] == values[3]
        ):
            return None
        self.refresh_permissions()
        return self.outbox.scope(Incoming(*values, "", int(time.time()), ""))

    @asynccontextmanager
    async def using_resources(self):
        if self.stopping or not self.outbox:
            raise QueueError("plugin_stopping")
        self.hooks += 1
        self.idle.clear()
        try:
            yield
        finally:
            self.hooks -= 1
            if not self.hooks:
                self.idle.set()

    def readable(self, event, scope):
        return self.allowed(event) and self.outbox.can_recall(scope, manual=True)

    @hindsight.command("help")
    async def memory_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Hindsight Memory\n"
            "/hindsight status：连接、模式、队列与清理进度\n"
            "/hindsight view [页码]：查看本人记忆，每页 10 条\n"
            "/hindsight show 记忆ID：查看类型、时间与来源\n"
            "/hindsight recall 问题：检索本人记忆\n"
            "/hindsight export：导出 JSON 文件（事实、整合观察、源文档与更正）\n"
            "/hindsight correct 记忆ID 更正原文：提交更正，最多 1000 字符\n"
            "/hindsight on | readonly | pause：开启、只读、暂停\n"
            "/hindsight retry：重试失败任务\n"
            "/hindsight cancel 任务编号：取消上传，已形成的记忆保留\n"
            "/hindsight forget：查看完整遗忘范围并取得确认码\n"
            "自动记录用户原文和标准会话历史中的助手正文；助手正文不证明送达或操作执行。"
        )

    @hindsight.command("view")
    async def memory_view(self, event: AstrMessageEvent, page: int = 1):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        if not 1 <= page <= 1000:
            yield event.plain_result("页码须在 1 到 1000 之间。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result("记忆范围正在恢复，请稍后查看。")
                return
            try:
                async with asyncio.timeout(8):
                    await self.client.ensure_auth()
                    offset = (page - 1) * 10
                    data = await self.client.memory_page(scope["bank"], page=page)
                if not self.readable(event, scope):
                    yield event.plain_result("记忆范围已变化，请重新查看。")
                    return
                changes = len(self.outbox.corrections(scope))
                yield event.plain_result(
                    render_page(data, offset=offset)
                    + (
                        f"\n下一页：/hindsight view {page + 1}"
                        if offset + len(data["items"]) < data["total"]
                        else ""
                    )
                    + (
                        f"\n已登记 {changes} 项显式更正；用 show 查看有效更正。"
                        if changes
                        else ""
                    )
                )
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    @hindsight.command("show")
    async def memory_show(self, event: AstrMessageEvent, memory_id: str):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result("记忆范围正在恢复，请稍后查看。")
                return
            try:
                async with asyncio.timeout(8):
                    await self.client.ensure_auth()
                    record = await self.client.get_memory(scope["bank"], memory_id)
                if not self.readable(event, scope):
                    yield event.plain_result("记忆范围已变化，请重新查看。")
                    return
                text = render_memory(record)
                for change in self.outbox.corrections(scope):
                    if change["memory_id"] == memory_id:
                        text += (
                            f"\n用户显式更正（{change['occurred_at']}）：{change['text']}"
                            f"\n更正任务：{change['task_id']}，状态：{change['state']}"
                        )
                yield event.plain_result(text)
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    @staticmethod
    def error_code(exc):
        if isinstance(exc, HindsightError):
            return exc.code
        return "timeout" if isinstance(exc, TimeoutError) else str(exc)

    @hindsight.command("export")
    async def memory_export(self, event: AstrMessageEvent):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result("记忆范围正在恢复，请稍后导出。")
                return
            try:
                async with asyncio.timeout(30):
                    await self.client.ensure_auth()
                    payload = await self.client.export_memories(scope["bank"])
                if not self.readable(event, scope):
                    yield event.plain_result("记忆范围已变化，请重新导出。")
                    return
                payload["corrections"] = self.outbox.corrections(scope)
                data = export_bytes(payload)
                if len(data) > 8 * 1024 * 1024:
                    raise HindsightError("export_too_large")
                with tempfile.TemporaryDirectory(
                    prefix="export-", dir=self.outbox.directory
                ) as directory:
                    path = Path(directory) / "hindsight-memory.json"
                    path.write_bytes(data)
                    path.chmod(0o600)
                    await event.send(
                        MessageChain([File(name=path.name, file=str(path))])
                    )
                yield event.plain_result(
                    "本人记忆 JSON 已交给平台发送。范围包含事实、整合观察、源文档与显式更正；"
                    "上传队列和 AstrBot 聊天历史不在导出范围内。"
                )
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))
            except Exception as exc:  # noqa: BLE001 -- file delivery must preserve chat
                self.warning("export_" + type(exc).__name__)
                yield event.plain_result(
                    "文件发送失败，请检查平台的私聊文件上传能力后重试。"
                )

    @hindsight.command("correct")
    async def memory_correct(self, event: AstrMessageEvent, memory_id: str, text: str):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        source = native_snapshot(event, Plain, ())
        arguments = source.text.split(None, 3) if source else []
        if len(arguments) != 4 or arguments[2] != memory_id:
            yield event.plain_result("请发送：/hindsight correct 记忆ID 更正原文")
            return
        text = arguments[3]
        if not text.strip() or len(text) > 1000:
            yield event.plain_result("请填写 1 到 1000 字符的更正原文。")
            return
        async with self.using_resources():
            if not self.readable(event, scope) or scope["mode"] != "active":
                yield event.plain_result("请先用 /hindsight on 开启采集，再提交更正。")
                return
            try:
                async with asyncio.timeout(8):
                    await self.client.ensure_auth()
                    record = await self.client.get_memory(scope["bank"], memory_id)
                if not self.readable(event, scope):
                    yield event.plain_result("记忆范围已变化，请重新提交更正。")
                    return
                incoming = replace(source, text=text)
                target = {
                    "id": memory_id,
                    "text": record["text"],
                    "document_id": record.get("document_id") or "",
                }
                capture = self.outbox.capture(
                    incoming, "manual-corrections", correction=target
                )
                self.outbox.release(capture.task_id)
                self.worker.wake.set()
                yield event.plain_result(
                    f"更正已保存，任务 {capture.task_id}；后续召回以新陈述为准。"
                    "后台写入进度见 /hindsight status；旧事实保留来源关联。"
                )
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))
            except (OSError, sqlite3.DatabaseError):
                yield event.plain_result("本地保存失败，请检查业务数据目录。")

    @hindsight.command("status")
    async def memory_status(self, event: AstrMessageEvent):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result(f"Hindsight：{self.status}；当前私聊未开放记忆。")
            return
        async with self.using_resources():
            stats = {}
            try:
                async with asyncio.timeout(4):
                    await self.client.ensure_auth(force=True)
                    stats = await self.client.stats(scope["bank"])
                self.outbox.pause_connection("")
                self.worker.wake.set()
                connection = "可用，认证已启用"
            except (HindsightError, TimeoutError) as exc:
                connection = self.diagnostic(self.error_code(exc))
            runner = (
                "内置执行器"
                if local_runner(self.context.get_config(event.unified_msg_origin))
                else "执行器尚未适配"
            )
            summary = self.outbox.summary(scope["scope_id"])
            counts = summary["counts"]
            waiting = counts.get("ready", 0)
            tracking = sum(
                counts.get(s, 0)
                for s in ("submitting", "unknown", "tracking", "cancel_requested")
            )
            modes = {"active": "开启", "readonly": "只读", "paused": "暂停"}
            erasures = self.outbox.control.erasures(
                self.outbox.connection_id, owner_id=scope["owner_id"]
            )
            cleanup = (
                "远端清理中"
                if any(r["phase"] != "verified" for r in erasures)
                else "远端删除已核对"
                if erasures
                else "无清空记录"
            )
            tasks = (
                "，".join(f"{r['id']}:{r['state']}" for r in summary["tasks"]) or "无"
            )
            yield event.plain_result(
                f"Hindsight：{connection}\n{runner}；模式：{modes[scope['mode']]}\n"
                f"恢复检查：{'进行中' if self.outbox.recovering else '完成'}\n"
                f"逻辑用户：{scope['principal_id']}；代次：{scope['generation']}\n"
                f"待提交 {waiting}，处理中 {tracking}，失败 {counts.get('failed', 0)}，"
                f"需核对 {counts.get('reconcile_required', 0)}，完成 {counts.get('done', 0)}\n"
                f"最老待办：{summary['oldest_seconds']} 秒；任务：{tasks}\n"
                f"Observation：{stats.get('total_observations', '未知')}；"
                f"待整合：{stats.get('pending_consolidation', '未知')}\n"
                f"清空：{cleanup}\n"
                f"连接暂停原因：{self.diagnostic(self.outbox.connection_error())}\n"
                f"最近队列错误：{self.diagnostic(summary['last_error'])}\n"
                f"最近采集提示：{self.status}\n"
                "助手自动记录：标准会话历史正文，发送状态未知\n"
                "常用操作：/hindsight help"
            )

    @hindsight.command("recall")
    async def memory_recall(self, event: AstrMessageEvent, query: str):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("当前私聊未开放记忆。")
            return
        source = native_snapshot(event, Plain, ())
        arguments = source.text.split(None, 2) if source else []
        if len(arguments) == 3:
            query = arguments[2]
        async with self.using_resources():
            if not self.outbox.can_recall(scope, manual=True):
                yield event.plain_result("记忆范围正在恢复，请稍后查询。")
                return
            try:
                from .core.materials import utc_timestamp

                result = await self.recall_text(
                    scope["bank"], query, timestamp=utc_timestamp(), scope=scope
                )
                if not self.outbox.can_recall(scope, manual=True) or not self.allowed(
                    event
                ):
                    yield event.plain_result("记忆范围已变化，请重新查询。")
                    return
                yield event.plain_result(result or "没有找到相关记忆。")
            except (HindsightError, TimeoutError):
                yield event.plain_result("记忆服务暂时不可用。")

    async def mode(self, event, mode):
        scope = self.command_scope(event)
        if scope is None:
            return "此私聊未开放记忆。"
        async with self.using_resources():
            try:
                if mode == "active":
                    if not await self.worker.maintenance.check(
                        force=True, owner_id=scope["owner_id"]
                    ):
                        return "旧范围仍在清理，请稍后开启。"
                    if not self.allowed(event):
                        return "此私聊未开放记忆。"
                    latest = self.outbox.scope_owner(scope["scope_id"])
                    if (
                        latest["revision"] != scope["revision"]
                        or latest["bank"] != scope["bank"]
                    ):
                        return "记忆范围已变化，请重新开启。"
                self.outbox.set_mode(scope["scope_id"], mode)
                self.worker.wake.set()
            except QueueError as exc:
                return "模式切换失败：" + str(exc)
        return {
            "active": "记忆已开启，从新消息开始采集；暂停前的同代次队列继续处理。",
            "readonly": "记忆已设为只读：继续召回，停止采集，未提交队列冻结。",
            "paused": "记忆已暂停：停止采集和自动召回，未提交队列冻结。",
        }[mode]

    @hindsight.command("on")
    async def memory_on(self, event: AstrMessageEvent):
        yield event.plain_result(await self.mode(event, "active"))

    @hindsight.command("readonly")
    async def memory_readonly(self, event: AstrMessageEvent):
        yield event.plain_result(await self.mode(event, "readonly"))

    @hindsight.command("pause")
    async def memory_pause(self, event: AstrMessageEvent):
        yield event.plain_result(await self.mode(event, "paused"))

    @hindsight.command("retry")
    async def memory_retry(self, event: AstrMessageEvent):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        try:
            count = self.outbox.request_retry(scope["scope_id"])
            self.worker.wake.set()
            yield event.plain_result(f"已登记 {count} 条失败任务重试。")
        except QueueError as exc:
            yield event.plain_result("无法重试：" + str(exc))

    @hindsight.command("cancel")
    async def memory_cancel(self, event: AstrMessageEvent, task_id: int):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        try:
            self.outbox.request_cancel(scope["scope_id"], task_id)
            self.worker.wake.set()
            yield event.plain_result(
                f"任务 {task_id} 已登记取消，后台继续核对根任务和子任务。已产生的记忆保留。"
            )
        except QueueError as exc:
            yield event.plain_result("无法取消：" + str(exc))

    @hindsight.command("forget")
    async def memory_forget(self, event: AstrMessageEvent, confirmation: str = ""):
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        key = scope["owner_id"]
        async with self.using_resources():
            if self.outbox.recovering:
                yield event.plain_result("记忆范围正在恢复，请稍后清空。")
                return
            if not confirmation:
                self.confirmations = {
                    k: v
                    for k, v in self.confirmations.items()
                    if v["until"] > time.monotonic()
                }
                token = secrets.token_hex(3)
                self.confirmations[key] = {
                    "token": token,
                    "source": scope["scope_id"],
                    "bank": scope["bank"],
                    "revision": scope["revision"],
                    "until": time.monotonic() + 120,
                }
                yield event.plain_result(
                    f"清空范围：{event.get_platform_id()} / {event.get_sender_id()}，"
                    f"逻辑用户 {scope['principal_id']}，代次 {scope['generation']}。\n"
                    "将删除此逻辑用户在该记忆空间的文档、事实、Observation、"
                    "异步任务及其原始材料，并作废本地旧队列。绑定到同一范围的账号共用此结果。\n"
                    f"确认请在 120 秒内发送：/hindsight forget {token}"
                )
                return
            previous = self.confirmations.pop(key, None)
            if (
                not previous
                or previous["until"] < time.monotonic()
                or not secrets.compare_digest(previous["token"], confirmation)
                or previous["source"] != scope["scope_id"]
                or previous["bank"] != scope["bank"]
                or previous["revision"] != scope["revision"]
            ):
                yield event.plain_result("确认码失效，请重新发送 /hindsight forget。")
                return
            try:
                self.outbox.forget(scope["scope_id"])
                self.worker.wake.set()
                yield event.plain_result(
                    "旧范围已停用，本地旧队列已作废；远端清理中。"
                    "记忆保持暂停，用 /hindsight status 查看结果，用 /hindsight on 开启新范围。"
                )
            except (QueueError, OSError, sqlite3.DatabaseError) as exc:
                self.warning(
                    str(exc) if isinstance(exc, QueueError) else type(exc).__name__
                )
                yield event.plain_result(
                    "清空处理遇到存储错误，请查看状态并检查数据目录。"
                )

    async def _close_resources(self):
        for task in list(self.history_tasks):
            task.cancel()
        if self.history_tasks:
            await asyncio.gather(*self.history_tasks, return_exceptions=True)
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.idle.wait()
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.outbox is not None:
            self.outbox.close()
            self.outbox = None

    async def terminate(self):
        self.stopping = True
        self.incoming.clear()
        self.handled.clear()
        self.turns.clear()
        await self._close_resources()
