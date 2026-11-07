"""AstrBot plugin-view connection, account and maintenance management."""

import asyncio
import copy
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from .client import HindsightClient, HindsightError
from .background import MAX_BACKGROUND_PAGES, ReadGuard, check_page, context_limits, reference, save_reference
from .diagnostics import (
    ConfigurationError, configuration_message, connection_fields, missing_connection,
    recall_timeout,
)
from .identity import Incoming, Scope, sha256
from .installer import HINDSIGHT_VERSION, SUPPORTED_VERSIONS, installation, uninstallation
from .outbox import QueueError
from .permissions import pairs
from .source import local_runner, platform_details, prepare_bot_identity, valid_user_id
from . import settings
from .materials import TokenizerError

NAME = "astrbot_plugin_hindsight_bridge"


class PanelError(ValueError):
    pass


class MemoryPanel:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        self.closed = False
        self.requests = set()
        self.model_task = None
        self.model_job = None
        self.model_selection = None
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
            "runtime.sqlite3", "outbox.sqlite3",
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
        state = self.state()
        if not self.managed_connection(state):
            raise PanelError("此操作仅适用于插件安装的配套服务；已有服务仅提供连接与记忆功能。")
        return state

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

    def config_revision(self):
        """Opaque fingerprint of the plugin configuration and setup metadata."""
        state = dict(self.state())
        state.pop("connection_job", None)
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

    def ensure_no_upgrade(self):
        state, queue = self.upgrade_state(), self.plugin.outbox
        check, pauses = state.get("upgrade_check"), state.get("upgrade_pauses")
        if queue and self.managed_connection(state) and (
            check and check.get("connection_id") == queue.connection_id and not check.get("verified")
            or pauses and pauses.get("connection_id") == queue.connection_id
        ):
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
        if self.model_task and not self.model_task.done():
            return await self._bootstrap()
        async with self.lock:
            return await self._bootstrap()

    async def service_status(self, *, force=True):
        plugin, state = self.plugin, self.upgrade_state()
        client = plugin.client
        managed = self.managed_connection(state) and (
            not client or self.connection_url(state["api_url"]) == client.url
        )
        ignored = state.get("ignored_upgrades", {})
        result = {
            "version": None, "target": HINDSIGHT_VERSION if managed else None,
            "managed": managed, "upgrade_available": False,
            "ignored": bool(managed and isinstance(ignored, dict)
                            and ignored.get(self.connection_revision()) == HINDSIGHT_VERSION),
        }
        if (not client or not plugin.outbox or plugin.stopping
                or not isinstance(getattr(client, "api_key", None), str)
                or not callable(getattr(client, "request", None))):
            return result
        identity = (
            client.url, sha256(client.api_key), self.config_revision(),
            bool(state.get("upgrade_check")), bool(state.get("upgrade_pauses")),
        )
        if not force and self.version_cache and self.version_cache[0] == identity and self.version_cache[1] > time.monotonic():
            return self.version_cache[2]
        try:
            async with plugin.using_resources(), asyncio.timeout(3):
                await client.ensure_auth()
                response = await client.request("GET", "/v1/version", timeout=2.5)
            version = response.get("api_version") if isinstance(response, dict) else None
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise PanelError("服务版本响应无效，请检查 Hindsight 服务。")
            result.update(version=version, supported=version in SUPPORTED_VERSIONS,
                          upgrade_available=managed and version == "1.1.0")
        except (HindsightError, TimeoutError, PanelError):
            result["message"] = "暂时无法核对服务版本，请检查连接后重试。"
        if plugin.client is not client or plugin.stopping:
            return {"version": None, "target": HINDSIGHT_VERSION, "upgrade_available": False}
        check = state.get("upgrade_check", {})
        result["pending"] = bool(
            managed and check and check.get("connection_id") == plugin.outbox.connection_id
            and not check.get("verified")
        )
        pauses = state.get("upgrade_pauses")
        result["upgrade_paused"] = bool(
            managed and pauses and pauses.get("connection_id") == plugin.outbox.connection_id
        )
        result["resume_available"] = bool(result["upgrade_paused"] and check.get("verified"))
        self.version_cache = (identity, time.monotonic() + 30, result)
        return result

    async def ignore_upgrade(self):
        from astrbot.api.web import request

        payload = await self.body()
        async with self.lock:
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            self.check_revision(payload)
            self.ensure_no_upgrade()
            self.require_managed()
            if (
                payload.get("target") != HINDSIGHT_VERSION
                or payload.get("connection_revision") != self.connection_revision()
            ):
                raise PanelError("服务连接或目标版本已变化，请重新获取升级提示。")
            state = self.state()
            if not isinstance(state.get("ignored_upgrades"), dict):
                state["ignored_upgrades"] = {}
            state.setdefault("ignored_upgrades", {})[self.connection_revision()] = HINDSIGHT_VERSION
            self.store_state(state)
            self.version_cache = None
            return {"config_revision": self.config_revision(), "message": "已忽略这个版本的升级提示。"}

    async def upgrade(self):
        from astrbot.api.web import request

        payload = await self.body()
        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(60):
            self.check_revision(payload)
            plugin, state = self.plugin, self.upgrade_state()
            status = await self.service_status()
            if not status.get("managed") or not plugin.outbox or plugin.outbox.recovering:
                raise PanelError("此入口只升级当前插件的配套服务；外部服务请由其管理员升级。")
            managed_status = await self.managed_request(state)
            if not managed_status["ready"]:
                raise PanelError("配套服务还不能处理记忆请求，请先恢复原服务连接。")
            queue, maintenance = plugin.outbox, plugin.worker.maintenance
            scopes = [row for row in queue.list_scopes()
                      if (owner := queue.control.owner(queue.connection_id, row["owner_id"]))
                      and owner["bank"] == row["bank"] and owner["revision"] == row["revision"]]
            unique = {row["bank"]: row for row in scopes}
            control_revision = queue.control.setting("revision")

            def recheck():
                self.check_revision(payload)
                if queue.control.setting("revision") != control_revision:
                    raise PanelError("记忆范围或暂停进度已变化；保持保存和查询暂停，刷新后重新检查，尚未确认升级结果。")
                if queue.in_flight or any(
                    row["phase"] != "verified" or row["cleanup_step"] != 7
                    for row in queue.control.erasures(queue.connection_id)
                ):
                    raise PanelError("仍有在途请求或旧记忆清理未完成；请等待处理完成后重新检查。")

            async def inspect_banks(*, allow_running=False):
                if queue.in_flight or not await maintenance.check(force=True):
                    raise PanelError("仍有在途请求或旧记忆清理未完成；请等待处理完成后重新检查。")
                banks = []
                for row in unique.values():
                    checked = await maintenance.status(row["scope_id"], management=queue.management(row["scope_id"]))
                    if (not checked["ready"] and not (allow_running and checked["phase"] == "inactive")) or checked.get("bank_missing"):
                        raise PanelError("仍有记录未完成保存或记忆库缺失；请在「用户与记忆」处理待保存或失败记录，并检查暂停进度。")
                    exists = await plugin.client.bank_exists(row["bank"])
                    uncreated = (
                        not exists and queue.control.bank_state(queue.connection_id, row["bank"]) == "new"
                        and not checked.get("remote_operations")
                        and not queue.control.db.execute(
                            "SELECT 1 FROM requests WHERE connection_id=? AND bank=? "
                            "AND (dispatched=1 OR accepted=1) LIMIT 1",
                            (queue.connection_id, row["bank"]),
                        ).fetchone()
                        and not await plugin.client.bank_list_contains(row["bank"])
                    )
                    if not exists and not uncreated:
                        raise PanelError("原记忆库未找到；保持保存和查询暂停并恢复原库，再重新检查。")
                    banks.append({**{key: row[key] for key in ("scope_id", "bank", "owner_id", "generation")},
                                  "existed": exists})
                return banks

            async def verify_original(check, *, allow_running=False):
                if check["connection_id"] != queue.connection_id or check["embedding_fingerprint"] != managed_status["embedding_fingerprint"]:
                    raise PanelError("服务连接或嵌入模型已变化；保持保存和查询暂停并恢复升级前配置。")
                if set(unique) != {saved["bank"] for saved in check["banks"]}:
                    raise PanelError("记忆库范围已变化；保持保存和查询暂停并核对原配置。")
                for saved in check["banks"]:
                    current = queue.scope_owner(saved["scope_id"])
                    if not current or any(current[key] != saved[key] for key in ("bank", "owner_id", "generation")):
                        raise PanelError("用户或记忆库归属已变化；保持保存和查询暂停并核对原配置。")
                inspected = {row["bank"]: row for row in await inspect_banks(allow_running=allow_running)}
                for saved in check["banks"]:
                    if saved["existed"] and not inspected[saved["bank"]]["existed"]:
                        raise PanelError("原记忆库未找到；保持保存和查询暂停并从升级前备份恢复，不能创建新库代替。")
                recheck()
                return inspected

            def restore_upgrade_pauses(inspected):
                pauses = state.get("upgrade_pauses")
                if not pauses:
                    return 0
                if (pauses["connection_id"] != queue.connection_id
                        or pauses["embedding_fingerprint"] != managed_status["embedding_fingerprint"]):
                    raise PanelError("升级前的连接或嵌入模型已变化；请保持暂停并核对原配置。")
                targets = []
                for saved in pauses["banks"]:
                    current = queue.scope_owner(saved["scope_id"])
                    if not current or any(current[key] != saved[key] for key in ("bank", "owner_id", "generation")):
                        raise PanelError("升级暂停的用户归属已变化；请保持暂停并核对原记忆库。")
                    record = queue.control.maintenance(queue.connection_id, saved["bank"])
                    if record and record["started_at"] == saved["started_at"]:
                        targets.append(current)
                recheck()
                # All remote checks finish before releasing any of this upgrade's pauses.
                with queue.control.transaction():
                    for row in targets:
                        if inspected[row["bank"]]["existed"]:
                            queue.control.set_bank_state(queue.connection_id, row["bank"], "established")
                        queue.control.finish_maintenance(queue.connection_id, row["owner_id"])
                queue.apply_control()
                state.pop("upgrade_pauses", None)
                plugin.worker.wake.set()
                return len(targets)

            action = payload.get("action")
            if action in {"verify", "resume"}:
                check = state.get("upgrade_check")
                if not check or status["version"] != HINDSIGHT_VERSION:
                    raise PanelError("尚未确认服务升级到 1.2.0；保持保存和查询暂停，执行命令后再核对。")
                if action == "resume" and not check.get("verified"):
                    raise PanelError("请先点击「核对升级结果」，确认版本与原记忆库后再解除暂停。")
                inspected = await verify_original(check, allow_running=action == "resume")
                if action == "resume":
                    count = restore_upgrade_pauses(inspected)
                    self.store_state(state)
                    self.version_cache = None
                    return {"message": f"已解除升级造成的 {count} 个用户暂停。记忆模式保持不变，升级前已有的暂停保留。",
                            "config_revision": self.config_revision(), "resumed": count}
                state["upgrade_check"]["verified"] = True
                self.store_state(state)
                self.version_cache = None
                message = ("版本与原记忆库核对通过。确认旧记忆可用后，点击「解除升级暂停」。"
                           if state.get("upgrade_pauses") else
                           "版本与原记忆库核对通过。旧版安装记录未区分暂停来源，请在用户与记忆中逐个查看暂停状态并解除读写暂停。")
                return {"message": message,
                        "config_revision": self.config_revision(), "verified": True}
            if status["version"] != "1.1.0":
                raise PanelError("此升级入口只支持实际运行的 Hindsight 1.1.0；请重新检查版本。")
            if action == "cancel":
                if state.get("upgrade_check"):
                    inspected = await verify_original(state["upgrade_check"], allow_running=True)
                else:
                    inspected = {row["bank"]: row for row in await inspect_banks(allow_running=True)}
                count = restore_upgrade_pauses(inspected)
                state.pop("upgrade_check", None)
                self.store_state(state)
                self.version_cache = None
                return {"message": f"继续使用 1.1.0，已解除升级造成的 {count} 个用户暂停。原有暂停保留；已复制的升级命令不会撤销，请勿再执行。",
                        "config_revision": self.config_revision()}
            if action == "begin":
                pauses = state.setdefault("upgrade_pauses", {
                    "connection_id": queue.connection_id, "embedding_fingerprint": managed_status["embedding_fingerprint"], "banks": [],
                })
                if pauses["connection_id"] != queue.connection_id:
                    raise PanelError("升级暂停记录属于另一套连接，请核对原配置。")
                with queue.control.transaction():
                    for row in unique.values():
                        if not queue.control.maintenance(queue.connection_id, row["bank"]):
                            record = queue.control.begin_maintenance(queue.connection_id, row["owner_id"])
                            pauses["banks"] = [saved for saved in pauses["banks"] if saved["bank"] != row["bank"]]
                            pauses["banks"].append({
                                **{key: row[key] for key in ("scope_id", "bank", "owner_id", "generation")},
                                "started_at": record["started_at"],
                            })
                    self.store_state(state)
                queue.apply_control()
                plugin.worker.wake.set()
                self.version_cache = None
                return {"message": "已为本插件的所有记忆库暂停保存和查询。待保存或失败记录不会丢弃；处理完成后再生成升级命令。",
                        "config_revision": self.config_revision()}
            if action != "prepare" or payload.get("backup_confirmed") is not True:
                raise PanelError("请先备份插件业务数据和最新控制数据，并确认停止其他客户端及官方面板写入。")
            banks = await inspect_banks()
            recheck()
            result = installation(Path(__file__).resolve().parent.parent, state,
                                  container_hostname=socket.gethostname() if Path("/.dockerenv").exists() else "",
                                  upgrade=True)
            state["upgrade_check"] = {"banks": banks, "verified": False, "from": "1.1.0", "to": HINDSIGHT_VERSION,
                                      "connection_id": queue.connection_id,
                                      "embedding_fingerprint": managed_status["embedding_fingerprint"]}
            self.store_state(state)
            self.version_cache = None
            return {"command": result["command"], "message": result["message"],
                    "config_revision": self.config_revision()}

    async def user_scopes(self):
        plugin, scopes = self.plugin, []
        accounts = pairs(plugin.config.get("allowed_accounts", []))
        queue = plugin.outbox if not plugin.stopping else None
        if queue:
            plugin.refresh_permissions()
            scopes = [dict(row) for row in queue.list_scopes()]
        bot_ids = {platform: await self.bot_id(platform) for platform in sorted({p for p, _ in accounts})}
        if queue:
            scopes = [dict(row) for row in queue.list_scopes()]
        known = set()
        for scope in scopes:
            source = Scope(*json.loads(scope["identity_json"]))
            account = (source.platform_instance_id, source.user_id)
            bot = bot_ids.get(source.platform_instance_id)
            current = bool(
                scope["allowed"] and bot == source.bot_id
            )
            scope["historical"] = not current
            scope["availability"] = (
                "removed" if account not in accounts else
                "disconnected" if not bot else
                "historical" if bot != source.bot_id else
                "confirmation" if not scope["allowed"] else "ready"
            )
            if current:
                known.add((source.platform_instance_id, source.user_id))
        for platform, user in sorted(accounts - known):
            if queue and not queue.recovering:
                if bot_ids[platform] and plugin.outbox is queue and not plugin.stopping:
                    incoming = Incoming(
                        next((row["logical_type"] for row in self.platforms()
                              if row["id"] == platform), ""),
                        platform, bot_ids[platform], user, "", int(time.time()), "",
                    )
                    if queue.access.permitted(incoming.scope(queue.deployment_id)):
                        current = dict(queue.scope(incoming))
                        if not any(row["scope_id"] == current["scope_id"] for row in scopes):
                            scopes.append(current)
                        known.add((platform, user))
                        continue
            scopes.append({
                "scope_id": "pending-" + sha256(json.dumps([
                    plugin.config.get("service_id", ""), platform, user,
                ])),
                "platform_instance_id": platform, "user_id": user,
                "mode": "active", "bank": "", "pending": True,
                "pending_reason": "recovering" if queue else "service",
            })
            if queue and not queue.recovering:
                scopes[-1]["pending_reason"] = "authorization" if bot_ids.get(platform) else "platform"
        result = []
        for scope in scopes:
            summary = queue.summary(scope["scope_id"]) if queue and not scope.get("pending") else {
                "counts": {}, "last_error": "",
            }
            summary["error_message"] = self.error_message(summary["last_error"]) if summary["last_error"] else ""
            summary["recall"] = plugin.recall_status(scope) if queue and not scope.get("pending") else None
            if summary["recall"]:
                summary["recall"]["error_message"] = (
                    self.error_message(summary["recall"]["code"]) if summary["recall"]["code"] else ""
                )
            result.append({key: scope[key] for key in (
                "scope_id", "platform_instance_id", "user_id", "mode", "bank",
            )} | {
                "pending": scope.get("pending", False),
                "pending_reason": scope.get("pending_reason", ""),
                "historical": scope.get("historical", False),
                "availability": scope.get("availability", scope.get("pending_reason", "")),
                "bot_id": Scope(*json.loads(scope["identity_json"])).bot_id if scope.get("identity_json") else "",
                "bank_state": queue.control.bank_state(queue.connection_id, scope["bank"])
                if queue and not scope.get("pending") else "",
                **{key: scope.get(key) for key in ("owner_id", "generation", "revision")},
                "summary": summary,
            })
            if queue and not scope.get("pending"):
                try:
                    selection = reference(queue, scope)
                    result[-1]["background"] = {
                        **selection, "scope_revision": scope["revision"],
                    }
                    if selection.get("invalidated"):
                        result[-1]["background"]["message"] = self.error_message("background_scope_changed")
                    state = getattr(plugin, "background_outcomes", {}).get(
                        (queue.connection_id, scope["owner_id"]), {},
                    )
                    if (
                        state.get("bank") == scope["bank"] and state.get("revision") == scope["revision"]
                        and state.get("selection_revision") == selection["revision"]
                        and selection["enabled"] and not selection.get("invalidated")
                        and not summary.get("maintenance") and not scope.get("historical")
                    ):
                        result[-1]["background"]["status"] = {
                            "used": state["used"], "checked_at": state["checked_at"],
                            "message": "上次聊天已使用知识页。" if state["used"] else (
                                "上次聊天未使用知识页：" + self.error_message(state["code"])
                                if state["code"] else "上次聊天未使用知识页。"
                            ) + ("超出：" + "、".join(state["exceeded"]) + "。请在插件设置调整对应上限。"
                                 if state.get("exceeded") else ""),
                        }
                        per_page = state.get("pages") or [
                            {**page, "code": state["code"], "used": False}
                            for page in selection["pages"]
                        ]
                        result[-1]["background"]["status"]["pages"] = [{
                            "page_id": item["page_id"], "used": item["used"],
                            "code": item["code"], "exceeded": item.get("exceeded", []),
                            "message": "上次聊天已使用。" if item["used"] else (
                                self.error_message(item["code"]) if item["code"] else "上次聊天未使用。"
                            ),
                        } for item in per_page]
                        if len(selection["pages"]) > 1:
                            used = sum(item["used"] for item in per_page)
                            result[-1]["background"]["status"]["message"] = (
                                f"上次聊天使用 {used}/{len(selection['pages'])} 篇知识页。"
                            )
                except QueueError as exc:
                    result[-1]["background"] = {"error": self.error_message(str(exc))}
        changed_accounts = {
            (row["platform_instance_id"], row["user_id"]) for row in result
            if row.get("background", {}).get("invalidated")
        }
        for row in result:
            if (
                not row["historical"] and not row.get("background", {}).get("revision", 0)
                and (row["platform_instance_id"], row["user_id"]) in changed_accounts
            ):
                row["background"] = {
                    **row.get("background", {}), "enabled": False, "invalidated": True,
                    "message": self.error_message("background_scope_changed"),
                }
        return result

    async def bot_id(self, platform):
        contract = next((row for row in self.platforms() if row["id"] == platform), None)
        if contract and contract["logical_type"] == "qq_official":
            await self.plugin.prepare_sources()
            identity = self.plugin.source_accounts.get(platform, {}).get("bot_id", "")
        else:
            identity = await prepare_bot_identity(self.plugin.context, platform)
        queue = self.plugin.outbox
        if identity and queue:
            contract = next((row for row in self.platforms() if row["id"] == platform), None)
            if contract and queue.access.observe_bot(platform, contract["logical_type"], identity):
                queue.sync_permissions(pairs(self.plugin.config.get("allowed_accounts", [])))
                self.plugin.confirmations.clear()
        return identity

    async def authorize(self):
        from astrbot.api.web import request

        payload = await self.body()
        async with self.lock, self.plugin.using_resources():
            self.ensure_no_upgrade()
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            self.check_revision(payload)
            plugin, queue = self.plugin, self.plugin.outbox
            platform, user = payload.get("platform_instance_id"), payload.get("user_id")
            if (platform, user) not in pairs(plugin.config.get("allowed_accounts", [])):
                raise PanelError("请先添加用户，再确认机器人身份。")
            plugin.refresh_permissions()
            contract = next((row for row in self.platforms() if row["id"] == platform), None)
            if not contract or not contract["supported"] or not contract["enabled"] or queue.recovering:
                raise PanelError("平台尚不可用或数据正在恢复，请稍后重试。")
            revision = self.config_revision()
            grant = queue.access.authorization(platform, user)
            if not grant:
                raise PanelError("尚未取得账号确认信息，请刷新页面后重新确认账号。")
            bot = await self.bot_id(platform)
            if not bot or revision != self.config_revision() or plugin.outbox is not queue:
                raise PanelError("无法确认机器人身份，请连接机器人后刷新页面重试。")
            latest = queue.access.authorization(platform, user)
            if latest["revision"] != grant["revision"]:
                raise PanelError("授权已变化，请重新确认。")
            incoming = Incoming(contract["logical_type"], platform, bot, user, "", int(time.time()), "")
            key = "authorize:" + sha256(json.dumps([platform, user]))
            if payload.get("action") == "prepare":
                token = secrets.token_hex(16)
                plugin.confirmations[key] = {
                    "token": token, "username": request.username, "source": incoming.scope(queue.deployment_id).id,
                    "config_revision": revision, "revision": grant["revision"],
                    "control_revision": queue.control.setting("revision"),
                    "until": time.monotonic() + 120,
                }
                return {"confirmation": token, "bot_id": bot, "message": f"将为机器人 {bot} 授权用户 {user}。"}
            old = plugin.confirmations.pop(key, None)
            if (
                payload.get("action") != "confirm" or not old or old["until"] < time.monotonic()
                or old["username"] != request.username or old["config_revision"] != revision
                or old["source"] != incoming.scope(queue.deployment_id).id
                or old["revision"] != grant["revision"]
                or old["control_revision"] != queue.control.setting("revision")
                or not secrets.compare_digest(old["token"], str(payload.get("confirmation", "")))
            ):
                raise PanelError("授权确认已失效，请重新确认当前机器人身份。")
            await plugin.prepare_sources()
            evidence = plugin.source_accounts.get(platform, {})
            if (
                evidence.get("bot_id") != bot or plugin.outbox is not queue
                or revision != self.config_revision()
                or queue.access.authorization(platform, user)["revision"] != grant["revision"]
            ):
                raise PanelError("机器人身份或授权已变化，请刷新后重新确认。")
            queue.activate(incoming, grant["revision"])
            plugin.worker.wake.set()
            return {"message": "账号已授权，从现在开始接收新消息。"}

    async def _bootstrap(self):
        plugin, config = self.plugin, self.plugin.config
        connected, connection_message = None, ""
        if plugin.client and plugin.outbox and not plugin.stopping:
            try:
                async with plugin.using_resources(), asyncio.timeout(3):
                    await plugin.client.ensure_auth(force=bool(
                        plugin.outbox.connection_error()
                        or getattr(plugin.client, "connection", {}).get("connected") is False
                    ))
                    self.connection_ready(plugin.client.url, plugin.client.api_key)
                connected = getattr(plugin.client, "connection", {}).get("connected", True)
            except HindsightError as exc:
                connected = False
                connection_message = self.error_message(plugin.error_code(exc))
            except TimeoutError:
                connected = False
                connection_message = self.error_message("auth_timeout")
        scopes = await self.user_scopes()
        state = self.state()
        managed = self.managed_connection(state)
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
        missing = missing_connection(config)
        configured = not missing
        backend = await self.service_status(force=True)
        if plugin.client and plugin.outbox and not plugin.stopping:
            evidence = getattr(plugin.client, "connection", {})
            connected = evidence.get("connected", connected)
            if connected is False:
                connection_message = self.error_message(evidence.get("code") or "connection_error")
            elif connected is True:
                connection_message = ""
        return {
            "config_revision": self.config_revision(),
            "parameters": settings.snapshot(plugin),
            "config": {
                "configured": configured,
                "missing_fields": missing,
                "api_url": config.get("api_url", "") or state.get("api_url", ""),
                "control_plane_url": self.displayed_control_plane_url(
                    config.get("control_plane_url")
                    or (state.get("control_plane_url", "") if not configured else "")
                ),
                "has_api_key": bool(config.get("api_key") or state.get("api_key")),
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
            "connection_job": self.public_connection_job(state["connection_job"])
            if managed and isinstance(state.get("connection_job"), dict) else None,
            "installation_id": state.get("id", "") if managed else "",
            "has_installation": managed,
            "installation_api_url": state.get("api_url", "") if managed else "",
            "control_plane_key_command": key_command,
            "providers": self.providers().options() if managed or not configured else {},
            "status": {
                "label": configuration_message(config) or connection_message or plugin.status,
                "message": connection_message,
                "connected": connected,
                "recovering": bool(plugin.outbox and plugin.outbox.recovering),
            },
            "scopes": scopes,
        }

    def credentials(self, payload):
        url = payload.get("api_url", self.plugin.config.get("api_url", ""))
        key = payload.get("api_key", "")
        if not isinstance(url, str) or not isinstance(key, str):
            raise PanelError("请填写有效的服务地址与 Key。")
        url, key = url.strip(), key.strip()
        if not key:
            if self.connection_url(url) == self.connection_url(self.plugin.config.get("api_url", "")):
                key = str(self.plugin.config.get("api_key", ""))
            state = self.state()
            if not key and self.connection_url(url) == self.connection_url(state.get("api_url", "")):
                key = str(state.get("api_key", ""))
        if not url or not key:
            raise PanelError("请填写服务地址与 Key，或先完成配套服务安装。")
        return url, key

    async def check(self):
        payload = await self.body()
        url, key = self.credentials(payload)
        state = self.state()
        if state.get("management_url") and self.managed_connection(state, api_url=url, api_key=key):
            status = await self.managed_request(state)
            if not status["ready"]:
                return {"ok": True, "ready": False, "message": "配套服务已安装，请选择模型并保存。"}
        client = HindsightClient(url, key)
        try:
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
        finally:
            await client.close()
        self.connection_ready(client.url, key)
        return {"ok": True, "ready": True, "message": "服务连接与 API 认证通过。"}

    async def managed_request(self, state, environment=None):
        url, key = state.get("management_url"), state.get("management_key")
        if not url or not key:
            raise PanelError("请先生成并执行配套服务安装命令。")
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=360 if environment is not None else 3,
            ) as http:
                headers = {"Authorization": "Bearer " + key}
                response = await http.get(url + "/status", headers=headers)
                response.raise_for_status()
                status = response.json()
                if status.get("installation_id") != state.get("id") or status.get("protocol") != 1:
                    raise PanelError("配套服务标识不匹配，请检查安装记录。")
                if environment is not None:
                    response = await http.post(url + "/models", json={"environment": environment}, headers=headers)
                    if response.status_code == 400:
                        raise PanelError(response.json().get("message", "模型配置应用失败。"))
                    response.raise_for_status()
                    status = response.json()
                    if status.get("installation_id") != state.get("id") or status.get("protocol") != 1:
                        raise PanelError("配套服务标识不匹配，请检查安装记录。")
                return {
                    "installed": True, "configured": status.get("configured") is True,
                    "ready": status.get("ready") is True,
                    "embedding_fingerprint": status.get("embedding_fingerprint", ""),
                    "configuration_fingerprint": status.get("configuration_fingerprint"),
                    "applying": status.get("applying") is True,
                    "applying_fingerprint": status.get("applying_fingerprint", ""),
                }
        except PanelError:
            raise
        except httpx.TimeoutException:
            raise PanelError("配套服务响应超时，请稍后重试。") from None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise PanelError("无法连接配套服务。请先执行安装命令；已安装则检查服务是否运行。") from None

    async def prepare(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            self.ensure_no_upgrade()
            explicit = payload.get("mode") == "managed"
            if not explicit and (self.state().get("api_url") or self.plugin.config.get("api_url")):
                self.require_managed()
            status = await self.service_status()
            if status.get("managed") and status.get("upgrade_available"):
                raise PanelError("当前服务为 1.1.0，请使用「升级 Hindsight」暂停保存和查询、备份并升级。")
            if (status.get("managed") and status.get("version") is None
                    and self.plugin.config.get("api_url") and payload.get("repair") is not True):
                raise PanelError("无法确认已有服务版本。请恢复连接；重新部署命令只会替换实际确认的 1.2.0 服务，不会升级未知版本。")
            state = self.state(create=True)
            if "control_plane_url" in payload:
                state["control_plane_url"] = self.control_plane_url(payload["control_plane_url"])
            result = installation(
                Path(__file__).resolve().parent.parent, state,
                container_hostname=socket.gethostname() if Path("/.dockerenv").exists() else "",
            )
            state["api_url"] = result["api_url"]
            state["management_url"] = result["management_url"]
            control_plane_url = self.control_plane_url(result.get("control_plane_url", ""))
            if control_plane_url:
                state["control_plane_url"] = control_plane_url
            self.store_state(state)
            return {
                "command": result["command"], "message": result["message"],
                "api_url": result["api_url"], "config_revision": self.config_revision(),
                "control_plane_url": control_plane_url,
                "installation_id": state["id"],
            }

    def managed_target(self, payload):
        self.ensure_open()
        state = self.state()
        if payload.get("mode") != "managed" or payload.get("installation_id") != state.get("id"):
            raise PanelError("配套安装记录已变化，请重新选择配套服务。")
        if not re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", ""))) or not all(
            isinstance(state.get(key), str) and state[key] for key in ("api_url", "api_key", "management_url", "management_key")
        ):
            raise PanelError("请先生成并执行配套服务安装命令。")
        return state

    @staticmethod
    def public_connection_job(job):
        result = {key: job[key] for key in ("request_id", "status", "stage", "message", "saved") if key in job}
        result["config_revision"] = job.get("config_revision", job.get("expected_revision", ""))
        return result

    async def managed_options(self):
        payload = await self.body()
        self.check_revision(payload)
        if payload.get("mode") != "managed":
            raise PanelError("请先选择安装配套服务。")
        state = self.state()
        present = bool(re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", ""))) and state.get("api_url"))
        return {"installation_id": state.get("id", "") if present else "", "api_url": state.get("api_url", "") if present else "",
                "has_installation": present, "providers": self.providers().options(),
                "selection": {key: state.get(key, "") for key in ("chat_provider_id", "embedding_provider_id", "rerank_provider_id")},
                "job": self.public_connection_job(state["connection_job"]) if isinstance(state.get("connection_job"), dict) else None}

    def persist_connection_job(self, job, **updates):
        state = self.state()
        stored = state.get("connection_job", {})
        if stored.get("request_id") != job["request_id"] or state.get("id") != job["installation_id"]:
            raise PanelError("配套接入任务已变化，请刷新页面。")
        job.update(updates)
        state["connection_job"] = dict(job)
        self.store_state(state)
        self.model_job = self.public_connection_job(job)

    def check_connection_job(self, job):
        self.ensure_open()
        state = self.managed_target({"mode": "managed", "installation_id": job["installation_id"]})
        if job["expected_revision"] != self.config_revision():
            raise PanelError("接入期间配置已变化，请重新保存并连接。")
        target_service = state.get("connections", {}).get(self.connection_url(state["api_url"]))
        if target_service and any((state.get(key) or {}).get("connection_id") == target_service
                                  for key in ("upgrade_check", "upgrade_pauses")):
            raise PanelError("配套升级尚未结束，请先核对升级结果并解除升级暂停。")
        return state

    async def managed_connect(self):
        payload = await self.body()
        self.managed_target(payload)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
            raise PanelError("请求标识无效，请重新保存并连接。")
        selection = {key: payload.get(key, "") for key in ("chat_provider_id", "embedding_provider_id", "rerank_provider_id")}
        if self.model_task and not self.model_task.done():
            if selection != self.model_selection:
                raise PanelError("模型配置正在处理，请等待完成后再更换模型。")
            return dict(self.model_job)
        async with self.lock:
            state = self.managed_target(payload)
            old = state.get("connection_job")
            if isinstance(old, dict) and old.get("request_id") == request_id:
                if old.get("selection") != selection:
                    raise PanelError("同一请求的模型选择已变化，请重新提交。")
                if old.get("status") == "succeeded":
                    self.check_connection_job(old)
                    return self.public_connection_job(old)
                job = dict(old)
            else:
                self.check_revision(payload)
                job = {"request_id": request_id, "installation_id": state["id"], "selection": selection,
                       "expected_revision": self.config_revision(), "fingerprint": "", "stage": "validating"}
                state["connection_job"] = job
                self.store_state(state)
            self.check_connection_job(job)
            self.persist_connection_job(job, status="running", message="正在检查模型与配套服务…")
            self.model_selection = selection
            self.model_task = asyncio.create_task(self.run_connection_job(job), name="hindsight-managed-connect")
            return dict(self.model_job)

    async def managed_status(self):
        payload = await self.body()
        state = self.managed_target(payload)
        job = state.get("connection_job")
        if not isinstance(job, dict):
            return {"status": "idle"}
        if job.get("status") in {"running", "interrupted"} and (not self.model_task or self.model_task.done()):
            async with self.lock:
                state = self.managed_target(payload)
                job = state.get("connection_job")
                if (not isinstance(job, dict) or job.get("status") not in {"running", "interrupted"}
                        or self.model_task and not self.model_task.done()):
                    return self.public_connection_job(job) if isinstance(job, dict) else {"status": "idle"}
                self.check_connection_job(job)
                self.model_selection = job["selection"]
                self.persist_connection_job(job, status="running", message="正在核对接入结果…")
                self.model_task = asyncio.create_task(self.run_connection_job(job), name="hindsight-managed-connect")
        return self.public_connection_job(self.state()["connection_job"])

    async def managed_version(self):
        state = self.managed_target(await self.body())
        status = await self.managed_request(state)
        result = {"managed": True, "target": HINDSIGHT_VERSION, "version": None, "upgrade_available": False}
        if not status["configured"] or not status["ready"]:
            return {**result, "message": "配套组件已安装，请先选择模型并点击「保存并连接」。"}
        client = HindsightClient(state["api_url"], state["api_key"])
        try:
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
                response = await client.request("GET", "/v1/version")
                version = response.get("api_version")
                if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                    raise PanelError("服务版本响应无效，请检查 Hindsight 服务。")
                return {**result, "version": version, "supported": version in SUPPORTED_VERSIONS,
                        "upgrade_available": version == "1.1.0"}
        finally:
            await client.close()

    async def run_connection_job(self, job):
        plugin = self.plugin
        closed_active = False
        try:
            async with self.lock:
                state = self.check_connection_job(job)
                status = await self.managed_request(state)
                if status.get("configuration_fingerprint") is None:
                    raise PanelError("配套组件不支持接入结果核对，请先重新部署配套服务文件，再保存并连接。")
                deadline = time.monotonic() + 360
                while status.get("applying"):
                    if not job["fingerprint"] or status.get("applying_fingerprint") != job["fingerprint"]:
                        raise PanelError("配套服务正在应用其他模型配置，请等待完成后重试。")
                    if time.monotonic() >= deadline:
                        raise PanelError("模型应用仍在进行，请稍后重新打开页面核对结果。")
                    await asyncio.sleep(2)
                    self.check_connection_job(job)
                    status = await self.managed_request(state)
                if not job["fingerprint"] or status.get("configuration_fingerprint") != job["fingerprint"] or not status["ready"]:
                    environment = await self.providers().resolve_checked(job["selection"])
                    self.check_connection_job(job)
                    fingerprint = hmac.new(state["api_key"].encode(), json.dumps(
                        environment, sort_keys=True, separators=(",", ":"),
                    ).encode(), hashlib.sha256).hexdigest()
                    if job["fingerprint"] and job["fingerprint"] != fingerprint:
                        raise PanelError("模型供应商配置已变化，请重新提交保存并连接。")
                    self.persist_connection_job(job, fingerprint=fingerprint, stage="applying", message="正在验证模型并启动记忆服务…")
                    if (self.connection_url(plugin.config.get("api_url", "")) == self.connection_url(state["api_url"])
                            and plugin.config.get("api_key") == state["api_key"] and plugin.outbox):
                        plugin.stopping = True
                        closed_active = True
                        await plugin._close_resources()
                        self.check_connection_job(job)
                    status = await self.managed_request(state, environment)
                    self.check_connection_job(job)
                    if not status["ready"] or status.get("configuration_fingerprint") != fingerprint:
                        raise PanelError("模型应用结果尚未确认，请稍后重试核对。")
                state = self.check_connection_job(job)
                state.update(job["selection"])
                self.store_state(state)
                self.persist_connection_job(job, expected_revision=self.config_revision(), stage="connecting", message="模型已保存，正在连接插件…")
                client = plugin.client
                active = (
                    self.managed_connection(state) and plugin.outbox and not plugin.stopping and client
                    and client.url == self.connection_url(state["api_url"])
                    and client.api_key == state["api_key"]
                    and plugin.outbox.connection_id == plugin.config.get("service_id")
                )
                if closed_active or not active:
                    await self._save({"api_url": state["api_url"], "api_key": state["api_key"], "mode": "managed"},
                                     self.config_revision(), force_initialize=True, job=job)
                else:
                    async with plugin.using_resources(), asyncio.timeout(10):
                        await plugin.client.ensure_auth(force=True)
                    self.check_connection_job(job)
                self.persist_connection_job(job, expected_revision=self.config_revision(), status="succeeded", stage="complete", saved=True,
                                            config_revision=self.config_revision(), message="模型已保存，插件已连接。")
        except asyncio.CancelledError:
            self.persist_connection_job(job, status="interrupted", message="接入已中断，重新打开页面可核对结果。")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, (PanelError, ValueError)) else "接入失败，请检查配套服务和插件数据目录后重试。"
            self.persist_connection_job(job, status="failed", message=message)
        finally:
            if closed_active and not plugin.outbox and not self.closed and not asyncio.current_task().cancelling():
                plugin.permissions = None
                plugin.stopping = False
                await plugin.initialize()

    async def save_models(self):
        payload = await self.body()
        self.require_managed()
        selection = {key: payload.get(key, "") for key in (
            "chat_provider_id", "embedding_provider_id", "rerank_provider_id",
        )}
        if self.model_task and not self.model_task.done():
            if selection != self.model_selection:
                raise PanelError("模型配置正在处理，请等待完成后再更换模型。")
            return dict(self.model_job)
        if payload.get("background") is True:
            # Retrying a lost start response must not restart the service twice.
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
                raise PanelError("请求标识无效，请重新保存模型。")
            if self.model_job and self.model_job["request_id"] == request_id:
                if selection != self.model_selection:
                    raise PanelError("模型选择已变化，请重新提交配置。")
                return dict(self.model_job)
            self.check_revision(payload)
            self.model_selection = selection
            self.model_job = {
                "request_id": request_id, "status": "running",
                "message": "正在检查模型连接…",
            }
            self.model_task = asyncio.create_task(
                self.run_model_job(payload), name="hindsight-model-setup",
            )
            return dict(self.model_job)
        return await self.apply_models(payload)

    async def model_status(self):
        return dict(self.model_job) if self.managed_connection() and self.model_job else {"status": "idle"}

    async def run_model_job(self, payload):
        try:
            result = await self.apply_models(payload)
            self.model_job.update(result, status="succeeded")
        except asyncio.CancelledError:
            self.model_job.update(status="interrupted", message="模型配置检查已中断，请重新检查服务状态后重试。")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, PanelError) else (
                str(exc) if isinstance(exc, ValueError) else "模型验证或保存失败，请检查模型服务后重试。"
            )
            self.model_job.update(status="failed", message=message)

    async def close(self):
        self.closed = True
        tasks = self.requests | {self.model_task}
        tasks = {task for task in tasks if task is not None and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def apply_models(self, payload):
        async with self.lock:
            self.ensure_no_upgrade()
            self.check_revision(payload)
            state = self.require_managed()
            await self.managed_request(state)
            selection = {key: payload.get(key, "") for key in (
                "chat_provider_id", "embedding_provider_id", "rerank_provider_id",
            )}
            environment = await self.providers().resolve_checked(selection)
            self.check_revision(payload)
            if self.model_job and self.model_job["status"] == "running":
                self.model_job["message"] = "正在验证模型并启动记忆服务…"
            plugin = self.plugin
            active = self.connection_url(plugin.config.get("api_url", "")) == self.connection_url(state.get("api_url", ""))
            if active:
                plugin.stopping = True
                await plugin._close_resources()
            try:
                self.ensure_open()
                result = await self.managed_request(state, environment)
                self.ensure_open()
                if not result["ready"]:
                    raise PanelError("模型配置已接收，但服务还不能处理记忆请求，请等待启动完成后重试。")
                state.update(selection)
                self.store_state(state)
            finally:
                if active:
                    plugin.permissions = None
                    # Plugin unload cancels this job and must not reopen resources.
                    if not self.closed and not asyncio.current_task().cancelling():
                        plugin.stopping = False
                        await plugin.initialize()
            return {"saved": True, "config_revision": self.config_revision(), "message": "模型配置已保存并生效。"}

    async def prepare_uninstall(self):
        payload = await self.body()
        async with self.lock:
            self.ensure_no_upgrade()
            self.check_revision(payload)
            state = self.require_managed()
            if payload.get("delete_memory_data") is not True:
                raise PanelError("请确认删除这套配套服务及全部记忆数据。")
            return uninstallation(state)

    async def save(self):
        payload = await self.body()
        async with self.lock:
            self.ensure_no_upgrade()
            self.check_revision(payload)
            revision = self.config_revision()
            return await self._save(payload, revision)

    async def save_parameters(self):
        from astrbot.api.web import request

        payload = await self.body()
        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")
        async with self.lock:
            if set(payload) != {"config_revision", "parameters"}:
                raise PanelError("请只提交记忆参数，并重新加载记忆设置后保存。")
            self.check_revision(payload)
            revision = self.config_revision()
            try:
                parameters = await settings.validate(payload["parameters"])
            except TokenizerError:
                raise PanelError("「查询分词编码」加载失败，请检查编码名称、网络和缓存目录权限后重试。") from None
            self.check_revision(payload)
            if self.plugin.stopping or self.model_task and not self.model_task.done():
                raise PanelError("插件正在重载或连接服务，请等待完成后重新保存记忆设置。")
            plugin = self.plugin
            previous = copy.deepcopy(dict(plugin.config))
            try:
                # Only local parameters change; existing queue and client stay alive.
                plugin.config.update(parameters)
                plugin.config.save_config()
            except Exception:
                plugin.config.clear()
                plugin.config.update(previous)
                try:
                    plugin.config.save_config()
                except Exception:
                    raise PanelError("记忆设置写入失败，原配置文件也无法恢复；请检查配置文件权限与磁盘空间，并从备份恢复插件配置。") from None
                raise PanelError("记忆设置保存失败，请检查插件配置文件权限与磁盘空间后重试。") from None
            settings.apply(plugin, parameters)
            return {
                "saved": True, "previous_revision": revision,
                "config_revision": self.config_revision(),
                "parameters": settings.snapshot(plugin),
                "message": "记忆设置已保存，下次读取和接收新记录时生效，无需重载插件。",
            }

    async def save_access(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            address = self.control_plane_url(payload.get("control_plane_url", ""))
            previous = copy.deepcopy(dict(self.plugin.config))
            try:
                self.write_config({"control_plane_url": address})
            except Exception:
                self.plugin.config.clear()
                self.plugin.config.update(previous)
                self.plugin.config.save_config()
                raise PanelError("管理地址保存失败，请检查插件数据目录后重试。") from None
            return {"saved": True, "config_revision": self.config_revision(), "message": "官方管理地址已保存。"}

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

    async def _save(self, payload, revision, *, force_initialize=False, job=None):
        self.ensure_open()
        plugin = self.plugin
        try:
            recall_timeout(plugin.config)
            context_limits(plugin.config)
        except ConfigurationError as exc:
            raise PanelError(self.error_message(str(exc))) from None
        control_plane_url = self.control_plane_url(
            payload.get("control_plane_url", plugin.config.get("control_plane_url", "")),
            api_key=payload.get("api_key", ""),
        )
        accounts = payload.get("allowed_accounts", plugin.config.get("allowed_accounts", []))
        selected = pairs(accounts)
        valid_platforms = {row["id"]: row for row in self.platforms()}
        previous_accounts = pairs(plugin.config.get("allowed_accounts", []))
        for platform, user in selected:
            contract = valid_platforms.get(platform)
            if (platform, user) in previous_accounts and (
                not contract or not contract["supported"] or not contract["enabled"]
            ):
                continue
            if not contract or not contract["supported"] or not contract["enabled"]:
                raise PanelError("请选择已开放记忆功能的平台实例。")
            if not valid_user_id(contract["type"], user):
                raise PanelError("用户 ID 格式无效，请按所选平台的提示填写。")
        for platform, user in selected:
            profile = plugin.context.get_config(f"{platform}:FriendMessage:{user}")
            if not local_runner(profile):
                raise PanelError("所选账号需要使用 AstrBot 内置 Agent，请先在配置文件中选择内置执行器。")
        url, key = self.credentials(payload)
        state = self.state()
        previous_mode = state.get("connection_mode")
        mode = payload.get("mode", previous_mode)
        if mode not in {None, "managed", "existing"}:
            raise PanelError("连接方式无效，请重新选择。")
        if not connection_fields(plugin.config)["service_id"] and self.has_identity_data():
            raise PanelError("已有记忆数据但记忆服务识别码缺失，请恢复原插件配置后重新保存。")
        try:
            client = HindsightClient(url, key)
        except (ValueError, httpx.InvalidURL):
            raise PanelError("服务地址格式无效，请填写完整的 HTTP 地址与有效 Key。") from None
        old_url = self.connection_url(plugin.config.get("api_url", ""))
        old_service = connection_fields(plugin.config)["service_id"]
        updates = {
            "api_url": client.url, "api_key": key,
            "control_plane_url": control_plane_url,
            "allowed_accounts": [
                {"__template_key": "account", "platform_instance_id": platform, "user_id": user}
                for platform, user in sorted(selected)
            ],
        }
        local_change = (
            not force_initialize and old_service and old_url == client.url and key == plugin.config.get("api_key")
            and mode == previous_mode
            and any(updates[name] != plugin.config.get(name, [] if name == "allowed_accounts" else "")
                    for name in ("allowed_accounts", "control_plane_url"))
        )
        try:
            if not local_change:
                if mode != "existing" and state.get("management_url") and self.managed_connection(
                    state, api_url=client.url, api_key=key,
                ):
                    status = await self.managed_request(state)
                    if not status["configured"] or not status["ready"]:
                        raise PanelError("请先选择模型并点击「保存模型」，服务启动成功后再保存连接。")
                async with asyncio.timeout(10):
                    await client.ensure_auth(force=True)
        except (HindsightError, TimeoutError):
            raise PanelError("记忆服务连接检查失败，请检查服务地址、Key 与服务运行状态。") from None
        finally:
            await client.close()
        self.ensure_open()
        if revision != self.config_revision():
            raise PanelError("连接检查期间配置已被更新，请刷新页面后重新保存。")
        previous = copy.deepcopy(dict(plugin.config))
        # Revoke before writing configuration; a failed save must keep the tombstone.
        if plugin.outbox:
            plugin.outbox.sync_permissions(selected & previous_accounts)
        if local_change:
            try:
                self.write_config(updates)
                if plugin.outbox:
                    plugin.refresh_permissions()
            except Exception:
                plugin.config.clear()
                plugin.config.update(previous)
                plugin.config.save_config()
                if plugin.outbox:
                    plugin.permissions = None
                    plugin.refresh_permissions()
                raise PanelError("设置保存失败，请检查插件数据目录后重试。") from None
            return self.settings_saved()
        self.connection_ready(client.url, key)
        connections = state.setdefault("connections", {})
        if not isinstance(connections, dict) or any(
            not isinstance(address, str) or not isinstance(service, str) or not service
            for address, service in connections.items()
        ):
            raise PanelError("服务连接记录损坏，请从备份恢复 setup.json。")
        if old_url and old_service:
            connections[old_url] = old_service
        service = connections.get(client.url)
        if not service or (client.url != old_url and service == old_service):
            service = "memory-" + secrets.token_hex(6)
        connections[client.url] = service
        if mode is not None:
            state["connection_mode"] = mode
        # Persist both identities before changing the active connection so a
        # process interruption cannot lose the route back to the old queue.
        self.store_state(state)
        if job is not None:
            self.persist_connection_job(job, expected_revision=self.config_revision())
        updates["service_id"] = service
        plugin.stopping = True
        write_started = False
        applied_config = None
        try:
            await plugin._close_resources()
            self.ensure_open()
            if job is not None:
                self.check_connection_job(job)
            write_started = True
            self.write_config(updates)
            if job is not None:
                self.persist_connection_job(job, expected_revision=self.config_revision())
            applied_config = copy.deepcopy(dict(plugin.config))
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            self.ensure_open()
            if job is not None:
                self.check_connection_job(job)
            if not plugin.outbox:
                raise PanelError("记忆服务初始化失败，请检查服务配置与本地数据目录。")
        except Exception:
            configuration_changed = job is not None and (not write_started or applied_config is not None) and (
                job["expected_revision"] != self.config_revision()
            )
            await plugin._close_resources()
            if not configuration_changed:
                plugin.config.clear()
                plugin.config.update(previous)
                plugin.config.save_config()
            elif applied_config is not None:
                # Roll back this save without overwriting independent edits.
                for key in previous.keys() | applied_config.keys():
                    if (key in plugin.config) == (key in applied_config) and plugin.config.get(key) == applied_config.get(key):
                        if key in previous:
                            plugin.config[key] = previous[key]
                        else:
                            plugin.config.pop(key, None)
                plugin.config.save_config()
            restored = self.state()
            if restored.get("connection_mode") == mode:
                if previous_mode is None:
                    restored.pop("connection_mode", None)
                else:
                    restored["connection_mode"] = previous_mode
            self.store_state(restored)
            if job is not None and not configuration_changed:
                self.persist_connection_job(job, expected_revision=self.config_revision())
            plugin.permissions = None
            self.ensure_open()
            plugin.stopping = False
            await plugin.initialize()
            if configuration_changed:
                raise PanelError("接入期间配置已变化，请重新保存并连接。") from None
            raise PanelError("配置应用失败，已恢复原有服务配置，请检查服务与数据目录。") from None
        return self.settings_saved()

    def scope(self, scope_id):
        plugin = self.plugin
        if not plugin.outbox or plugin.stopping:
            raise PanelError("请先在插件设置中完成记忆服务连接。")
        if scope_id.startswith("pending-"):
            raise PanelError("请先连接所选机器人，再在用户与记忆中确认机器人身份。")
        plugin.refresh_permissions()
        scope = plugin.outbox.scope_owner(scope_id)
        if not scope or plugin.outbox.recovering:
            raise PanelError("这个用户的记忆正在恢复或已停用，请稍后刷新。")
        owner = plugin.outbox.control.owner(plugin.outbox.connection_id, scope["owner_id"])
        if not owner or owner["bank"] != scope["bank"] or owner["revision"] != scope["revision"]:
            raise PanelError("记忆范围已变化，请刷新后重试。")
        return scope

    async def background(self, scope_id):
        from astrbot.api.web import request

        reading = request.method == "GET"
        payload = dict(request.query) if reading else await self.body()
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            plugin = self.plugin
            await plugin.prepare_sources()
            scope = self.scope(scope_id)
            source = Scope(*json.loads(scope["identity_json"]))
            if not scope["allowed"] or await self.bot_id(source.platform_instance_id) != source.bot_id:
                raise PanelError("请连接机器人并在用户与记忆中确认身份；旧记忆库不能选择或读取知识页。")
            scope = self.scope(scope_id)
            guard = ReadGuard.freeze(plugin, scope, manual=True)
            revision = self.config_revision()
            page_ids = [page["page_id"] for page in guard.selection["pages"]]
            if not reading:
                if payload.get("config_revision") != revision:
                    raise QueueError("background_changed")
                identity_key = "page_ids" if "page_ids" in payload else "page_id"
                if (
                    type(payload.get("scope_revision")) is not int
                    or payload["scope_revision"] != scope["revision"]
                    or type(payload.get("revision")) is not int
                    or payload.get("revision") != guard.selection["revision"]
                    or type(payload.get("enabled")) is not bool
                    or set(payload) != {identity_key, "enabled", "revision", "scope_revision", "config_revision"}
                ):
                    raise QueueError("background_changed")
                if identity_key == "page_id":
                    if not isinstance(payload["page_id"], str):
                        raise QueueError("background_settings_invalid")
                    page_ids = [payload["page_id"]] if payload["page_id"] else []
                    if not payload["enabled"] and payload["page_id"] == guard.selection["page_id"]:
                        page_ids = [page["page_id"] for page in guard.selection["pages"]]
                else:
                    page_ids = payload["page_ids"]
                if (
                    not isinstance(page_ids, list) or len(page_ids) > MAX_BACKGROUND_PAGES
                    or any(not isinstance(value, str) or not value for value in page_ids)
                    or len(set(page_ids)) != len(page_ids)
                ):
                    raise QueueError("background_settings_invalid")
            saved_ids = [page["page_id"] for page in guard.selection["pages"]]
            closing = not reading and not payload["enabled"] and page_ids in ([], saved_ids)
            if not guard.valid(plugin, metadata=closing):
                raise QueueError("scope_not_available")
            if not reading and payload["enabled"] and not page_ids:
                raise QueueError("background_missing")

            def unchanged():
                if not guard.valid(plugin, metadata=closing) or self.config_revision() != revision:
                    raise QueueError("background_changed")

            async def recheck():
                await plugin.prepare_sources()
                unchanged()
                bot = await self.bot_id(source.platform_instance_id)
                unchanged()
                if bot != source.bot_id:
                    raise QueueError("background_changed")

            pages = []
            checks = []
            selected_refs = []
            limits = context_limits(plugin.config)
            if reading or (page_ids and not closing):
                await guard.client.ensure_auth()
                await recheck()
                pages = await guard.client.knowledge_pages(scope["bank"])
                await recheck()
            if closing:
                selected_refs = guard.selection["pages"] if page_ids else []
            elif not reading or guard.selection["enabled"]:
                by_id = {page["page_id"]: page for page in pages}
                saved_refs = {page["page_id"]: page for page in guard.selection["pages"]}
                slots = asyncio.Semaphore(4)

                mapped = []
                for page_id in page_ids:
                    selected = by_id.get(page_id)
                    if selected is None:
                        if not reading and page_id not in saved_refs:
                            raise QueueError("background_missing")
                        continue
                    if reading and {
                        "page_id": page_id, "mental_model_id": selected["mental_model_id"],
                    } not in guard.selection["pages"]:
                        raise QueueError("background_changed")
                    mapped.append(page_id)

                async def fetch(page_id, *, body):
                    try:
                        async with slots:
                            unchanged()
                            value = (
                                await guard.client.knowledge_page(scope["bank"], page_id) if body else
                                await guard.client.background_page(scope["bank"], by_id[page_id]["mental_model_id"])
                            )
                            unchanged()
                            return value
                    except HindsightError as exc:
                        if (
                            exc.authentication or exc.code == "bank_missing"
                            or (not reading and (
                                saved_refs.get(page_id, {}).get("mental_model_id") != by_id[page_id]["mental_model_id"]
                                or exc.code != "background_missing"
                            ))
                        ):
                            raise
                        return exc

                async def batch(identities, *, body):
                    if not identities:
                        return {}
                    tasks = [asyncio.create_task(fetch(page_id, body=body)) for page_id in identities]
                    try:
                        values = await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    # Refresh native identity once between whole page/model phases.
                    await recheck()
                    return dict(zip(identities, values))

                bodies = await batch(mapped, body=True) if not reading else {}
                models = await batch([page_id for page_id in mapped
                                      if not isinstance(bodies.get(page_id), HindsightError)], body=False)
                for page_id in page_ids:
                    if page_id not in by_id:
                        checks.append({"page_id": page_id, "code": "background_missing"})
                        continue
                    page, model = bodies.get(page_id), models.get(page_id)
                    error = next((value for value in (page, model) if isinstance(value, HindsightError)), None)
                    if error:
                        checks.append({"page_id": page_id, "code": error.code})
                        continue
                    if page is not None and page["body"] != model["content"]:
                        raise QueueError("background_changed")
                    checks.append({"page_id": page_id, **check_page(model, tokenizer=guard.client.tokenizer, **{
                        k: limits[k] for k in ("tokens", "chars", "page_tokens")
                    })})
                selected_refs = [
                    {k: by_id[page_id][k] for k in ("page_id", "mental_model_id")}
                    if page_id in by_id else saved_refs[page_id]
                    for page_id in page_ids
                ]
            for check in checks:
                if "tokens" not in check:
                    check["message"] = self.error_message(check["code"])
                    continue
                check["message"] = (
                    f"含说明与元数据：{check['tokens']} token、{check['chars']} 字符"
                    f"（知识页上限 {limits['page_tokens']} token，共享 {limits['tokens']} token / {limits['chars']} 字符）。"
                    + ("超出：" + "、".join(check["exceeded"]) + "。" if check.get("exceeded") else "")
                    + (self.error_message(check["code"]) if check["code"] else "长度检查通过；聊天时仍与检索结果共用总预算。")
                )
            await recheck()
            if not reading:
                selection = save_reference(
                    guard.queue, scope, revision=payload["revision"], pages=selected_refs,
                    enabled=payload["enabled"],
                )
                return {"selection": selection, "check": checks[0] if checks else None,
                        "checks": checks, "message": "知识页选择与顺序已保存。"}
            return {
                "pages": [{k: page[k] for k in ("page_id", "path", "index_stale")} for page in pages],
                "selection": guard.selection, "scope_revision": scope["revision"],
                "config_revision": revision,
                "check": checks[0] if checks else None, "checks": checks, "budget": limits,
                "max_pages": MAX_BACKGROUND_PAGES,
            }

    async def action(self, scope_id):
        from astrbot.api.web import request

        reading = request.method == "GET"
        payload = dict(request.query) if reading else await self.body()
        action = payload.get("action")
        if reading and action != "maintenance_status":
            raise PanelError("GET 仅支持查询暂停进度。")
        async with self.lock, self.plugin.using_resources():
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            scope = self.scope(scope_id)
            plugin, queue = self.plugin, self.plugin.outbox
            if action in {"forget_prepare", "forget_confirm"}:
                self.ensure_no_upgrade()
            maintenance = plugin.worker.maintenance
            if (action == "maintenance_resume" and self.managed_connection()
                    and (check := self.upgrade_state().get("upgrade_check"))
                    and check.get("connection_id") == queue.connection_id and not check.get("verified")):
                raise PanelError("请先在插件设置中核对升级结果，确认原记忆库仍在，再解除读写暂停。")
            management = queue.management(scope_id)
            if action == "bank_check":
                self.check_revision(payload)
                client = plugin.client
                async with asyncio.timeout(10):
                    await client.ensure_auth()
                    exists = await client.bank_exists(scope["bank"])
                latest = self.scope(scope_id)
                if plugin.client is not client or plugin.outbox is not queue or any(
                    latest[key] != scope[key] for key in ("bank", "owner_id", "generation", "revision")
                ):
                    raise PanelError("记忆范围已变化，请重新获取用户状态。")
                self.check_revision(payload)
                if not exists:
                    raise PanelError("原记忆库仍不存在。请从匹配的备份恢复原库，或清空旧记忆后重新开始。")
                queue.control.set_bank_state(queue.connection_id, scope["bank"], "established")
                queue.apply_control()
                plugin.worker.wake.set()
                return {"message": "原记忆库已找到。"}
            if action in {"mode", "retry"}:
                await plugin.prepare_sources()
                scope = self.scope(scope_id)
                if not scope["allowed"]:
                    raise PanelError("旧记忆库不能调整模式或重试保存；请在用户与记忆中确认机器人身份后使用记忆。")
                source = Scope(*json.loads(scope["identity_json"]))
                if await self.bot_id(source.platform_instance_id) != source.bot_id:
                    raise PanelError("无法确认机器人身份，请连接机器人后在用户与记忆中确认身份。")
                latest = self.scope(scope_id)
                if not latest["allowed"] or latest["revision"] != scope["revision"]:
                    raise PanelError("记忆范围已变化，请刷新后重试。")
            if action == "maintenance_begin":
                self.check_revision(payload)
                discard = payload.get("discard_pending", False)
                if not isinstance(discard, bool):
                    raise PanelError("discard_pending 须为布尔值。")
                setup = self.state()
                pauses = setup.get("upgrade_pauses")
                if pauses and pauses.get("connection_id") == queue.connection_id:
                    pauses["banks"] = [row for row in pauses["banks"] if row["bank"] != scope["bank"]]
                    self.store_state(setup)
                    self.version_cache = None
                state = maintenance.begin(scope_id, discard_pending=discard, management=management)
                plugin.worker.wake.set()
                return {
                    "maintenance": state,
                    "config_revision": self.config_revision(),
                    "message": "新记录和记忆查询已暂停。请等到显示「可以修改记忆」后再去官方面板操作。普通聊天继续，暂停期间的新聊天不会保存。",
                }
            if action == "maintenance_status":
                state = await maintenance.status(scope_id, management=management)
                plugin.worker.wake.set()
                return {"maintenance": state}
            if action == "maintenance_resume":
                await maintenance.resume(scope_id, management=management)
                plugin.worker.wake.set()
                state = await maintenance.status(scope_id, management=queue.management(scope_id))
                return {"maintenance": state, "message": "读写暂停已解除，记忆模式保持原值。"}
            if action == "mode":
                mode = payload.get("mode")
                if mode not in {"active", "readonly", "paused"}:
                    raise PanelError("请选择「读写记忆」「只读记忆」或「停用记忆」。")
                if mode == "active":
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先查看暂停状态并解除读写暂停；如果记忆库已被删除，请恢复原库或清空旧记忆后重新开始。")
                    if not await maintenance.check(force=True, owner_id=scope["owner_id"]):
                        raise PanelError("旧记忆仍在清理，请等清理完成后再开启记忆。")
                    latest = self.scope(scope_id)
                    if latest["bank"] != scope["bank"] or latest["revision"] != scope["revision"]:
                        raise PanelError("记忆范围已变化，请刷新后重试。")
                    if await self.bot_id(source.platform_instance_id) != source.bot_id or not self.scope(scope_id)["allowed"]:
                        raise PanelError("机器人身份或授权已变化，请刷新后重试。")
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先查看暂停状态并解除读写暂停；如果记忆库已被删除，请恢复原库或清空旧记忆后重新开始。")
                queue.set_mode(scope_id, mode)
                message = "记忆模式已更新。"
            elif action == "retry":
                count = queue.request_retry(scope_id)
                message = f"已安排重新保存 {count} 条失败记录。"
            elif action == "forget_prepare":
                if not request.username:
                    raise PanelError("请使用 AstrBot 管理账号登录。")
                token = secrets.token_hex(6)
                plugin.confirmations["panel:" + scope["owner_id"]] = {
                    "token": token, "source": scope_id, "bank": scope["bank"],
                    "revision": scope["revision"], "username": request.username,
                    "generation": scope["generation"], "connection": queue.connection_id,
                    "until": time.monotonic() + 120,
                }
                return {
                    "confirmation": token,
                    "message": "清空后记忆功能保持暂停。等清理完成，再切换为「读写记忆」；"
                    "下次保存聊天时会自动创建新的记忆库。",
                }
            elif action == "forget_confirm":
                confirmation = str(payload.get("confirmation", ""))
                old = plugin.confirmations.pop("panel:" + scope["owner_id"], None)
                if (
                    not old or old["until"] < time.monotonic()
                    or old["username"] != request.username
                    or old["source"] != scope_id or old["bank"] != scope["bank"]
                    or old["revision"] != scope["revision"]
                    or old["generation"] != scope["generation"] or old["connection"] != queue.connection_id
                    or not secrets.compare_digest(old["token"], confirmation)
                ):
                    raise PanelError("确认已失效，请重新点击「清空记忆」。")
                queue.forget(scope_id, management=management)
                message = "正在清空旧记忆，记忆功能已暂停。清理完成后，可切换为「读写记忆」重新开始。"
            else:
                raise PanelError("操作无效。")
            plugin.worker.wake.set()
            return {"message": message}
