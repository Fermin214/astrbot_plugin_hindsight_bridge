"""AstrBot plugin-view connection, account and maintenance management."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit

import httpx

from .client import HindsightClient, HindsightError
from .diagnostics import configuration_message, missing_connection
from .identity import sha256
from .installer import HINDSIGHT_VERSION, installation, local_installation_supported
from .managed_connections import ManagedConnections, PanelError
from .outbox import QueueError
from .source import platform_details
from . import settings
from .reference import PROMPT_TOKEN_LIMIT
from .panel_setup import PanelSetup
from .panel_upgrade import PanelUpgrade
from .panel_scopes import PanelScopes

NAME = "astrbot_plugin_hindsight_bridge"


@dataclass(frozen=True, repr=False)
class PanelSnapshot:
    client: HindsightClient | None
    outbox: object
    revision: str
    setup: dict
    api_url: str
    api_key: str


class MemoryPanel(PanelSetup, PanelUpgrade, PanelScopes):
    def __init__(self, plugin):
        from .bank_stats import BankStats

        self.plugin = plugin
        self.bank_stats = BankStats(plugin)
        self.lock = asyncio.Lock()
        self.closed = False
        self.requests = set()
        self.model_task = None
        self.model_job = None
        self.model_selection = None
        self.managed_connections = ManagedConnections(self)
        self.version_cache = None
        routes = [
            ("bootstrap", "GET", self.bootstrap),
            ("connection/check", "POST", self.check),
            ("settings/save", "POST", self.save),
            ("parameters/save", "POST", self.save_parameters),
            ("access/save", "POST", self.save_access),
            ("accounts/authorize", "POST", self.authorize),
            ("install/prepare", "POST", self.prepare),
            ("service/status", "GET", self.service_status),
            ("service/upgrade", "POST", self.upgrade),
            ("service/upgrade/ignore", "POST", self.ignore_upgrade),
            ("models/save", "POST", self.save_models),
            ("models/status", "GET", self.model_status),
            ("managed/options", "POST", self.managed_options),
            ("managed/connect", "POST", self.managed_connect),
            ("managed/status", "POST", self.managed_status),
            ("managed/version", "POST", self.managed_version),
            ("uninstall/prepare", "POST", self.prepare_uninstall),
            ("scopes/<scope_id>/action", ["GET", "POST"], self.action),
            ("scopes/<scope_id>/background", ["GET", "POST"], self.background),
            ("scopes/<scope_id>/reading", ["GET", "POST"], self.reading),
            ("scopes/<scope_id>/references", ["GET", "POST"], self.references),
            ("scopes/<scope_id>/references/count", "POST", self.prompt_count),
            ("scopes/<scope_id>/usage", "GET", self.usage),
            ("scopes/<scope_id>/usage/<turn_id>", "GET", self.usage),
            ("scopes/<scope_id>/skips", ["GET", "POST"], self.skips),
            ("scopes/<scope_id>/reflect", ["GET", "POST"], self.reflect),
        ]
        if hasattr(plugin.context, "register_web_api"):
            for route, method, handler in routes:
                plugin.context.register_web_api(
                    f"/{NAME}/{route}", self.endpoint(handler),
                    [method] if isinstance(method, str) else method, "Hindsight 记忆管理"
                )

    def endpoint(self, handler):
        async def invoke(**kwargs):
            from astrbot.api.web import error_response, json_response

            task = asyncio.current_task()
            try:
                self.ensure_open()
                self.require_login()
                self.requests.add(task)
                value = await handler(**kwargs)
                if isinstance(value, dict):
                    return json_response(value, headers={"Cache-Control": "no-store"})
                return value
            except (PanelError, ValueError, QueueError, HindsightError, TimeoutError) as exc:
                message = (
                    str(exc) if isinstance(exc, (PanelError, ValueError))
                    and not isinstance(exc, QueueError)
                    else self.error_message(self.plugin.error_code(exc))
                )
                return error_response(message, headers={"Cache-Control": "no-store"})
            except (OSError, sqlite3.DatabaseError):
                return error_response("无法保存数据，请检查 AstrBot 数据目录与磁盘空间。")
            finally:
                self.requests.discard(task)
        return invoke

    @staticmethod
    def require_login():
        from astrbot.api.web import request

        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")

    @asynccontextmanager
    async def route(self, *, resources=True, timeout=8):
        async with self.lock, self.plugin.using_resources() if resources else nullcontext(), asyncio.timeout(timeout):
            self.ensure_open()
            self.require_login()
            yield

    def check_card(self, payload, scope, selection, code):
        self.check_scope_revision(payload, scope, code)
        if type(payload.get("revision")) is not int or payload["revision"] != selection["revision"]:
            raise QueueError(code)

    def check_scope_revision(self, payload, scope, code="scope_not_available"):
        if (
            type(payload.get("scope_revision")) is not int or payload["scope_revision"] != scope["revision"]
            or payload.get("config_revision") != self.config_revision()
        ):
            raise QueueError(code)

    def ensure_open(self):
        if self.closed:
            raise PanelError("插件已停用，请先启用插件并重新打开页面。")

    def connection_ready(self, url, key):
        plugin, queue = self.plugin, self.plugin.outbox
        if (
            not self.closed and not plugin.stopping and queue
            and self.connection_url(plugin.config.get("api_url", "")) == url
            and plugin.config.get("api_key") == key
            and queue.connection_error()
        ):
            queue.pause_connection("")
            plugin.worker.wake.set()

    def error_message(self, code):
        messages = {
            "reference_turn_missing": "这轮使用记录已清除，请重新打开使用记录。",
            "reference_changed": "记忆参考设置已变化，请重新载入后保存。",
            "reference_settings_invalid": "记忆参考设置损坏，请检查插件控制库。",
            "reference_prompt_too_long": f"记忆提示超过 {PROMPT_TOKEN_LIMIT} token，请缩短后保存。",
            "reference_history_unavailable": "无法读取或保存使用记录，请检查插件数据目录权限与剩余空间。",
            "remote_failed": "记忆整理失败。请检查模型配置和服务日志，修复后点击「重试保存」。",
            "remote_cancelled": "这条记录的保存已取消。",
            "cancellation_registered": "正在取消这条记录的保存。",
            "retry_state_changed": "任务状态已变化，请刷新状态。",
            "cancel_target_missing": "需要取消的任务已不存在，请刷新状态。",
        }
        if code in messages:
            return messages[code]
        return self.plugin.diagnostic(code).removesuffix(f"（{code}）")

    @staticmethod
    async def body():
        from astrbot.api.web import request

        if len(await request.body()) > 65536:
            raise PanelError("提交内容过长。")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise PanelError("请提交有效的配置。")
        return payload

    def providers(self):
        from .providers import ProviderCatalog

        return ProviderCatalog(self.plugin.context)

    def platforms(self):
        return [
            {
                "id": str(row["id"]), "name": str(row.get("name") or row["id"]),
                "enabled": row.get("enable", True), **platform_details(row.get("type", "")),
            }
            for row in self.plugin.context.get_config().get("platform", [])
        ]

    def directory(self):
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(NAME))

    def has_identity_data(self):
        return any((self.directory() / name).is_file() for name in (
            "bridge.sqlite3", "runtime.sqlite3", "outbox.sqlite3",
        )) or (self.plugin.control_directory() / "control.sqlite3").is_file()

    def state(self, *, create=False):
        path = self.directory() / "setup.json"
        value = {}
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise PanelError("安装配置损坏，请从备份恢复 setup.json。") from None
            if not isinstance(value, dict):
                raise PanelError("安装配置损坏，请从备份恢复 setup.json。")
        if not create:
            return value
        changed = False
        for key, size in (
            ("id", 6), ("database_password", 32), ("api_key", 32), ("management_key", 32)
        ):
            if not value.get(key):
                value[key] = secrets.token_hex(size)
                changed = True
        if changed:
            self.store_state(value)
        return value

    def store_state(self, value):
        directory = self.directory()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "setup.json.new"
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(directory / "setup.json")

    def managed_connection(self, state=None, *, api_url=None, api_key=None):
        state = self.state() if state is None else state
        url = self.plugin.config.get("api_url", "") if api_url is None else api_url
        key = self.plugin.config.get("api_key", "") if api_key is None else api_key
        return bool(
            state.get("connection_mode") != "existing"
            and re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", "")))
            and state.get("api_url")
            and (not url or (
                self.connection_url(url) == self.connection_url(state["api_url"])
                and state.get("api_key") and key == state["api_key"]
            ))
        )

    def require_managed(self):
        self.ensure_local_installation()
        state = self.state()
        if not self.managed_connection(state):
            raise PanelError("此操作仅适用于插件安装的配套服务；已有服务仅提供连接与记忆功能。")
        return state

    def ensure_local_installation(self):
        if not local_installation_supported():
            raise PanelError("配套部署命令需要 Linux Docker Engine。请启动 Hindsight 后使用「连接已有服务」。")

    def upgrade_state(self, state=None):
        state = dict(self.state() if state is None else state)
        queue = self.plugin.outbox
        names = ("upgrade_check", "upgrade_pauses")
        records = [
            state[name] for name in names if state.get(name) and queue
            and state[name].get("connection_id") == queue.connection_id
        ]
        if records:
            retired = {
                (row["owner_id"], row["old_bank"], row["old_generation"])
                for row in queue.control.erasures(queue.connection_id)
            }
            if any(
                (bank.get("owner_id"), bank.get("bank"), bank.get("generation")) in retired
                for record in records for bank in record.get("banks", [])
            ):
                # Forgetting invalidates the snapshot, never the actual Bank pauses.
                for name in names:
                    if state.get(name) and state[name].get("connection_id") == queue.connection_id:
                        state.pop(name)
        return state

    def config_revision(self, setup=None):
        """Opaque fingerprint of the plugin configuration and setup metadata."""
        state = dict(self.state() if setup is None else setup)
        state.pop("connection_job", None)
        state.pop("auth_fingerprint", None)
        state.pop("auth_fingerprints", None)
        return sha256(json.dumps(
            {"config": dict(self.plugin.config), "setup": state},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))

    def connection_revision(self):
        return sha256(json.dumps([
            self.plugin.config.get(key, "") for key in (
                "api_url", "api_key", "service_id", "service_address_revision",
            )
        ]))

    def check_revision(self, payload):
        self.ensure_open()
        if "config_revision" in payload and payload["config_revision"] != self.config_revision():
            raise PanelError("配置已被更新，请刷新页面后重新保存。")

    def upgrade_progress(self, state):
        queue = self.plugin.outbox
        if not queue or not self.managed_connection(state):
            return False, False
        check, pauses = state.get("upgrade_check") or {}, state.get("upgrade_pauses") or {}
        return (
            check.get("connection_id") == queue.connection_id and not check.get("verified"),
            pauses.get("connection_id") == queue.connection_id,
        )

    def ensure_no_upgrade(self):
        if any(self.upgrade_progress(self.upgrade_state())):
            raise PanelError("服务升级尚未结束。请先核对升级结果并解除升级造成的暂停，或选择暂不升级，再修改连接、模型、账号或删除数据。")

    @staticmethod
    def connection_url(value):
        try:
            return str(httpx.URL(str(value).strip())).rstrip("/")
        except (TypeError, ValueError, httpx.InvalidURL):
            raise PanelError("服务地址格式无效，请填写完整的 HTTP 地址。") from None

    def control_plane_url(self, value, *, api_key=""):
        if not isinstance(value, str):
            raise PanelError("请填写以 http:// 或 https:// 开头的面板地址。")
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            url = httpx.URL(value)
            if (
                parsed.scheme.lower() not in {"http", "https"} or not url.host
                or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value or "\\" in value
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError
            # Management addresses are public display data, never credential transport.
            state = self.state()
            if any(str(secret) in value for secret in (
                self.plugin.config.get("api_key"), state.get("api_key"),
                state.get("management_key"), state.get("control_plane_key"),
                api_key,
            ) if secret):
                raise ValueError
            return str(url)
        except (ValueError, httpx.InvalidURL):
            raise PanelError(
                "请填写面板首页地址（以 http:// 或 https:// 开头），去掉 ? 或 # 后面的内容。"
                "登录密钥请在面板登录页输入。"
            ) from None

    def displayed_control_plane_url(self, value):
        try:
            return self.control_plane_url(value)
        except PanelError:
            return ""

    async def bootstrap(self):
        snapshot = self.poll_snapshot()
        async with self.plugin.using_resources() if snapshot.outbox else nullcontext():
            return await self._bootstrap(snapshot)

    def poll_snapshot(self):
        self.ensure_open()
        plugin = self.plugin
        if plugin.stopping:
            raise PanelError("连接正在切换，请稍后刷新。")
        setup = self.state()
        return PanelSnapshot(plugin.client, plugin.outbox, self.config_revision(setup), setup,
                             plugin.config.get("api_url", ""), plugin.config.get("api_key", ""))

    def check_snapshot(self, snapshot):
        self.ensure_open()
        plugin = self.plugin
        if (plugin.stopping or plugin.client is not snapshot.client or plugin.outbox is not snapshot.outbox
                or self.config_revision() != snapshot.revision):
            raise PanelError("连接或配置已变化，请刷新页面。")

    async def _bootstrap(self, snapshot):
        plugin, config = self.plugin, self.plugin.config
        client, queue, state = snapshot.client, snapshot.outbox, snapshot.setup
        connected, connection_message = None, ""
        if client and queue:
            try:
                async with asyncio.timeout(3):
                    await client.ensure_auth(force=bool(
                        queue.connection_error()
                        or getattr(client, "connection", {}).get("connected") is False
                    ))
                    self.check_snapshot(snapshot)
                    self.connection_ready(client.url, client.api_key)
                connected = getattr(client, "connection", {}).get("connected", True)
            except HindsightError as exc:
                connected = False
                connection_message = self.error_message(plugin.error_code(exc))
            except TimeoutError:
                connected = False
                connection_message = self.error_message("auth_timeout")
        self.check_snapshot(snapshot)
        scopes = await self.user_scopes()
        self.check_snapshot(snapshot)
        local_installation = local_installation_supported()
        managed = local_installation and self.managed_connection(state, api_url=snapshot.api_url, api_key=snapshot.api_key)
        installation_state = state if local_installation else {}
        key_command = ""
        if managed:
            key_command = (
                "sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' "
                '"${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/'
                f'astrbot-memory-{state["id"]}/.env"'
                r" | tr -d '\047\042'"
            )
        managed_status = None
        if managed and state.get("management_url") and state.get("management_key"):
            try:
                managed_status = await self.managed_request(state)
            except PanelError:
                managed_status = {"installed": False, "configured": False, "ready": False}
            self.check_snapshot(snapshot)
        missing = missing_connection(config)
        configured = not missing
        backend = await self._read_service_status(snapshot, force=True)
        self.check_snapshot(snapshot)
        if client and queue:
            evidence = getattr(client, "connection", {})
            connected = evidence.get("connected", connected)
            if connected is False:
                connection_message = self.error_message(evidence.get("code") or "connection_error")
            elif connected is True:
                connection_message = ""
        return {
            "config_revision": snapshot.revision,
            "capabilities": {
                "managed_installation": local_installation,
            },
            "parameters": settings.snapshot(plugin),
            "config": {
                "configured": configured,
                "missing_fields": missing,
                "api_url": config.get("api_url", "") or installation_state.get("api_url", ""),
                "control_plane_url": self.displayed_control_plane_url(
                    config.get("control_plane_url")
                    or (installation_state.get("control_plane_url", "") if not configured else "")
                ),
                "has_api_key": bool(config.get("api_key") or installation_state.get("api_key")),
                "connection_revision": self.connection_revision(),
                "allowed_accounts": config.get("allowed_accounts", []),
                "managed": managed,
                **{k: state.get(k, "") if managed else "" for k in (
                    "chat_provider_id", "embedding_provider_id", "rerank_provider_id"
                )},
            },
            "platforms": self.platforms(),
            "managed_service": managed_status,
            "backend": backend,
            "model_job": self.model_job if managed else None,
            "connection_job": self.managed_connections.public(state["connection_job"])
            if managed and isinstance(state.get("connection_job"), dict) else None,
            "installation_id": state.get("id", "") if managed else "",
            "has_installation": managed,
            "installation_api_url": state.get("api_url", "") if managed else "",
            "control_plane_key_command": key_command,
            "providers": self.providers().options() if local_installation and (managed or not configured) else {},
            "status": {
                "label": configuration_message(config) or connection_message or plugin.status,
                "message": connection_message,
                "connected": connected,
                "recovering": bool(plugin.outbox and plugin.outbox.recovering),
            },
            "scopes": scopes,
        }

    async def close(self):
        self.closed = True
        await self.bank_stats.close()
        tasks = self.requests | {self.model_task}
        tasks = {task for task in tasks if task is not None and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def write_config(self, updates):
        config = self.plugin.config
        for obsolete in ("enabled", "permission_config_version", "allowed_platform_ids", "allowed_user_ids", "timezone"):
            config.pop(obsolete, None)
        config.update(updates)
        config.save_config()

    def settings_saved(self):
        self.plugin.confirmations.clear()
        return {
            "saved": True, "config_revision": self.config_revision(),
            "message": "配置已保存并生效。正常聊天即可，无需再重载插件。",
        }

    @property
    def target_version(self):
        return HINDSIGHT_VERSION

    @staticmethod
    def supports_installation():
        return local_installation_supported()

    def new_client(self, url, key):
        fingerprint = HindsightClient.connection_fingerprint(url, key)
        state = self.state()
        verified = fingerprint == state.get("auth_fingerprint") or fingerprint in state.get("auth_fingerprints", [])
        return HindsightClient(url, key, auth_verified=verified)

    def remember_auth(self, client):
        if not isinstance(client.auth_verified, str) or not client.auth_verified:
            return
        state = self.state()
        verified = list(state.get("auth_fingerprints", []))
        if client.auth_verified not in verified:
            verified.append(client.auth_verified)
            state["auth_fingerprints"] = verified
            self.store_state(state)

    @staticmethod
    def installation_command(state, **options):
        return installation(Path(__file__).resolve().parent.parent, state, **options)
