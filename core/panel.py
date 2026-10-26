"""AstrBot plugin-view setup and administrator memory management."""

import asyncio
import copy
from datetime import datetime, UTC
import json
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .catalog import export_bytes
from .client import HindsightClient, HindsightError
from .identity import Incoming, sha256
from .installer import installation
from .materials import recall_query
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
        routes = [
            ("bootstrap", "GET", self.bootstrap),
            ("connection/check", "POST", self.check),
            ("settings/save", "POST", self.save),
            ("install/prepare", "POST", self.prepare),
            ("scopes/<scope_id>/memories", "GET", self.memories),
            ("scopes/<scope_id>/detail", "GET", self.detail),
            ("scopes/<scope_id>/search", "POST", self.search),
            ("scopes/<scope_id>/export", "GET", self.export),
            ("scopes/<scope_id>/action", "POST", self.action),
        ]
        if hasattr(plugin.context, "register_web_api"):
            for route, method, handler in routes:
                plugin.context.register_web_api(
                    f"/{NAME}/{route}", self.endpoint(handler), [method], "Hindsight 记忆管理"
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
                    else self.plugin.diagnostic(self.plugin.error_code(exc))
                )
                return error_response(message, headers={"Cache-Control": "no-store"})
            except (OSError, sqlite3.DatabaseError):
                return error_response("无法保存数据，请检查 AstrBot 数据目录与磁盘空间。")
        return invoke

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
            ("id", 6), ("database_password", 32), ("api_key", 32), ("panel_key", 32)
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

    async def bootstrap(self):
        async with self.lock:
            return await self._bootstrap()

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
                connection_message = plugin.diagnostic(plugin.error_code(exc))
        scopes = []
        if plugin.outbox and not plugin.stopping:
            plugin.refresh_permissions()
            for scope in plugin.outbox.list_scopes():
                if scope["allowed"]:
                    scopes.append({
                        key: scope[key]
                        for key in ("scope_id", "platform_instance_id", "user_id", "mode", "bank")
                    } | {"summary": plugin.outbox.summary(scope["scope_id"])})
        state = self.state()
        return {
            "config_revision": self.config_revision(),
            "config": {
                "enabled": bool(config.get("enabled")),
                "api_url": config.get("api_url", "") or state.get("api_url", ""),
                "has_api_key": bool(config.get("api_key") or state.get("api_key")),
                "timezone": config.get("timezone", "UTC"),
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
        client = HindsightClient(url, key)
        try:
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
        finally:
            await client.close()
        return {"ok": True, "message": "记忆服务连接正常，API Key 有效。"}

    async def installation_marker(self, state, url, *, api_key=None):
        """Return the deployed marker, or None when it cannot be read."""
        identifier = state.get("id")
        key = state.get("api_key") if api_key is None else api_key
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{12}", identifier):
            return None
        if not isinstance(key, str) or not key:
            return None
        client = None
        try:
            client = HindsightClient(url, key)
            bank = "astrbot-setup-" + identifier
            offset = 0
            async with asyncio.timeout(5):
                while offset < 10000:
                    result = await client.request(
                        "GET", "/v1/default/banks?"
                        + urlencode({"q": bank, "limit": 100, "offset": offset}),
                    )
                    rows, total = result.get("banks"), result.get("total")
                    if (
                        not isinstance(rows, list) or len(rows) > 100
                        or type(total) is not int or total < offset + len(rows)
                    ):
                        raise HindsightError("invalid_bank_list")
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("bank_id"), str):
                            raise HindsightError("invalid_bank_list")
                        if row["bank_id"] == bank:
                            name = row.get("name")
                            return name if isinstance(name, str) else ""
                    offset += len(rows)
                    if offset >= total:
                        return None
                    if not rows:
                        raise HindsightError("invalid_bank_list")
            raise PanelError("安装标记查询超过一万条记录，请在服务端检查安装标记后重试。")
        except PanelError:
            raise
        except (HindsightError, TimeoutError, OSError, ValueError):
            return None
        finally:
            if client is not None:
                await client.close()

    async def prepare(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            payload = dict(payload)
            payload.pop("config_revision", None)
            environment = await self.providers().resolve_checked(payload)
            state = self.state(create=True)
            for key in ("chat_provider_id", "embedding_provider_id", "rerank_provider_id"):
                state[key] = str(payload.get(key, ""))
            result = installation(
                Path(__file__).resolve().parent.parent, state, environment,
                container_hostname=socket.gethostname() if Path("/.dockerenv").exists() else "",
            )
            fingerprint = result.get("embedding_fingerprint")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise PanelError("安装器未提供有效的嵌入配置标识，请更新插件后重试。")
            marker = await self.installation_marker(state, result["api_url"])
            if marker is not None and marker != "astrbot-setup-v1:" + fingerprint:
                raise PanelError("已部署服务的嵌入配置与所选模型不一致，请先迁移已有向量数据。")
            state.pop("embedding_identity", None)
            state["candidate_fingerprint"] = fingerprint
            state["api_url"] = result["api_url"]
            self.store_state(state)
            return {**result, "config_revision": self.config_revision()}

    async def save(self):
        payload = await self.body()
        async with self.lock:
            self.check_revision(payload)
            revision = self.config_revision()
            return await self._save(payload, revision)

    async def _save(self, payload, revision):
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PanelError("启用选项无效。")
        accounts = payload.get("allowed_accounts", [])
        selected = pairs(accounts)
        valid_platforms = {row["id"] for row in self.platforms()}
        if any(platform not in valid_platforms for platform, _ in selected):
            raise PanelError("请选择 AstrBot 中已启用的 OneBot 接入实例。")
        if enabled and not selected:
            raise PanelError("请至少添加一个允许使用记忆的 QQ 账号。")
        if any(not user.isdecimal() or len(user) > 20 for _, user in selected):
            raise PanelError("用户账号请填写自己的 QQ 号。")
        zone = str(payload.get("timezone", "UTC"))
        try:
            ZoneInfo(zone)
        except (ValueError, KeyError):
            raise PanelError("时区无效，例如中国大陆可填写 Asia/Shanghai。") from None
        url, key = self.credentials(payload)
        try:
            client = HindsightClient(url, key)
        except (ValueError, httpx.InvalidURL):
            raise PanelError("服务地址格式无效，请填写完整的 HTTP 地址与有效 Key。") from None
        state = self.state()
        try:
            if enabled:
                async with asyncio.timeout(10):
                    await client.ensure_auth(force=True)
                if state.get("id") and client.url == self.connection_url(state.get("api_url", "")):
                    candidate = state.get("candidate_fingerprint")
                    if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate):
                        raise PanelError("请先生成并执行安装命令，再连接并启用。")
                    marker = await self.installation_marker(state, client.url, api_key=key)
                    if marker != "astrbot-setup-v1:" + candidate:
                        raise PanelError("服务尚未完成所选配置的安装，请执行最新安装命令后重试。")
        except (HindsightError, TimeoutError):
            raise PanelError("记忆服务连接检查失败，请检查服务地址、Key 与服务运行状态。") from None
        finally:
            await client.close()
        context = self.plugin.context
        conversation_configs = []
        for platform, user in selected:
            profile = context.get_config(f"{platform}:FriendMessage:{user}")
            if not local_runner(profile):
                raise PanelError("所选账号需要使用 AstrBot 内置 Agent，请先在配置文件中选择内置执行器。")
            if all(profile is not previous for previous in conversation_configs):
                conversation_configs.append(profile)
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
            "enabled": enabled, "api_url": client.url, "api_key": key,
            "service_id": service,
            "timezone": zone, "allowed_accounts": [
                {"platform_instance_id": platform, "user_id": user}
                for platform, user in sorted(selected)
            ], "permission_config_version": 2,
        }
        plugin.stopping = True
        try:
            await plugin._close_resources()
            plugin.config.update(updates)
            plugin.config.save_config()
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            if enabled and not plugin.outbox:
                raise PanelError("记忆服务初始化失败，请检查服务配置与本地数据目录。")
            for profile in conversation_configs:
                settings = profile.setdefault("provider_settings", {})
                settings["datetime_system_prompt"] = False
                settings["identifier"] = False
                profile.save_config()
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
            raise PanelError("请先在首次设置中连接并启用记忆服务。")
        plugin.refresh_permissions()
        scope = plugin.outbox.scope_owner(scope_id)
        if not scope or not plugin.outbox.can_recall(scope, manual=True):
            raise PanelError("这个用户的记忆正在恢复或已停用，请稍后刷新。")
        return scope

    def unchanged(self, scope):
        if not self.plugin.outbox.can_recall(scope, manual=True):
            raise PanelError("记忆范围已变化，请刷新后重试。")

    async def memories(self, scope_id):
        from astrbot.api.web import request

        scope = self.scope(scope_id)
        page = request.query.get("page", 1, type=int)
        if not isinstance(page, int) or not 1 <= page <= 1000:
            raise PanelError("页码须在 1 到 1000 之间。")
        async with self.plugin.using_resources(), asyncio.timeout(10):
            data = await self.plugin.client.memory_page(scope["bank"], page=page)
            self.unchanged(scope)
            return {**data, "page": page, "corrections": self.plugin.outbox.corrections(scope)}

    async def detail(self, scope_id):
        from astrbot.api.web import request

        scope = self.scope(scope_id)
        async with self.plugin.using_resources(), asyncio.timeout(10):
            record = await self.plugin.client.get_memory(
                scope["bank"], request.query.get("memory_id", "")
            )
            self.unchanged(scope)
            return {"record": record, "corrections": [
                row for row in self.plugin.outbox.corrections(scope)
                if row["memory_id"] == record["id"]
            ]}

    async def search(self, scope_id):
        payload = await self.body()
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise PanelError("请输入 1 到 4000 字符的查询。")
        scope = self.scope(scope_id)
        async with self.plugin.using_resources(), asyncio.timeout(10):
            query = recall_query(query, budget=380, name=self.plugin.client.tokenizer)
            results = await self.plugin.client.recall(
                scope["bank"], query, timestamp=datetime.now(UTC).isoformat()
            )
            self.unchanged(scope)
            return {"results": results}

    async def export(self, scope_id):
        from astrbot.api.web import stream_response

        scope = self.scope(scope_id)
        async with self.plugin.using_resources(), asyncio.timeout(35):
            payload = await self.plugin.client.export_memories(scope["bank"])
            self.unchanged(scope)
            payload["corrections"] = self.plugin.outbox.corrections(scope)
            content = export_bytes(payload)
            if len(content) > 8 * 1024 * 1024:
                raise HindsightError("export_too_large")

        async def chunks():
            yield content

        return stream_response(chunks(), content_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="hindsight-memory.json"',
            "Cache-Control": "no-store",
        })

    async def action(self, scope_id):
        from astrbot.api.web import request

        payload = await self.body()
        async with self.lock, self.plugin.using_resources():
            scope = self.scope(scope_id)
            action = payload.get("action")
            plugin, queue = self.plugin, self.plugin.outbox
            if action == "mode":
                mode = payload.get("mode")
                if mode not in {"active", "readonly", "paused"}:
                    raise PanelError("请选择开启、只读或暂停。")
                if mode == "active":
                    if not await plugin.worker.maintenance.check(
                        force=True, owner_id=scope["owner_id"]
                    ):
                        raise PanelError("旧范围仍在清理，请稍后开启。")
                    self.unchanged(scope)
                queue.set_mode(scope_id, mode)
            elif action == "retry":
                count = queue.request_retry(scope_id)
                plugin.worker.wake.set()
                return {"message": f"已登记 {count} 条失败任务重试。"}
            elif action == "correct":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                    raise PanelError("更正内容需要 1 到 1000 字符。")
                if scope["mode"] != "active":
                    raise PanelError("请先开启记忆，再提交更正。")
                administrator = request.username
                if not administrator:
                    raise PanelError("请使用 AstrBot 管理账号登录后提交更正。")
                async with asyncio.timeout(10):
                    record = await plugin.client.get_memory(scope["bank"], payload.get("memory_id", ""))
                self.unchanged(scope)
                identity = json.loads(scope["identity_json"])
                incoming = Incoming(
                    *identity[1:5], "panel-correction-" + secrets.token_hex(16),
                    int(time.time()), text,
                )
                captured = queue.capture(incoming, "management-corrections", correction={
                    "id": record["id"], "text": record["text"],
                    "document_id": record.get("document_id") or "",
                    "source": "administrator", "submitted_by": administrator,
                })
                queue.release(captured.task_id)
            elif action == "forget_prepare":
                token = secrets.token_hex(6)
                plugin.confirmations["panel:" + scope["owner_id"]] = {
                    "token": token, "source": scope_id, "bank": scope["bank"],
                    "revision": scope["revision"], "username": request.username,
                    "until": time.monotonic() + 120,
                }
                return {
                    "confirmation": token,
                    "message": f"将清空 {scope['platform_instance_id']} / {scope['user_id']} "
                    "的全部记忆、来源和待办，绑定账号共用此结果。清空后保持暂停。",
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
                    raise PanelError("确认已失效，请重新选择完整遗忘。")
                queue.forget(scope_id)
            else:
                raise PanelError("操作无效。")
            plugin.worker.wake.set()
            return {"message": {
                "mode": "记忆模式已更新。", "correct": "管理员更正已保存，后台正在整理。",
                "forget_confirm": "旧记忆已停用，后台正在清理。完成后可重新开启记忆。",
            }[action]}
