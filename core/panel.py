"""AstrBot plugin-view connection, account and maintenance management."""

import asyncio
import copy
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
from .background import ReadGuard, reference, save_reference
from .diagnostics import (
    ConfigurationError, configuration_message, connection_fields, missing_connection,
    recall_timeout,
)
from .identity import Incoming, Scope, sha256
from .installer import HINDSIGHT_VERSION, SUPPORTED_VERSIONS, installation, uninstallation
from .outbox import QueueError
from .permissions import pairs
from .source import local_runner, platform_details, prepare_bot_identity, valid_user_id

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
            ("accounts/authorize", "POST", self.authorize),
            ("install/prepare", "POST", self.prepare),
            ("service/status", "GET", self.service_status),
            ("service/upgrade", "POST", self.upgrade),
            ("models/save", "POST", self.save_models),
            ("models/status", "GET", self.model_status),
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
            "remote_failed": "记忆整理失败。请检查模型配置和服务日志，修复后点击「重试失败记录」。",
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

    def config_revision(self):
        """Opaque fingerprint of the plugin configuration and setup metadata."""
        return sha256(json.dumps(
            {"config": dict(self.plugin.config), "setup": self.state()},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))

    def check_revision(self, payload):
        self.ensure_open()
        if "config_revision" in payload and payload["config_revision"] != self.config_revision():
            raise PanelError("配置已被更新，请刷新页面后重新保存。")

    def ensure_no_upgrade(self):
        state, queue = self.state(), self.plugin.outbox
        check, pauses = state.get("upgrade_check"), state.get("upgrade_pauses")
        if queue and (
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
        plugin, state = self.plugin, self.state()
        client = plugin.client
        result = {"version": None, "target": HINDSIGHT_VERSION, "upgrade_available": False}
        if (not client or not plugin.outbox or plugin.stopping
                or not isinstance(getattr(client, "api_key", None), str)
                or not callable(getattr(client, "request", None))):
            return result
        identity = (client.url, sha256(client.api_key), self.config_revision())
        if not force and self.version_cache and self.version_cache[0] == identity and self.version_cache[1] > time.monotonic():
            return self.version_cache[2]
        try:
            async with plugin.using_resources(), asyncio.timeout(3):
                await client.ensure_auth()
                response = await client.request("GET", "/v1/version")
            version = response.get("api_version") if isinstance(response, dict) else None
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise PanelError("服务版本响应无效，请检查 Hindsight 服务。")
            result.update(version=version, supported=version in SUPPORTED_VERSIONS,
                          upgrade_available=version == "1.1.0")
        except (HindsightError, TimeoutError, PanelError):
            result["message"] = "暂时无法核对服务版本；升级前请恢复连接并重新检查。"
        if plugin.client is not client or plugin.stopping:
            return {"version": None, "target": HINDSIGHT_VERSION, "upgrade_available": False}
        result["managed"] = bool(state.get("id")) and self.connection_url(state.get("api_url", "")) == client.url
        result["pending"] = bool(state.get("upgrade_check") and not state["upgrade_check"].get("verified"))
        pauses = state.get("upgrade_pauses")
        result["upgrade_paused"] = bool(pauses and pauses.get("connection_id") == plugin.outbox.connection_id)
        result["resume_available"] = bool(result["upgrade_paused"] and state.get("upgrade_check", {}).get("verified"))
        self.version_cache = (identity, time.monotonic() + 30, result)
        return result

    async def upgrade(self):
        from astrbot.api.web import request

        payload = await self.body()
        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(60):
            self.check_revision(payload)
            plugin, state = self.plugin, self.state()
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
                        raise PanelError("仍有记录未完成保存或记忆库缺失；请在「记忆与保存」处理待保存或失败记录，并检查暂停进度。")
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
                message = ("版本与原记忆库核对通过。确认旧记忆可用后，点击「解除升级造成的暂停」。"
                           if state.get("upgrade_pauses") else
                           "版本与原记忆库核对通过。旧版安装记录未区分暂停来源，请在用户记忆中逐用户恢复原记忆模式。")
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
            current = bool(
                scope["allowed"] and bot_ids.get(source.platform_instance_id) == source.bot_id
            )
            scope["historical"] = not current
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
                "bot_id": Scope(*json.loads(scope["identity_json"])).bot_id if scope.get("identity_json") else "",
                "summary": summary,
            })
            if queue and not scope.get("pending"):
                try:
                    selection = reference(queue, scope)
                    result[-1]["background"] = {
                        **selection, "scope_revision": scope["revision"],
                    }
                    state = getattr(plugin, "background_outcomes", {}).get(
                        (queue.connection_id, scope["owner_id"]), {},
                    )
                    if (
                        state.get("bank") == scope["bank"] and state.get("revision") == scope["revision"]
                        and state.get("selection_revision") == selection["revision"]
                        and not summary.get("maintenance") and not scope.get("historical")
                    ):
                        result[-1]["background"]["status"] = {
                            "used": state["used"], "checked_at": state["checked_at"],
                            "message": self.error_message(state["code"]) if state["code"] else "",
                        }
                except QueueError as exc:
                    result[-1]["background"] = {"error": self.error_message(str(exc))}
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
                raise PanelError("请先保存这个用户的设置，再确认授权。")
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
        key_command = ""
        if re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", ""))):
            key_command = (
                "sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' "
                '"${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/'
                f'astrbot-memory-{state["id"]}/.env"'
                r" | tr -d '\047\042'"
            )
        managed_status = None
        if state.get("management_url") and state.get("management_key"):
            try:
                managed_status = await self.managed_request(state)
            except PanelError:
                managed_status = {"installed": False, "configured": False, "ready": False}
        missing = missing_connection(config)
        configured = not missing
        return {
            "config_revision": self.config_revision(),
            "config": {
                "configured": configured,
                "missing_fields": missing,
                "api_url": config.get("api_url", "") or state.get("api_url", ""),
                "control_plane_url": self.displayed_control_plane_url(
                    config.get("control_plane_url")
                    or (state.get("control_plane_url", "") if not configured else "")
                ),
                "has_api_key": bool(config.get("api_key") or state.get("api_key")),
                "allowed_accounts": config.get("allowed_accounts", []),
                "managed": bool(state.get("id")) and (
                    not config.get("api_url")
                    or self.connection_url(config["api_url"])
                    == self.connection_url(state.get("api_url", ""))
                ),
                **{k: state.get(k, "") for k in (
                    "chat_provider_id", "embedding_provider_id", "rerank_provider_id"
                )},
            },
            "platforms": self.platforms(),
            "managed_service": managed_status,
            "backend": await self.service_status(force=False),
            "model_job": self.model_job,
            "has_installation": bool(state.get("id")),
            "installation_api_url": state.get("api_url", ""),
            "control_plane_key_command": key_command,
            "providers": self.providers().options(),
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
        if state.get("management_url") and self.connection_url(url) == self.connection_url(state.get("api_url", "")):
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
                return {
                    "installed": True, "configured": status.get("configured") is True,
                    "ready": status.get("ready") is True,
                    "embedding_fingerprint": status.get("embedding_fingerprint", ""),
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
            status = await self.service_status()
            if status.get("managed") and status.get("upgrade_available"):
                raise PanelError("当前服务为 1.1.0，请使用「升级 Hindsight」暂停保存和查询、备份并升级。")
            if status.get("managed") and status.get("version") is None and payload.get("repair") is not True:
                raise PanelError("无法确认已有服务版本。请恢复连接；若选择修复，命令也只会替换实际确认的 1.2.0 服务，不会升级未知版本。")
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
            }

    async def save_models(self):
        payload = await self.body()
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
        return dict(self.model_job) if self.model_job else {"status": "idle"}

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
            state = self.state()
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
            if payload.get("delete_memory_data") is not True:
                raise PanelError("请确认删除这套配套服务及全部记忆数据。")
            return uninstallation(self.state())

    async def save(self):
        payload = await self.body()
        async with self.lock:
            self.ensure_no_upgrade()
            self.check_revision(payload)
            revision = self.config_revision()
            return await self._save(payload, revision)

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

    async def _save(self, payload, revision):
        self.ensure_open()
        plugin = self.plugin
        try:
            recall_timeout(plugin.config)
        except ConfigurationError as exc:
            raise PanelError(self.error_message(str(exc))) from None
        control_plane_url = self.control_plane_url(
            payload.get("control_plane_url", plugin.config.get("control_plane_url", "")),
            api_key=payload.get("api_key", ""),
        )
        accounts = payload.get("allowed_accounts", [])
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
            old_service and old_url == client.url and key == plugin.config.get("api_key")
            and any(updates[name] != plugin.config.get(name, [] if name == "allowed_accounts" else "")
                    for name in ("allowed_accounts", "control_plane_url"))
        )
        try:
            if not local_change:
                if state.get("management_url") and client.url == self.connection_url(state.get("api_url", "")):
                    status = await self.managed_request(state)
                    if not status["configured"] or not status["ready"]:
                        raise PanelError("请先选择模型并点击「验证并保存模型」，服务启动成功后再保存连接和使用者名单。")
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
        # Persist both identities before changing the active connection so a
        # process interruption cannot lose the route back to the old queue.
        self.store_state(state)
        updates["service_id"] = service
        plugin.stopping = True
        try:
            await plugin._close_resources()
            self.ensure_open()
            self.write_config(updates)
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            self.ensure_open()
            if not plugin.outbox:
                raise PanelError("记忆服务初始化失败，请检查服务配置与本地数据目录。")
        except Exception:
            await plugin._close_resources()
            plugin.config.clear()
            plugin.config.update(previous)
            plugin.config.save_config()
            plugin.permissions = None
            self.ensure_open()
            plugin.stopping = False
            await plugin.initialize()
            raise PanelError("配置应用失败，已恢复原有服务配置，请检查服务与数据目录。") from None
        return self.settings_saved()

    def scope(self, scope_id):
        plugin = self.plugin
        if not plugin.outbox or plugin.stopping:
            raise PanelError("请先在设置中完成记忆服务配置。")
        if scope_id.startswith("pending-"):
            raise PanelError("请先连接所选机器人，并在设置中确认账号授权。")
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
                raise PanelError("请连接当前机器人并确认账号授权；历史范围不能选择或读取知识页。")
            scope = self.scope(scope_id)
            guard = ReadGuard.freeze(plugin, scope, manual=True)
            revision = self.config_revision()
            if not reading:
                if payload.get("config_revision") != revision:
                    raise QueueError("background_changed")
                if (
                    type(payload.get("scope_revision")) is not int
                    or payload["scope_revision"] != scope["revision"]
                    or payload.get("revision") != guard.selection["revision"]
                    or type(payload.get("enabled")) is not bool
                    or not isinstance(payload.get("page_id"), str)
                    or set(payload) != {"page_id", "enabled", "revision", "scope_revision", "config_revision"}
                ):
                    raise QueueError("background_changed")
            closing = not reading and not payload["enabled"] and payload["page_id"] in {
                "", guard.selection["page_id"],
            }
            if not guard.valid(plugin, metadata=closing):
                raise QueueError("scope_not_available")
            page_id = "" if reading else payload["page_id"]
            if not reading and payload["enabled"] and not page_id:
                raise QueueError("background_missing")

            async def recheck():
                await plugin.prepare_sources()
                if not guard.valid(plugin, metadata=closing) or self.config_revision() != revision:
                    raise QueueError("background_changed")
                if (
                    await self.bot_id(source.platform_instance_id) != source.bot_id
                    or not guard.valid(plugin, metadata=closing) or self.config_revision() != revision
                ):
                    raise QueueError("background_changed")

            pages = []
            model_id = ""
            if reading or (page_id and not closing):
                await guard.client.ensure_auth()
                await recheck()
                pages = await guard.client.knowledge_pages(scope["bank"])
                await recheck()
            if page_id and closing:
                model_id = guard.selection["mental_model_id"]
            elif page_id:
                selected = next((page for page in pages if page["page_id"] == page_id), None)
                if not selected:
                    raise QueueError("background_missing")
                model_id = selected["mental_model_id"]
                page = await guard.client.knowledge_page(scope["bank"], page_id)
                await recheck()
                model = await guard.client.background_page(scope["bank"], model_id)
                if page["body"] != model["content"]:
                    raise QueueError("background_changed")
            await recheck()
            if not reading:
                selection = save_reference(
                    guard.queue, scope, revision=payload["revision"], page_id=page_id,
                    model_id=model_id, enabled=payload["enabled"],
                )
                return {"selection": selection, "message": "知识页选择和聊天使用开关已保存。"}
            return {
                "pages": [{k: page[k] for k in ("page_id", "path", "index_stale")} for page in pages],
                "selection": guard.selection, "scope_revision": scope["revision"],
                "config_revision": revision,
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
            if (action == "maintenance_resume" and (check := self.state().get("upgrade_check"))
                    and check.get("connection_id") == queue.connection_id and not check.get("verified")):
                raise PanelError("请先在设置中核对升级结果，确认原记忆库仍在，再恢复原记忆模式。")
            management = queue.management(scope_id)
            if action in {"mode", "retry"}:
                await plugin.prepare_sources()
                scope = self.scope(scope_id)
                if not scope["allowed"]:
                    raise PanelError("历史范围只能暂停保存和查询或清空；请在设置中确认当前机器人身份后使用记忆。")
                source = Scope(*json.loads(scope["identity_json"]))
                if await self.bot_id(source.platform_instance_id) != source.bot_id:
                    raise PanelError("无法确认当前机器人身份，请连接机器人并在设置中确认授权。")
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
                return {"maintenance": state, "message": "已恢复原记忆模式。原模式为暂停或只读时，不会自动开启保存。"}
            if action == "mode":
                mode = payload.get("mode")
                if mode not in {"active", "readonly", "paused"}:
                    raise PanelError("请选择「保存并自动使用记忆」「只自动使用已有记忆」或「暂停保存和自动使用」。")
                if mode == "active":
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先恢复原记忆模式；如果记忆库已被删除，请恢复原记忆库或清空旧记忆后重新开始。")
                    if not await maintenance.check(force=True, owner_id=scope["owner_id"]):
                        raise PanelError("旧记忆仍在清理，请等清理完成后再开启记忆。")
                    latest = self.scope(scope_id)
                    if latest["bank"] != scope["bank"] or latest["revision"] != scope["revision"]:
                        raise PanelError("记忆范围已变化，请刷新后重试。")
                    if await self.bot_id(source.platform_instance_id) != source.bot_id or not self.scope(scope_id)["allowed"]:
                        raise PanelError("机器人身份或授权已变化，请刷新后重试。")
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先恢复原记忆模式；如果记忆库已被删除，请恢复原记忆库或清空旧记忆后重新开始。")
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
                    "message": "清空后记忆功能保持暂停。等清理完成，再切换为「保存并自动使用记忆」；"
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
                    raise PanelError("确认已失效，请重新点击「清空此用户的记忆」。")
                queue.forget(scope_id, management=management)
                message = "正在清空旧记忆，记忆功能已暂停。清理完成后，可切换为「保存并自动使用记忆」重新开始。"
            else:
                raise PanelError("操作无效。")
            plugin.worker.wake.set()
            return {"message": message}
