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
from .identity import Incoming, sha256
from .installer import installation, uninstallation
from .outbox import QueueError
from .permissions import pairs
from .source import local_runner

NAME = "astrbot_plugin_hindsight_bridge"


class PanelError(ValueError):
    pass


class MemoryPanel:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        self.model_task = None
        self.model_job = None
        self.model_selection = None
        routes = [
            ("bootstrap", "GET", self.bootstrap),
            ("connection/check", "POST", self.check),
            ("settings/save", "POST", self.save),
            ("install/prepare", "POST", self.prepare),
            ("models/save", "POST", self.save_models),
            ("models/status", "GET", self.model_status),
            ("uninstall/prepare", "POST", self.prepare_uninstall),
            ("scopes/<scope_id>/action", ["GET", "POST"], self.action),
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

            try:
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
        return invoke

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
            {"id": str(row["id"]), "name": str(row.get("name") or row["id"])}
            for row in self.plugin.context.get_config().get("platform", [])
            if row.get("type") == "aiocqhttp" and row.get("enable", True)
        ]

    def directory(self):
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(NAME))

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
        if "config_revision" in payload and payload["config_revision"] != self.config_revision():
            raise PanelError("配置已被更新，请刷新页面后重新保存。")

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

    async def user_scopes(self):
        plugin, scopes = self.plugin, []
        accounts = pairs(plugin.config.get("allowed_accounts", []))
        queue = plugin.outbox if not plugin.stopping else None
        if queue:
            plugin.refresh_permissions()
            scopes = [dict(row) for row in queue.list_scopes() if row["allowed"]]
        known = {(row["platform_instance_id"], row["user_id"]) for row in scopes}
        bot_ids = {}
        for platform, user in sorted(accounts - known):
            if queue and not queue.recovering:
                if platform not in bot_ids:
                    bot_ids[platform] = await self.bot_id(platform)
                if bot_ids[platform] and plugin.outbox is queue and not plugin.stopping:
                    scopes.append(dict(queue.scope(Incoming(
                        "aiocqhttp", platform, bot_ids[platform], user, "", int(time.time()), "",
                    ))))
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
                scopes[-1]["pending_reason"] = "platform"
        result = []
        for scope in scopes:
            summary = queue.summary(scope["scope_id"]) if queue and not scope.get("pending") else {
                "counts": {}, "last_error": "",
            }
            summary["error_message"] = self.error_message(summary["last_error"]) if summary["last_error"] else ""
            result.append({key: scope[key] for key in (
                "scope_id", "platform_instance_id", "user_id", "mode", "bank",
            )} | {
                "pending": scope.get("pending", False),
                "pending_reason": scope.get("pending_reason", ""),
                "summary": summary,
            })
        return result

    async def bot_id(self, platform):
        try:
            get_platform = getattr(self.plugin.context, "get_platform_inst", None)
            adapter = get_platform(platform) if get_platform else None
            if adapter is None:
                return ""
            async with self.plugin.using_resources(), asyncio.timeout(1):
                login = await adapter.get_client().get_login_info()
            identity = str(login.get("user_id", ""))
            return identity if identity.isdecimal() and int(identity) > 0 else ""
        except Exception:  # An offline QQ adapter must not hide configured users.
            return ""

    async def _bootstrap(self):
        plugin, config = self.plugin, self.plugin.config
        connected, connection_message = None, ""
        if plugin.client and plugin.outbox and not plugin.stopping:
            try:
                async with plugin.using_resources(), asyncio.timeout(3):
                    await plugin.client.ensure_auth()
                connected = True
            except (HindsightError, TimeoutError) as exc:
                connected = False
                connection_message = self.error_message(plugin.error_code(exc))
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
        configured = bool(config.get("api_url") and config.get("api_key") and config.get("service_id"))
        return {
            "config_revision": self.config_revision(),
            "config": {
                "configured": configured,
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
            "model_job": self.model_job,
            "has_installation": bool(state.get("id")),
            "installation_api_url": state.get("api_url", ""),
            "control_plane_key_command": key_command,
            "providers": self.providers().options(),
            "status": {
                "label": connection_message or plugin.status,
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
        return {"ok": True, "ready": True, "message": "记忆服务连接正常。"}

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
        if self.model_task and not self.model_task.done():
            self.model_task.cancel()
            await asyncio.gather(self.model_task, return_exceptions=True)

    async def apply_models(self, payload):
        async with self.lock:
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
                result = await self.managed_request(state, environment)
                if not result["ready"]:
                    raise PanelError("模型配置已接收，服务尚未就绪，请稍后重试。")
                state.update(selection)
                self.store_state(state)
            finally:
                if active:
                    plugin.permissions = None
                    # Plugin unload cancels this job and must not reopen resources.
                    if not asyncio.current_task().cancelling():
                        plugin.stopping = False
                        await plugin.initialize()
            return {"saved": True, "config_revision": self.config_revision(), "message": "模型配置已保存并生效。"}

    async def prepare_uninstall(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            if payload.get("delete_memory_data") is not True:
                raise PanelError("请确认删除这套配套服务及全部记忆数据。")
            return uninstallation(self.state())

    async def save(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            revision = self.config_revision()
            return await self._save(payload, revision)

    async def _save(self, payload, revision):
        control_plane_url = self.control_plane_url(
            payload.get("control_plane_url", self.plugin.config.get("control_plane_url", "")),
            api_key=payload.get("api_key", ""),
        )
        accounts = payload.get("allowed_accounts", [])
        selected = pairs(accounts)
        valid_platforms = {row["id"] for row in self.platforms()}
        if any(platform not in valid_platforms for platform, _ in selected):
            raise PanelError("请选择 AstrBot 中已启用的 OneBot 接入实例。")
        if any(not user.isdecimal() or len(user) > 20 for _, user in selected):
            raise PanelError("用户账号请填写自己的 QQ 号。")
        url, key = self.credentials(payload)
        try:
            client = HindsightClient(url, key)
        except (ValueError, httpx.InvalidURL):
            raise PanelError("服务地址格式无效，请填写完整的 HTTP 地址与有效 Key。") from None
        state = self.state()
        try:
            if state.get("management_url") and client.url == self.connection_url(state.get("api_url", "")):
                status = await self.managed_request(state)
                if not status["configured"] or not status["ready"]:
                    raise PanelError("请先在设置中选择模型并保存，待服务就绪后完成设置。")
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
        except (HindsightError, TimeoutError):
            raise PanelError("记忆服务连接检查失败，请检查服务地址、Key 与服务运行状态。") from None
        finally:
            await client.close()
        context = self.plugin.context
        for platform, user in selected:
            profile = context.get_config(f"{platform}:FriendMessage:{user}")
            if not local_runner(profile):
                raise PanelError("所选账号需要使用 AstrBot 内置 Agent，请先在配置文件中选择内置执行器。")
        if revision != self.config_revision():
            raise PanelError("连接检查期间配置已被更新，请刷新页面后重新保存。")
        plugin = self.plugin
        previous = copy.deepcopy(dict(plugin.config))
        connections = state.setdefault("connections", {})
        if not isinstance(connections, dict) or any(
            not isinstance(address, str) or not isinstance(service, str) or not service
            for address, service in connections.items()
        ):
            raise PanelError("服务连接记录损坏，请从备份恢复 setup.json。")
        old_url = self.connection_url(plugin.config.get("api_url", ""))
        old_service = plugin.config.get("service_id")
        if old_url and old_service:
            connections[old_url] = old_service
        service = connections.get(client.url)
        if not service or (client.url != old_url and service == old_service):
            service = "memory-" + secrets.token_hex(6)
        connections[client.url] = service
        # Persist both identities before changing the active connection so a
        # process interruption cannot lose the route back to the old queue.
        self.store_state(state)
        updates = {
            "api_url": client.url, "api_key": key,
            "control_plane_url": control_plane_url,
            "service_id": service,
            "allowed_accounts": [
                {"platform_instance_id": platform, "user_id": user}
                for platform, user in sorted(selected)
            ],
        }
        plugin.stopping = True
        try:
            await plugin._close_resources()
            for obsolete in ("enabled", "permission_config_version", "allowed_platform_ids", "allowed_user_ids", "timezone"):
                plugin.config.pop(obsolete, None)
            plugin.config.update(updates)
            plugin.config.save_config()
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            if not plugin.outbox:
                raise PanelError("记忆服务初始化失败，请检查服务配置与本地数据目录。")
        except Exception:
            await plugin._close_resources()
            plugin.config.clear()
            plugin.config.update(previous)
            plugin.config.save_config()
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            raise PanelError("配置应用失败，已恢复原有服务配置，请检查服务与数据目录。") from None
        plugin.confirmations.clear()
        return {
            "saved": True, "config_revision": self.config_revision(),
            "message": "配置已保存并生效。正常聊天即可，无需再重载插件。",
        }

    def scope(self, scope_id):
        plugin = self.plugin
        if not plugin.outbox or plugin.stopping:
            raise PanelError("请先在设置中完成记忆服务配置。")
        if scope_id.startswith("pending-"):
            raise PanelError("请先连接所选 QQ 机器人，连接后会自动更新。")
        plugin.refresh_permissions()
        scope = plugin.outbox.scope_owner(scope_id)
        if not scope or not scope["allowed"] or plugin.outbox.recovering:
            raise PanelError("这个用户的记忆正在恢复或已停用，请稍后刷新。")
        owner = plugin.outbox.control.owner(plugin.outbox.connection_id, scope["owner_id"])
        if not owner or owner["bank"] != scope["bank"] or owner["revision"] != scope["revision"]:
            raise PanelError("记忆范围已变化，请刷新后重试。")
        return scope

    async def action(self, scope_id):
        from astrbot.api.web import request

        reading = request.method == "GET"
        payload = dict(request.query) if reading else await self.body()
        action = payload.get("action")
        if reading and action != "maintenance_status":
            raise PanelError("GET 仅支持查询维护状态。")
        async with self.lock, self.plugin.using_resources():
            scope = self.scope(scope_id)
            plugin, queue = self.plugin, self.plugin.outbox
            maintenance = plugin.worker.maintenance
            if action == "maintenance_begin":
                discard = payload.get("discard_pending", False)
                if not isinstance(discard, bool):
                    raise PanelError("discard_pending 须为布尔值。")
                state = maintenance.begin(scope_id, discard_pending=discard)
                plugin.worker.wake.set()
                return {
                    "maintenance": state,
                    "message": "维护已开始，新记录和记忆查询已暂停。请等到显示「可以修改记忆」后再去官方面板操作。",
                }
            if action == "maintenance_status":
                state = await maintenance.status(scope_id)
                plugin.worker.wake.set()
                return {"maintenance": state}
            if action == "maintenance_resume":
                await maintenance.resume(scope_id)
                plugin.worker.wake.set()
                state = await maintenance.status(scope_id)
                return {"maintenance": state, "message": "维护已结束，请确认记忆模式。"}
            if action == "mode":
                mode = payload.get("mode")
                if mode not in {"active", "readonly", "paused"}:
                    raise PanelError("请选择「正常记忆」「只用已有记忆」或「暂停记忆」。")
                if mode == "active":
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先结束维护；如果记忆库已被删除，请恢复原记忆库或清空旧记忆后重新开始。")
                    if not await maintenance.check(force=True, owner_id=scope["owner_id"]):
                        raise PanelError("旧记忆仍在清理，请等清理完成后再开启记忆。")
                    latest = self.scope(scope_id)
                    if latest["bank"] != scope["bank"] or latest["revision"] != scope["revision"]:
                        raise PanelError("记忆范围已变化，请刷新后重试。")
                    summary = queue.summary(scope_id)
                    if summary.get("maintenance") or summary.get("bank_missing"):
                        raise PanelError("请先结束维护；如果记忆库已被删除，请恢复原记忆库或清空旧记忆后重新开始。")
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
                    "until": time.monotonic() + 120,
                }
                return {
                    "confirmation": token,
                    "message": "清空后记忆功能保持暂停。等清理完成，再切换为「正常记忆」；"
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
                    or not secrets.compare_digest(old["token"], confirmation)
                ):
                    raise PanelError("确认已失效，请重新点击「清空此用户的记忆」。")
                queue.forget(scope_id)
                message = "正在清空旧记忆，记忆功能已暂停。清理完成后，可切换为「正常记忆」重新开始。"
            else:
                raise PanelError("操作无效。")
            plugin.worker.wake.set()
            return {"message": message}
