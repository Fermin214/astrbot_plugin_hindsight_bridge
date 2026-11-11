"""AstrBot hooks, personal memory controls, and durable erasure recovery."""

import asyncio
import json
import secrets
import sqlite3
import tempfile
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.catalog import export_bytes, render_memory, render_page
from .core.background import ReadGuard, compose_backgrounds, context_limits, reference
from .core.agent_tools import MemoryTools
from .core.client import HindsightClient, HindsightError
from .core.clock import host_timezone
from .core.diagnostics import (
    ConfigurationError, WarningLog, configuration_message, connection_fields,
    missing_connection, recall_description, recall_notice, recall_timeout,
)
from .core.dialogue import (
    appended_history,
    final_reply,
    history_boundary,
    history_messages,
    persistent_messages,
)
from .core.identity import Incoming, sha256
from .core.materials import TokenizerError, load_encoding, memory_block, recall_query, token_count
from .core.outbox import Outbox, QueueError
from .core.permissions import configuration_accounts, pairs
from .core.panel import MemoryPanel, PanelError
from .core.reflection import Reflection
from .core.source import local_runner, native_header, native_login, platform_details, prepare_bot_identity, source_identity
from .core.worker import Worker


class HindsightBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.outbox = self.client = self.worker = self.task = None
        self.connection_task = None
        self.incoming = weakref.WeakKeyDictionary()
        self.handled = weakref.WeakSet()
        self.turns = weakref.WeakKeyDictionary()
        self.history_tasks = set()
        self.status = "未配置"
        self.warning_log = WarningLog(logger)
        self.recall_outcomes = {}
        self.background_outcomes = {}
        self.background_sequence = 0
        self.background_sequences = {}
        self.stopping = False
        self.permissions = None
        self.source_accounts = {}
        self.source_lock = asyncio.Lock()
        self.permission_revision = 0
        self.hooks = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.confirmations = {}
        self.reflection = Reflection(self)
        self.read_tools = MemoryTools(self)
        self.read_tools.register()
        self.panel = MemoryPanel(self)

    @staticmethod
    def diagnostic(code):
        explanations = {
            "http_401": "连接密钥无效，请检查服务和插件的 API Key 是否一致",
            "http_403": "服务拒绝访问，请检查 API Key 权限",
            "server_auth_not_enforced": "后端未启用认证，请配置服务 API Key",
            "connection_error": "无法连接记忆服务，请检查 API 地址、网络和容器服务名",
            "connect_timeout": "连接记忆服务超时，请检查 API 地址、网络和服务状态",
            "read_timeout": "等待记忆服务响应超时，请检查服务负载；记忆检索超时可增加「记忆检索最长等待时间」",
            "write_timeout": "向记忆服务发送请求超时，请检查网络连接",
            "pool_timeout": "等待可用连接超时，请检查服务负载后重试",
            "auth_timeout": "连接认证检查超时，请检查服务状态后重试",
            "history_timeout": "读取会话信息超时，请检查 AstrBot 会话服务",
            "conversation_not_available": "无法取得当前会话，请检查 AstrBot 会话状态",
            "invalid_recall_timeout": "记忆检索最长等待时间须设为 0.2 到 30 秒，请修改插件配置",
            "invalid_background_timeout": "知识页读取最长等待时间须设为 0.05 到 10 秒，请修改插件配置",
            "invalid_background_max_tokens": "知识页长度上限须设为 64 到 8192 token，请修改插件配置",
            "recall_content_unavailable": "检索结果无法放入当前记忆长度限制，请检查记忆内容与字符上限",
            "timeout": "记忆服务响应超时",
            "recall_timeout": "查找记忆超时，可增加「记忆检索最长等待时间」或检查服务负载",
            "tokenizer_timeout": "分词器加载超时，请检查 AstrBot 的网络后重载插件",
            "tokenizer_load_failed": "分词器加载失败，请检查分词编码名称、网络和缓存目录权限后重载插件",
            "queue_capacity_reached": "本地队列已满，请恢复连接并处理积压",
            "accepted_operation_missing": "Hindsight 找不到已接收的记录，请管理员检查服务恢复时使用的备份",
            "payload_digest_conflict": "待保存内容校验失败，请管理员检查插件本地数据库",
            "scope_not_active": "请先用 /hindsight on 开启记忆",
            "scope_not_available": "这个用户的记忆正在恢复或设置已变化，请稍后重试",
            "bank_missing": "找不到原记忆库，记忆功能已暂停；请管理员恢复原库并恢复原记忆模式，或清空旧记忆后重新开始",
            "maintenance_active": "新记录和记忆查询已暂停；请管理员在插件页查看进度，完成操作后点击「恢复原记忆模式」",
            "maintenance_pending": "还有记录未处理完；请管理员在插件页查看保存进度，等待处理完成，或放弃尚未发送和保存失败的记录",
            "maintenance_unknown_operation": "记录的保存或取消结果还无法确认；请管理员修复服务后检查暂停进度。需要清空全部旧记忆时，可使用清空操作重新开始",
            "legacy_corrections_require_handover": "旧版插件的本地更正需要转存；请管理员按备份与恢复指南备份 runtime.sqlite3 和最新 control.sqlite3，完成转换后重载插件",
            "message_before_accept_from": "消息早于采集时间下限",
            "authorization_changed": "账号授权已变化，请管理员在插件页确认当前机器人身份",
            "command_already_applied": "这条命令已处理，请发送一条新命令",
            "command_identity_conflict": "消息编号与已处理的命令冲突，请重新发送命令",
            "command_expired": "命令早于最近的授权或模式变化，请重新发送",
            "invalid_capture": "内容为空或超出长度限制",
            "assistant_history_changed": "已保存的助手正文发生变化，请检查会话编辑记录",
            "conversation_history_not_saved": "助手正文尚未保存到会话历史，下一条消息继续核对",
            "http_404": "你的记忆库中没有这条记忆",
            "export_too_large": "导出文件过大，请联系管理员从官方面板导出",
            "export_limit_exceeded": "记忆数量超过命令导出上限，请联系管理员从官方面板导出",
            "export_changed": "导出期间记忆发生变化，请稍后重试",
            "catalog_changed": "查看期间记忆发生变化，请重新查看该页",
            "export_source_missing": "记忆来源不完整，请管理员检查 Hindsight 数据和保留设置",
            "invalid_identifier": "记忆 ID 无效，请从 view 列表复制完整 ID",
            "invalid_background_id": "知识页编号无效，请从插件页的知识页列表重新选择",
            "invalid_background_response": "知识页响应无法识别，请检查 Hindsight 版本后重试",
            "background_response_too_large": "知识页响应过大，请在官方面板准备较短的知识页后重新选择",
            "background_index_too_large": "知识页目录超过读取限制，请联系管理员从官方面板整理目录",
            "background_settings_invalid": "知识页选择或使用开关无法读取，请管理员检查最新控制库；不要恢复旧控制快照",
            "background_changed": "知识页选择、使用开关或账号范围已变化，请刷新后重试",
            "background_scope_changed": "身份或记忆库已变化，知识页选择已失效，请重新选择",
            "background_missing": "找不到所选知识页，请在官方面板确认后回到插件页重新选择",
            "background_empty": "知识页还没有正文，请等待官方生成完成；一直为空时请管理员检查已有记忆和标签筛选",
            "background_timeout": "读取知识页超时，请检查记忆服务，或调整知识页读取与记忆检索的等待时间",
            "background_too_large": "知识页太长，超出使用上限；请缩短官方页面，或增加知识页长度上限及共享 token、字符上限；记忆检索继续执行",
            "background_view_too_large": "知识页超过私聊查看上限，请联系管理员从官方面板查看完整内容",
            "reading_settings_invalid": "记忆读取方式无法读取，请检查最新控制库",
            "reading_changed": "记忆读取方式、账号或模型已变化，请重新发起查询",
            "tool_read_timeout": "按需读取超时，可重试或使用官方面板查看",
            "tool_budget_exhausted": "本条消息的记忆工具额度已用完，请发送新消息继续",
            "invalid_read_position": "阅读行号无效，请从知识页第一行重新读取",
            "invalid_read_version": "续读需要上一段返回的版本和下一行号，请从第一行重新读取",
            "read_version_changed": "知识页已更新，请从新版第一行重新读取",
            "read_line_too_large": "这一整行超过工具读取上限，请通过官方面板查看完整内容",
            "tool_result_too_large": "读取结果超过工具输出上限，请缩小查询范围或通过官方面板查看",
            "history_response_too_large": "知识页历史超过传输上限，正文仍可单独读取；完整历史请通过官方面板查看",
            "tool_read_failed": "按需读取失败，请检查服务后重试，或使用官方面板查看",
            "reflect_settings_invalid": "Reflect 设置无法读取，请检查最新控制库",
            "reflect_changed": "Reflect 设置或记忆范围已变化，请刷新后重试",
            "reflect_timeout": "Reflect 等待超时，已发送的原生请求仍会继续核对；不要重复调用",
            "reflect_busy": "已有 Reflect 请求尚未结束，请稍后查看状态；不要重复发送",
            "reflect_execution_unknown": "Reflect 请求的执行结果不明，相关旧记忆库不能完成清空；需要 Hindsight 提供可核验的请求完成凭据",
            "reflect_transport_timeout": "Reflect 连接超时，无法确认服务端请求是否结束；相关旧记忆库的清空会保持暂停",
            "reflect_no_answer": "Reflect 没有返回答案，可调整问题或使用 Recall 查找事实",
            "reflect_response_too_large": "Reflect 原生响应超过传输上限，可缩小问题范围；请求终点不明时不能完成相关旧记忆库的清空",
            "invalid_reflect_response": "Reflect 响应无法识别，请检查 Hindsight 版本及接口响应",
            "bank_not_created": "记忆库尚未建立，请先在读写记忆模式下保存一条普通聊天",
            "invalid_query_budget": "查询问题过长，请缩短后重试",
            "local_OperationalError": "本地数据库不可写，请检查磁盘空间与目录权限",
            "control_directory_must_be_absolute": "控制目录须填写绝对路径，或留空使用默认目录",
            "control_directory_outside_plugin": "控制目录须位于 AstrBot 的 data/plugin_data/astrbot_plugin_hindsight_bridge_control 内；旧外部目录请先按备份与恢复指南停机迁移，再修改配置并重载",
            "control_directory_invalid": "控制目录路径无法解析，请检查路径和符号链接，按备份与恢复指南迁移后重载",
        }
        if not code:
            return "无"
        return explanations.get(code, "处理失败，请检查服务与数据目录") + f"（{code}）"

    async def initialize(self):
        if self.panel.closed or self.stopping:
            return
        self.confirmations.clear()
        self.read_tools.invalidate()
        self.source_accounts.clear()
        fields = connection_fields(self.config)
        url, key, service = (fields[name] for name in ("api_url", "api_key", "service_id"))
        if missing_connection(self.config):
            self.status = configuration_message(self.config)
            if any(fields.values()):
                logger.warning("Hindsight: %s", self.status)
            return
        try:
            control_directory = self.control_directory()
            timeout = recall_timeout(self.config)
            context_limits(self.config)
            try:
                accounts = configuration_accounts(self.config.get("allowed_accounts", []))
            except QueueError:
                accounts = None
            if accounts is not None and accounts != self.config.get("allowed_accounts", []):
                self.config["allowed_accounts"] = accounts
                self.config.save_config()
            tokenizer = str(self.config.get("query_tokenizer", "o200k_base"))
            await load_encoding(tokenizer)
            if self.panel.closed or self.stopping:
                return
            self.client = HindsightClient(url, key, timeout=max(15, timeout), tokenizer=tokenizer)
            self.recall_outcomes.clear()
            self.background_outcomes.clear()
            self.background_sequences.clear()
            self.outbox = Outbox(
                Path(StarTools.get_data_dir("astrbot_plugin_hindsight_bridge")),
                service,
                self.client.url,
                credential_fingerprint=sha256(key),
                address_revision=int(self.config.get("service_address_revision", 1)),
                zone=host_timezone(self.context),
                max_tasks=int(self.config.get("queue_max_tasks", 10000)),
                max_bytes=int(self.config.get("queue_max_mib", 64)) * 1024 * 1024,
                control_directory=control_directory,
                control_root=self.control_root(),
                agent_id=str(self.config.get("agent_id", "default")),
            )
            self.refresh_permissions()
            await self.prepare_sources()
            self.outbox.recovering = True
            self.worker = Worker(
                self.outbox,
                self.client,
                logger,
                refresh_permissions=self.refresh_permissions,
                prepare_sources=self.prepare_sources,
            )
            self.task = asyncio.create_task(self.worker.run(), name="hindsight-outbox")
            self.connection_task = asyncio.create_task(
                self.monitor_connection(), name="hindsight-connection"
            )
            self.status = "已配置"
        except Exception as exc:  # noqa: BLE001 -- plugin initialization must preserve chat
            self.status = "初始化失败：" + (
                self.diagnostic(str(exc))
                if isinstance(exc, (QueueError, TokenizerError, ConfigurationError))
                else type(exc).__name__
            )
            logger.error("Hindsight: %s", self.status)
            await self._close_resources()

    def control_root(self):
        # Derive the allocation before resolving links, so a redirected root is rejected.
        return (
            (Path(get_astrbot_data_path()) / "plugin_data").resolve()
            / "astrbot_plugin_hindsight_bridge_control"
        )

    def control_directory(self):
        root = self.control_root()
        path = str(self.config.get("control_state_dir", "")).strip()
        directory = Path(path).expanduser() if path else root
        if not directory.is_absolute():
            raise QueueError("control_directory_must_be_absolute")
        try:
            resolved = directory.resolve()
            if root.resolve() != root or not resolved.is_relative_to(root):
                raise QueueError("control_directory_outside_plugin")
        except (OSError, RuntimeError):
            raise QueueError("control_directory_invalid") from None
        return resolved

    def refresh_permissions(self):
        if self.outbox is not None:
            self.outbox.zone = host_timezone(self.context)
        try:
            accounts = pairs(self.config.get("allowed_accounts", []))
        except QueueError as exc:
            self.warning(str(exc))
            accounts = frozenset()
        if accounts != self.permissions:
            self.outbox.sync_permissions(accounts)
            self.permissions = accounts
            self.permission_revision += 1
        if self.outbox is not None:
            for instance, evidence in self.source_accounts.items():
                adapter = self.context.get_platform_inst(instance)
                login = evidence.get("login")
                observed = native_login(adapter)
                if observed is not None:
                    logical = platform_details(adapter.meta().name)["logical_type"]
                    bot = observed.account_id if logical == "weixin_oc" else observed.appid
                    if self.outbox.access.observe_bot(instance, logical, bot):
                        self.outbox.sync_permissions(accounts)
                        self.confirmations.clear()
                        self.permission_revision += 1
                if login is not None and not login.matches(observed):
                    evidence["invalid_login"] = evidence.get("host_type") != "weixin_oc"
                    evidence["bot_id"] = ""
                if (
                    adapter is None or adapter is not evidence["adapter"]
                    or adapter.get_client() is not evidence["client"]
                    or evidence.get("invalid_login")
                ):
                    evidence["bot_id"] = ""
            for row in self.outbox.list_scopes():
                evidence = self.source_accounts.get(row["platform_instance_id"])
                if evidence is not None:
                    source = json.loads(row["identity_json"])
                    if source[3] != evidence["bot_id"]:
                        self.outbox.db.execute(
                            "UPDATE scopes SET allowed=0 WHERE connection_id=? AND scope_id=?",
                            (self.outbox.connection_id, row["scope_id"]),
                        )
            self.outbox.db.commit()

    async def prepare_sources(self):
        getter = getattr(self.context, "get_platform_inst", None)
        if getter is None or self.outbox is None or self.stopping:
            return
        async with self.source_lock:
            queue = self.outbox
            try:
                accounts = pairs(self.config.get("allowed_accounts", []))
            except QueueError:
                self.refresh_permissions()
                return
            for instance in sorted({platform for platform, _ in accounts}):
                adapter = getter(instance)
                client = adapter.get_client() if adapter else None
                bot = await prepare_bot_identity(self.context, instance)
                if (
                    self.stopping or self.outbox is not queue
                    or accounts != pairs(self.config.get("allowed_accounts", []))
                ):
                    return
                if getter(instance) is not adapter or (adapter and adapter.get_client() is not client):
                    bot = ""
                previous = self.source_accounts.get(instance, {})
                login = native_login(adapter)
                record = next((row for row in self.context.get_config().get("platform", [])
                               if str(row.get("id")) == instance), {})
                same_adapter = previous.get("adapter") is adapter
                invalid = record.get("type") != "weixin_oc" and same_adapter and (
                    previous.get("invalid_login", False)
                    or previous.get("login") is not None
                    and not previous["login"].matches(login)
                )
                if invalid:
                    bot = ""
                self.source_accounts[instance] = {
                    "adapter": adapter, "client": client, "bot_id": bot,
                    "login": previous.get("login") if invalid else login,
                    "invalid_login": invalid, "host_type": record.get("type", ""),
                }
                logical_type = platform_details(record.get("type", ""))["logical_type"]
                if bot and self.outbox.access.observe_bot(instance, logical_type, bot):
                    self.outbox.sync_permissions(accounts)
                    self.confirmations.clear()
                    self.permission_revision += 1
            self.outbox.sync_permissions(accounts)
            self.refresh_permissions()

    def allowed(self, event):
        try:
            accounts = pairs(self.config.get("allowed_accounts", []))
        except ValueError:
            return False
        incoming = self.source_header(event)
        return (
            not self.stopping
            and incoming is not None
            and (event.get_platform_id(), event.get_sender_id()) in accounts
            and self.outbox is not None
            and self.outbox.accepts(incoming)
            and (
                getattr(self.context, "get_platform_inst", None) is None
                or self.source_accounts.get(incoming.platform_instance_id, {}).get("bot_id") == incoming.bot_id
            )
        )

    def source_header(self, event):
        getter = getattr(self.context, "get_platform_inst", None)
        adapter = getter(event.get_platform_id()) if getter else None
        binding = self.source_accounts.get(event.get_platform_id())
        incoming = native_header(
            event, Plain, self.context.get_config().get("wake_prefix", []),
            adapter=adapter, binding=binding,
        )
        if incoming is None:
            return None
        if getter and adapter is None:
            return None
        return incoming if source_identity(event, incoming, adapter, binding=binding) else None

    def snapshot(self, event):
        if not self.allowed(event):
            return None
        prefixes = ("/", *self.config.get("command_prefixes", []))
        incoming = self.source_header(event)
        return incoming if not any(
            incoming.text.lstrip().startswith(prefix) for prefix in prefixes if prefix
        ) else None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def observe_incoming(self, event: AstrMessageEvent):
        if self.outbox is not None and event not in self.incoming:
            incoming = self.snapshot(event)
            if incoming:
                self.incoming[event] = incoming

    def warning(self, label):
        self.status = label
        self.warning_log.warning(label)

    async def monitor_connection(self):
        client, queue = self.client, self.outbox
        last_error = ""
        while not self.stopping and self.client is client:
            try:
                await self.prepare_sources()
                await client.check_connection(force=True)
                if queue.connection_error():
                    queue.pause_connection("")
                    self.worker.wake.set()
                if last_error:
                    self.warning_log.recovered(last_error)
                    last_error = ""
            except HindsightError as exc:
                if exc.authentication:
                    try:
                        queue.pause_connection(exc.code)
                    except (OSError, sqlite3.DatabaseError) as storage_error:
                        self.warning("local_" + type(storage_error).__name__)
                last_error = exc.code
                self.warning(exc.code)
            except (QueueError, OSError, sqlite3.DatabaseError) as exc:
                last_error = str(exc) if isinstance(exc, QueueError) else "local_" + type(exc).__name__
                self.warning(last_error)
            await asyncio.sleep(30 if last_error else 60)

    def record_recall(self, scope, outcome, started, code=""):
        key = (self.outbox.connection_id, scope["owner_id"])
        previous = self.recall_outcomes.get(key, {})
        if previous.get("bank") != scope["bank"] or previous.get("revision") != scope["revision"]:
            previous = {}
        failures = previous.get("consecutive_failures", 0)
        if outcome in {"success", "empty"} and failures:
            logger.info("Hindsight recall recovered: %s (failures=%s)", scope["scope_id"], failures)
        self.recall_outcomes[key] = {
            "bank": scope["bank"], "revision": scope["revision"],
            "outcome": outcome, "code": code, "last_at": time.time(),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "consecutive_failures": failures + 1 if outcome == "error" else 0,
        }

    def recall_status(self, scope):
        value = self.recall_outcomes.get((self.outbox.connection_id, scope["owner_id"]), {})
        if value.get("bank") != scope["bank"] or value.get("revision") != scope["revision"]:
            return None
        return {key: value[key] for key in (
            "outcome", "code", "last_at", "elapsed_ms", "consecutive_failures",
        )}

    async def recall_text(self, bank, current, history=(), *, timestamp, prefix=""):
        timeout = recall_timeout(self.config)
        budget = max(32, min(400, int(self.config.get("recall_query_tokens", 380))))
        limits = context_limits(self.config)
        result_tokens, limit = limits["tokens"], limits["chars"]
        client = self.client
        sequence = client.next_connection_sequence()
        stage = "auth"
        try:
            async with asyncio.timeout(timeout):
                query = recall_query(current, history, budget=budget, name=client.tokenizer)
                await client.ensure_auth(timeout=timeout, _connection_sequence=sequence)
                stage = "recall"
                results = await client.recall(
                    bank, query, result_tokens, timestamp=timestamp, timeout=timeout,
                )
                selected, block = [], ""
                for item in results[:12]:
                    candidate = memory_block([*selected, item], limit=limit)
                    if (
                        candidate and candidate != block and len(prefix + candidate) <= limit
                        and token_count(prefix + candidate, client.tokenizer) <= result_tokens
                    ):
                        selected.append(item)
                        block = candidate
                if results and not block:
                    raise HindsightError("recall_content_unavailable")
                return block
        except TimeoutError:
            code = "auth_timeout" if stage == "auth" else "recall_timeout"
            if stage == "auth":
                client.connection_result(False, code, sequence=sequence)
            raise HindsightError(code) from None

    async def recall_background(self, event, guard, current, history, *, timestamp, include_facts=True, prefix=""):
        timeout = recall_timeout(self.config)
        deadline = time.monotonic() + timeout
        limits = context_limits(self.config)
        tokens, chars = limits["tokens"], limits["chars"]
        budget = max(32, min(400, int(self.config.get("recall_query_tokens", 380))))
        tasks = []
        stage = "auth"

        page_slots = asyncio.Semaphore(4)
        page_deadline = None

        async def read_page(selected):
            page, code = None, ""
            try:
                async with asyncio.timeout_at(page_deadline):
                    async with page_slots:
                        page = await guard.client.background_page(
                            guard.scope["bank"], selected["mental_model_id"],
                        )
            except (HindsightError, TimeoutError) as exc:
                code = exc.code if isinstance(exc, HindsightError) else "background_timeout"
            return {"reference": selected, "page": page, "code": code}

        async def read_pages():
            return await asyncio.gather(*(read_page(selected) for selected in guard.selection["pages"]))

        async def read_facts(query):
            if not include_facts:
                return [], ""
            try:
                async with asyncio.timeout_at(deadline - min(0.02, timeout / 10)):
                    rows = await guard.client.recall(
                        guard.scope["bank"], query, tokens, timestamp=timestamp,
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    return rows, ""
            except (HindsightError, TimeoutError) as exc:
                return [], exc.code if isinstance(exc, HindsightError) else "recall_timeout"

        try:
            async with asyncio.timeout_at(deadline):
                query = recall_query(current, history, budget=budget, name=guard.client.tokenizer)
                await guard.client.ensure_auth(timeout=max(0.001, deadline - time.monotonic()))
                if not guard.valid(self, event):
                    raise QueueError("background_changed")
                stage = "recall"
                page_deadline = time.monotonic() + min(
                    limits["page_timeout"], max(0, deadline - time.monotonic()) / 4,
                )
                tasks = [asyncio.create_task(read_pages()), asyncio.create_task(read_facts(query))]
                reads, (rows, recall_code) = await asyncio.gather(*tasks)
                if not guard.valid(self, event):
                    raise QueueError("background_changed")
                auth_code = next((code for code in (recall_code, *(read["code"] for read in reads))
                                  if code and HindsightError(code).authentication), "")
                if auth_code:
                    rows, recall_code = [], auth_code
                    reads = [{**read, "page": None, "code": auth_code} for read in reads]
                block, facts_used, outcomes = compose_backgrounds(
                    reads, rows, tokens=tokens, chars=chars, tokenizer=guard.client.tokenizer,
                    page_tokens=limits["page_tokens"], recall_code=recall_code,
                    on_demand=not include_facts,
                    prefix=prefix,
                )
                if rows and not facts_used and not recall_code:
                    recall_code = "recall_content_unavailable"
                if time.monotonic() > deadline:
                    raise HindsightError("recall_timeout")
                if any(item["code"] == "bank_missing" for item in outcomes):
                    guard.queue.control.set_bank_state(guard.queue.connection_id, guard.scope["bank"], "missing")
                    guard.queue.apply_control()
                    raise HindsightError("bank_missing")
                return block, recall_code, facts_used, {
                    "code": next((item["code"] for item in outcomes if item["code"]), ""),
                    "used": any(item["used"] for item in outcomes), "pages": outcomes,
                    "exceeded": list(dict.fromkeys(label for item in outcomes for label in item["exceeded"])),
                }
        except TimeoutError:
            raise HindsightError("auth_timeout" if stage == "auth" else "recall_timeout") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

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
        except TimeoutError:
            self.warning("history_timeout")
            return None
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

    @filter.on_agent_begin()
    async def bind_memory_tools(self, event, run_context):
        self.read_tools.begin(event, run_context)

    @filter.on_agent_done()
    async def remember_assistant(self, event, run_context, response):
        if not self.read_tools.finish(event, run_context):
            return
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
                    await self.prepare_sources()
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
        original_tools = self.read_tools.filter_request(event, req)
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
        scope = None
        outcome = None
        code = ""
        content = ""
        read_guard = None
        background_status = None
        request_prefix = ""
        started = time.monotonic()
        try:
            self.refresh_permissions()
            permission_revision = self.permission_revision
            scope = self.outbox.scope(incoming)
            if not self.outbox.can_recall(scope):
                self.record_recall(scope, "skipped", started)
                return
            self.background_sequence += 1
            sequence = self.background_sequence
            background_key = (self.outbox.connection_id, scope["owner_id"])
            self.background_sequences[background_key] = sequence
            outcome = "error"
            try:
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
            except TimeoutError:
                raise HindsightError("history_timeout") from None
            if not conversation:
                code = "conversation_not_available"
                self.warning(code)
                return
            self.refresh_permissions()
            await self.prepare_sources()
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
                    outcome = None
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
            read_guard = ReadGuard.freeze(self, scope)
            options = await self.read_tools.prepare(event, req, incoming, scope, original_tools)
            request_prefix = self.read_tools.request_prefix(event)
            on_demand = options["mode"] == "tools"
            history = [
                    ("用户" if m.role == "user" else "助手", m.text)
                    for m in capture.history
                ] if capture else ()
            if read_guard.selection["enabled"]:
                memory, code, has_facts, background_status = await self.recall_background(
                    event, read_guard, incoming.text, history, timestamp=incoming.iso_timestamp,
                    include_facts=not on_demand,
                    prefix=request_prefix,
                )
                outcome = "error" if code else "on_demand" if on_demand else "success" if has_facts else "empty"
                if code:
                    if HindsightError(code).authentication:
                        self.outbox.pause_connection(code)
                    self.warning(code)
            elif on_demand:
                memory = self.read_tools.notice(scope)
                outcome = "on_demand"
            else:
                memory = await self.recall_text(
                    scope["bank"], incoming.text, history, timestamp=incoming.iso_timestamp,
                    prefix=request_prefix,
                )
                outcome = "success" if memory else "empty"
            content = memory or "Hindsight 检索完成，本轮未匹配到相关记忆。"
            self.status = "运行中"
        except asyncio.CancelledError:
            outcome = None
            raise
        except HindsightError as exc:
            code = exc.code
            if exc.authentication:
                self.outbox.pause_connection(exc.code)
            self.warning(exc.code)
        except QueueError as exc:
            code = str(exc)
            self.warning(code)
        except Exception as exc:  # noqa: BLE001 -- local storage failures must preserve chat
            code = "local_" + type(exc).__name__
            self.warning(code)
        finally:
            try:
                if outcome is not None and scope is not None:
                    await self.prepare_sources()
                    self.refresh_permissions()
                    if (
                        self.allowed(event)
                        and self.outbox.can_recall(scope)
                        and self.permission_revision == permission_revision
                        and (read_guard is None or read_guard.valid(self, event))
                    ):
                        self.record_recall(scope, outcome, started, code)
                        if not content:
                            content = recall_notice(code)
                        req.extra_user_content_parts.append(TextPart(text=request_prefix + content).mark_as_temp())
                        if (
                            read_guard is not None and read_guard.selection["enabled"]
                            and self.background_sequences.get(background_key) == sequence
                        ):
                            self.background_outcomes[background_key] = {
                                "bank": scope["bank"], "revision": scope["revision"],
                                "selection_revision": read_guard.selection["revision"],
                                **(background_status or {"code": code, "used": False}),
                                "checked_at": time.time(),
                            }
            except Exception as exc:  # noqa: BLE001 -- diagnostics must preserve chat
                self.warning("local_" + type(exc).__name__)
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
        """个人记忆查看、导出、模式、任务控制与完整遗忘。"""

    def command_scope(self, event):
        if self.outbox is None or not self.allowed(event):
            return None
        if not local_runner(self.context.get_config(event.unified_msg_origin)):
            return None
        self.refresh_permissions()
        return self.outbox.scope(self.source_header(event)) if self.allowed(event) else None

    def prepare_command(self, event, scope, name, arguments=()):
        self.refresh_permissions()
        incoming = self.source_header(event)
        if not self.allowed(event) or incoming is None:
            raise QueueError("authorization_changed")
        return self.outbox.access.command(
            incoming, incoming.scope(self.outbox.deployment_id), scope, name, arguments,
        )

    def apply_command(self, event, scope, command, operation):
        self.refresh_permissions()
        if not self.allowed(event):
            raise QueueError("authorization_changed")
        try:
            result = self.outbox.access.run(command, scope, operation)
            self.outbox.apply_control()
            return result
        except (OSError, sqlite3.DatabaseError):
            self.outbox.recovering = True
            raise

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

    def unavailable_message(self, scope):
        summary = self.outbox.summary(scope["scope_id"])
        if summary.get("bank_missing"):
            return self.diagnostic("bank_missing")
        if summary.get("maintenance"):
            return self.diagnostic("maintenance_active")
        return self.diagnostic("scope_not_available")

    @hindsight.command("help")
    async def memory_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Hindsight 记忆：正常聊天即可保存和查找长期记忆，无需逐条操作。\n"
            "/hindsight status：连接、模式和保存进度\n"
            "/hindsight view [页码]：查看本人记忆，每页 10 条\n"
            "/hindsight show 记忆ID：查看详情和来源\n"
            "/hindsight recall 问题：查找相关记忆\n"
            "/hindsight background [序号]：查看本人已选知识页（Knowledge Page）\n"
            "可在插件页选择一份 Hindsight 知识页供聊天参考，例如项目目标与约束；默认关闭。\n"
            "/hindsight export：导出记忆和来源（JSON）\n"
            "修改记忆：联系管理员，在插件页暂停保存和查询后到 Hindsight 官方面板操作\n"
            "/hindsight on：保存新聊天，并自动使用已有记忆\n"
            "/hindsight readonly：继续使用已有记忆，不保存新聊天\n"
            "/hindsight pause：暂停保存和自动检索，已有记忆与手动查看保留\n"
            "/hindsight retry：重试保存失败的记录\n"
            "/hindsight cancel 任务编号：取消上传，已形成的记忆保留\n"
            "/hindsight forget：清空本人全部记忆，需再次确认\n"
            "修改前暂停保存和查询，Hindsight 已接收的记录继续处理，普通聊天继续。\n"
            "自动记录你的原话和机器人保存到会话历史的最终回复。"
        )

    @hindsight.command("view")
    async def memory_view(self, event: AstrMessageEvent, page: int = 1):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        if not 1 <= page <= 1000:
            yield event.plain_result("页码须在 1 到 1000 之间。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result(self.unavailable_message(scope))
                return
            try:
                async with asyncio.timeout(8):
                    await self.client.ensure_auth()
                    offset = (page - 1) * 10
                    data = await self.client.memory_page(scope["bank"], page=page)
                await self.prepare_sources()
                if not self.readable(event, scope):
                    yield event.plain_result(self.unavailable_message(scope))
                    return
                yield event.plain_result(
                    render_page(data, offset=offset)
                    + (
                        f"\n下一页：/hindsight view {page + 1}"
                        if offset + len(data["items"]) < data["total"]
                        else ""
                    )
                )
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    @hindsight.command("background")
    async def memory_background(self, event: AstrMessageEvent, page: int = 1):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result(self.unavailable_message(scope))
                return
            try:
                guard = ReadGuard.freeze(self, scope, manual=True)
                selected = guard.selection["pages"]
                if not selected:
                    yield event.plain_result(
                        "尚未选择知识页（Knowledge Page）。记忆检索按所选记忆模式运行。\n"
                        "知识页由 Hindsight 从已有记忆按主题生成，可让新会话参考项目目标等资料。"
                        "请管理员在官方面板准备知识页，再到插件页「聊天知识页」选择。"
                    )
                    return
                if type(page) is not int or not 1 <= page <= len(selected):
                    yield event.plain_result(f"知识页序号须为 1 到 {len(selected)}。")
                    return
                position = page
                selected_page = selected[position - 1]
                async with asyncio.timeout(8):
                    await guard.client.ensure_auth()
                    if not guard.valid(self, event):
                        raise QueueError("background_changed")
                    page = await guard.client.background_page(scope["bank"], selected_page["mental_model_id"])
                await self.prepare_sources()
                if not guard.valid(self, event):
                    raise QueueError("background_changed")
                if not page["content"].strip():
                    yield event.plain_result(self.diagnostic("background_empty"))
                    return
                # One whole bounded response; larger documents remain in the official panel.
                position_label = f"第 {position}/{len(selected)} 篇"
                if position < len(selected):
                    position_label += f"；下一篇：/hindsight background {position + 1}"
                content = (
                    f"知识页（Knowledge Page）：{selected_page['page_id']}\n"
                    f"{position_label}\n"
                    f"聊天时使用所选知识页（开关设置）：{'开启' if guard.selection['enabled'] else '关闭'}\n"
                    f"刷新时间：{page['last_refreshed_at'] or '未知'}\n"
                    f"最近刷新失败：{page['last_refresh_failed_at'] or '无记录'}\n"
                    "内容是否过期：未知；请在官方面板核对内容更新。\n\n" + page["content"]
                )
                if len(content) > 3500 or len(content.encode("utf-8")) > 12000:
                    yield event.plain_result(self.diagnostic("background_view_too_large"))
                else:
                    yield event.plain_result(content)
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    @hindsight.command("show")
    async def memory_show(self, event: AstrMessageEvent, memory_id: str):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result(self.unavailable_message(scope))
                return
            try:
                async with asyncio.timeout(8):
                    await self.client.ensure_auth()
                    record = await self.client.get_memory(scope["bank"], memory_id)
                await self.prepare_sources()
                if not self.readable(event, scope):
                    yield event.plain_result(self.unavailable_message(scope))
                    return
                yield event.plain_result(render_memory(record))
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    @staticmethod
    def error_code(exc):
        if isinstance(exc, HindsightError):
            return exc.code
        return "timeout" if isinstance(exc, TimeoutError) else str(exc)

    @hindsight.command("export")
    async def memory_export(self, event: AstrMessageEvent):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        async with self.using_resources():
            if not self.readable(event, scope):
                yield event.plain_result(self.unavailable_message(scope))
                return
            try:
                async with asyncio.timeout(30):
                    await self.client.ensure_auth()
                    payload = await self.client.export_memories(scope["bank"])
                await self.prepare_sources()
                if not self.readable(event, scope):
                    yield event.plain_result(self.unavailable_message(scope))
                    return
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
                    "已发送记忆 JSON 文件，包含记忆事实、整合结果和来源文档。"
                    "插件待保存记录和 AstrBot 聊天历史不在文件中。"
                )
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))
            except Exception as exc:  # noqa: BLE001 -- file delivery must preserve chat
                self.warning("export_" + type(exc).__name__)
                yield event.plain_result(
                    "文件发送失败，请检查平台的私聊文件上传能力后重试。"
                )

    @hindsight.command("correct")
    async def memory_correct(
        self, event: AstrMessageEvent, memory_id: str = "", text: str = ""
    ):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        yield event.plain_result(
            "请联系管理员修改记忆：在 AstrBot 插件页暂停保存和查询，"
            "等记录处理完成后，到 Hindsight 官方面板按记忆库 ID找到并修改记忆。"
        )

    @hindsight.command("status")
    async def memory_status(self, event: AstrMessageEvent):
        await self.prepare_sources()
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
            await self.prepare_sources()
            latest = self.outbox.scope_owner(scope["scope_id"])
            if (
                not self.allowed(event) or not latest or not latest["allowed"]
                or latest["bank"] != scope["bank"] or latest["revision"] != scope["revision"]
            ):
                yield event.plain_result(self.diagnostic("scope_not_available"))
                return
            runner = (
                "内置执行器"
                if local_runner(self.context.get_config(event.unified_msg_origin))
                else "执行器尚未适配"
            )
            summary = self.outbox.summary(scope["scope_id"])
            maintenance = (
                self.diagnostic("bank_missing")
                if summary.get("bank_missing")
                else self.diagnostic("maintenance_active")
                if summary.get("maintenance")
                else "无"
            )
            counts = summary["counts"]
            waiting = counts.get("ready", 0)
            tracking = sum(
                counts.get(s, 0)
                for s in ("submitting", "unknown", "deferred", "tracking", "cancel_requested")
            )
            modes = {"active": "保存并自动使用记忆", "readonly": "只自动使用已有记忆", "paused": "暂停保存和自动使用"}
            erasures = self.outbox.control.erasures(
                self.outbox.connection_id, owner_id=scope["owner_id"]
            )
            cleanup = (
                "正在删除旧记忆"
                if any(r["phase"] != "verified" for r in erasures)
                else "旧记忆已删除"
                if erasures
                else "无清空记录"
            )
            task_labels = {
                "ready": "等待发送", "submitting": "正在发送",
                "unknown": "确认保存结果中", "deferred": "暂停发送，继续确认保存结果",
                "tracking": "Hindsight 正在整理",
                "held": "等待机器人回复", "failed": "保存失败",
                "reconcile_required": "需要检查保存结果",
                "cancel_requested": "正在取消", "cancelled": "已取消",
                "done": "已保存", "discarded": "已放弃保存",
            }
            tasks = "，".join(
                f"{r['id']}（{task_labels.get(r['state'], r['state'])}）"
                for r in summary["tasks"]
            ) or "无"
            yield event.plain_result(
                f"Hindsight：{connection}\n{runner}；模式：{modes[scope['mode']]}\n"
                f"恢复检查：{'进行中' if self.outbox.recovering else '完成'}\n"
                f"诊断编号：{scope['principal_id']} / {scope['generation']}\n"
                f"等待发送 {waiting}，发送与保存确认中 {tracking}，失败 {counts.get('failed', 0)}，"
                f"需核对 {counts.get('reconcile_required', 0)}，完成 {counts.get('done', 0)}\n"
                f"最早记录已等待：{summary['oldest_seconds']} 秒；任务编号与进度：{tasks}\n"
                f"观察（Observations）：{stats.get('total_observations', '未知')}；"
                f"待整合：{stats.get('pending_consolidation', '未知')}\n"
                f"清空：{cleanup}\n"
                f"保存和查询暂停状态：{maintenance}\n"
                f"连接暂停原因：{self.diagnostic(self.outbox.connection_error())}\n"
                f"最近保存错误：{self.diagnostic(summary['last_error'])}\n"
                f"最近记录状态：{self.status}\n"
                f"最近记忆检索：{recall_description(self.recall_status(scope))}\n"
                "常用操作：/hindsight help"
            )

    @hindsight.command("recall")
    async def memory_recall(self, event: AstrMessageEvent, query: str):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("当前私聊未开放记忆。")
            return
        source = self.source_header(event)
        arguments = source.text.split(None, 2) if source else []
        if len(arguments) == 3:
            query = arguments[2]
        async with self.using_resources():
            if not self.outbox.can_recall(scope, manual=True):
                yield event.plain_result(self.unavailable_message(scope))
                return
            try:
                from .core.materials import utc_timestamp

                result = await self.recall_text(
                    scope["bank"], query, timestamp=utc_timestamp()
                )
                await self.prepare_sources()
                if not self.outbox.can_recall(scope, manual=True) or not self.allowed(
                    event
                ):
                    yield event.plain_result(self.unavailable_message(scope))
                    return
                yield event.plain_result(result or "没有找到相关记忆。")
            except (HindsightError, QueueError, TimeoutError) as exc:
                yield event.plain_result(self.diagnostic(self.error_code(exc)))

    async def mode(self, event, mode):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            return "此私聊未开放记忆。"
        async with self.using_resources():
            try:
                command = self.prepare_command(event, scope, "mode", (mode,))
                if mode == "active":
                    summary = self.outbox.summary(scope["scope_id"])
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        return self.unavailable_message(scope)
                    if not await self.worker.maintenance.check(
                        force=True, owner_id=scope["owner_id"]
                    ):
                        return "旧记忆仍在清理，请等清理完成后再开启记忆。"
                    await self.prepare_sources()
                    if not self.allowed(event):
                        return "此私聊未开放记忆。"
                    latest = self.outbox.scope_owner(scope["scope_id"])
                    if (
                        latest["revision"] != scope["revision"]
                        or latest["bank"] != scope["bank"]
                    ):
                        return "记忆范围已变化，请重新开启。"
                self.apply_command(
                    event, scope, command,
                    lambda: self.outbox.set_mode(scope["scope_id"], mode, journal_only=True),
                )
                self.worker.wake.set()
            except QueueError as exc:
                return "模式切换失败：" + self.diagnostic(str(exc))
        return {
            "active": "记忆已开启：开始记录新消息，并继续保存暂停前的待处理记录。",
            "readonly": "只使用已有记忆，不再记录新消息；尚未发送的记录等重新开启后再保存。",
            "paused": "记忆已暂停：不再记录新消息或自动查找记忆；尚未发送的记录等重新开启后再保存。",
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
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        try:
            command = self.prepare_command(event, scope, "retry")
            summary = self.outbox.summary(scope["scope_id"])
            if summary.get("maintenance") or summary.get("bank_missing"):
                yield event.plain_result(self.unavailable_message(scope))
                return
            count = self.apply_command(
                event, scope, command,
                lambda: self.outbox.request_retry(scope["scope_id"], journal_only=True),
            )
            self.worker.wake.set()
            yield event.plain_result(f"已安排重新保存 {count} 条失败记录。")
        except QueueError as exc:
            yield event.plain_result("无法重试：" + self.diagnostic(str(exc)))

    @hindsight.command("cancel")
    async def memory_cancel(self, event: AstrMessageEvent, task_id: int):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        try:
            command = self.prepare_command(event, scope, "cancel", (task_id,))
            self.apply_command(
                event, scope, command,
                lambda: self.outbox.request_cancel(scope["scope_id"], task_id, journal_only=True),
            )
            self.worker.wake.set()
            yield event.plain_result(
                f"正在取消任务 {task_id}，可用 /hindsight status 查看进度。已经生成的记忆保留。"
            )
        except QueueError as exc:
            yield event.plain_result("无法取消：" + self.diagnostic(str(exc)))

    @hindsight.command("forget")
    async def memory_forget(self, event: AstrMessageEvent, confirmation: str = ""):
        await self.prepare_sources()
        scope = self.command_scope(event)
        if scope is None:
            yield event.plain_result("此私聊未开放记忆。")
            return
        key = scope["owner_id"]
        async with self.using_resources():
            try:
                self.panel.ensure_no_upgrade()
            except PanelError:
                yield event.plain_result("服务升级期间暂时不能清空记忆。请先完成升级核对并解除升级造成的暂停，或在插件面板选择暂不升级。")
                return
            if self.outbox.recovering:
                yield event.plain_result("记忆范围正在恢复，请稍后清空。")
                return
            latest = self.outbox.scope_owner(scope["scope_id"])
            owner = self.outbox.control.owner(
                self.outbox.connection_id, scope["owner_id"]
            )
            if (
                not self.allowed(event)
                or not latest
                or not latest["allowed"]
                or latest["owner_id"] != scope["owner_id"]
                or latest["bank"] != scope["bank"]
                or latest["revision"] != scope["revision"]
                or not owner
                or owner["bank"] != scope["bank"]
                or owner["revision"] != scope["revision"]
            ):
                yield event.plain_result(self.diagnostic("scope_not_available"))
                return
            if not confirmation:
                try:
                    command = self.prepare_command(event, scope, "forget_prepare")
                    self.apply_command(event, scope, command, lambda: None)
                except QueueError as exc:
                    yield event.plain_result(self.diagnostic(str(exc)))
                    return
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
                    "authorization_revision": self.outbox.access.authorization(
                        event.get_platform_id(), event.get_sender_id(),
                    )["revision"],
                    "until": time.monotonic() + 120,
                }
                yield event.plain_result(
                    f"用户：{event.get_platform_id()} / {event.get_sender_id()}。\n"
                    "将删除这个用户在 Hindsight 中的全部记忆、知识页及其历史、用于生成记忆的聊天内容，"
                    "以及插件中尚未保存的记录，无法撤销。AstrBot 聊天记录、工具历史和日志保留。"
                    "如果其他账号共用此记忆库，它们的记忆也会一起清空。\n"
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
                or previous["authorization_revision"] != self.outbox.access.authorization(
                    event.get_platform_id(), event.get_sender_id(),
                )["revision"]
            ):
                yield event.plain_result("确认码失效，请重新发送 /hindsight forget。")
                return
            try:
                command = self.prepare_command(event, scope, "forget", (sha256(confirmation),))
                self.apply_command(
                    event, scope, command,
                    lambda: self.outbox.forget(scope["scope_id"], journal_only=True),
                )
                self.worker.wake.set()
                yield event.plain_result(
                    "正在清空旧记忆，记忆功能已暂停。"
                    "用 /hindsight status 查看进度；清理完成后发送 /hindsight on，开始记录新记忆。"
                )
            except (QueueError, OSError, sqlite3.DatabaseError) as exc:
                self.warning(
                    str(exc) if isinstance(exc, QueueError) else type(exc).__name__
                )
                yield event.plain_result(
                    "清空处理遇到存储错误，请查看状态并检查数据目录。"
                )

    async def _close_resources(self):
        await self.reflection.cancel()
        if self.connection_task is not None:
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
            self.connection_task = None
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
        await self.read_tools.close()
        await self.reflection.close()
        self.confirmations.clear()
        await self.panel.close()
        self.incoming.clear()
        self.source_accounts.clear()
        self.handled.clear()
        self.turns.clear()
        await self._close_resources()
