import asyncio
import copy
import json
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import urlsplit

import httpx

from .client import HindsightError
from .background import context_limits
from .diagnostics import ConfigurationError, connection_fields, recall_timeout
from .installer import SUPPORTED_VERSIONS, UPGRADE_PATHS, uninstallation
from .managed_connections import PanelError
from .permissions import pairs
from .source import local_runner, valid_user_id
from . import settings
from .materials import TokenizerError


class PanelSetup:
    def credentials(self, payload):
        url = payload.get("api_url", self.plugin.config.get("api_url", ""))
        key = payload.get("api_key", "")
        if not isinstance(url, str) or not isinstance(key, str):
            raise PanelError("请填写有效的服务地址与 Key。")
        url, key = url.strip(), key.strip()
        if not key:
            if self.connection_url(url) == self.connection_url(self.plugin.config.get("api_url", "")):
                key = str(self.plugin.config.get("api_key", ""))
            if not key and self.supports_installation():
                state = self.state()
                if self.connection_url(url) == self.connection_url(state.get("api_url", "")):
                    key = str(state.get("api_key", ""))
        if not url or not key:
            raise PanelError("请填写 Hindsight 的 API 地址与密钥。")
        return url, key

    async def check(self):
        payload = await self.body()
        url, key = self.credentials(payload)
        state = self.state()
        if (
            self.supports_installation() and state.get("management_url")
            and self.managed_connection(state, api_url=url, api_key=key)
        ):
            status = await self.managed_request(state)
            url = state["api_url"]
            if not status["ready"]:
                return {"ok": True, "ready": False, "message": "配套服务已安装，请选择模型并保存。"}
        client = self.new_client(url, key)
        try:
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
                self.remember_auth(client)
        finally:
            await client.close()
        self.connection_ready(client.url, key)
        return {"ok": True, "ready": True, "message": "服务连接与 API 认证通过。"}

    def network_endpoints(self, state):
        path = self.directory() / "managed-network.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("installation_id") != state.get("id"):
                return None
            endpoints = {name: value[name] for name in ("api_url", "management_url")}
            addresses = [urlsplit(url) for url in endpoints.values()]
            if any(
                address.scheme != "http" or not address.hostname or address.username is not None
                or address.password is not None or address.path or address.query or address.fragment
                for address in addresses
            ) or addresses[0].hostname != addresses[1].hostname:
                raise ValueError()
            return endpoints
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise PanelError("配套网络记录损坏，请重新执行安装命令。") from None

    async def managed_request(self, state, environment=None, *, recover_upgrade=False):
        if not recover_upgrade or environment is not None or not any(self.upgrade_progress(state)):
            self.ensure_local_installation()
        endpoints = self.network_endpoints(state)
        url, key = state.get("management_url"), state.get("management_key")
        if endpoints:
            url = endpoints["management_url"]
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
                if endpoints:
                    state.update(endpoints)
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
        self.ensure_local_installation()
        payload = await self.body()
        async with self.route(resources=False, timeout=None):
            self.check_revision(payload)
            self.ensure_no_upgrade()
            explicit = payload.get("mode") == "managed"
            if not explicit and (self.state().get("api_url") or self.plugin.config.get("api_url")):
                self.require_managed()
            status = await self.service_status()
            if status.get("managed") and status.get("upgrade_available"):
                raise PanelError(f"当前服务为 {status['version']}，请使用「升级 Hindsight」暂停保存和查询、备份并升级。")
            if (status.get("managed") and status.get("version") is None
                    and self.plugin.config.get("api_url") and payload.get("repair") is not True):
                raise PanelError(f"无法确认已有服务版本。请恢复连接；重新部署命令只会替换实际确认的 {self.target_version} 服务，不会升级未知版本。")
            state = self.state(create=True)
            if "control_plane_url" in payload:
                state["control_plane_url"] = self.control_plane_url(payload["control_plane_url"])
            result = self.installation_command(
                state,
                container_hostname=socket.gethostname() if Path("/.dockerenv").exists() else "",
                setup_path=str(self.directory() / "setup.json"),
            )
            for field in ("api_url", "management_url"):
                if not Path("/.dockerenv").exists() or not state.get(field):
                    state[field] = result[field]
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
        self.ensure_local_installation()
        self.ensure_open()
        state = self.state()
        if payload.get("mode") != "managed" or payload.get("installation_id") != state.get("id"):
            raise PanelError("配套安装记录已变化，请重新选择配套服务。")
        if not re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", ""))) or not all(
            isinstance(state.get(key), str) and state[key] for key in ("api_url", "api_key", "management_url", "management_key")
        ):
            raise PanelError("请先生成并执行配套服务安装命令。")
        return state

    async def managed_options(self):
        self.ensure_local_installation()
        payload = await self.body()
        self.check_revision(payload)
        if payload.get("mode") != "managed":
            raise PanelError("请先选择安装配套服务。")
        state = self.state()
        present = bool(re.fullmatch(r"[0-9a-f]{12}", str(state.get("id", ""))) and state.get("api_url"))
        return {"installation_id": state.get("id", "") if present else "", "api_url": state.get("api_url", "") if present else "",
                "has_installation": present, "providers": self.providers().options(),
                "selection": {key: state.get(key, "") for key in ("chat_provider_id", "embedding_provider_id", "rerank_provider_id")},
                "job": self.managed_connections.public(state["connection_job"]) if isinstance(state.get("connection_job"), dict) else None}

    async def managed_connect(self):
        return await self.managed_connections.connect(await self.body())

    async def managed_status(self):
        return await self.managed_connections.status(await self.body())

    async def managed_version(self):
        state = self.managed_target(await self.body())
        status = await self.managed_request(state)
        result = {"managed": True, "target": self.target_version, "version": None, "upgrade_available": False}
        if not status["configured"] or not status["ready"]:
            return {**result, "message": "配套组件已安装，请先选择模型并点击「保存并连接」。"}
        version = await self.managed_backend_version(state)
        return {**result, "version": version, "supported": version in SUPPORTED_VERSIONS,
                "upgrade_available": UPGRADE_PATHS.get(version) == self.target_version}

    async def managed_backend_version(self, state):
        client = self.new_client(state["api_url"], state["api_key"])
        try:
            async with asyncio.timeout(10):
                await client.ensure_auth(force=True)
                response = await client.request("GET", "/v1/version")
                self.remember_auth(client)
                version = response.get("api_version")
                if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                    raise PanelError("服务版本响应无效，请检查 Hindsight 服务。")
                return version
        finally:
            await client.close()

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
        if self.supports_installation() and self.managed_connection() and self.model_job:
            return dict(self.model_job)
        return {"status": "idle"}

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

    async def apply_models(self, payload):
        async with self.route(resources=False, timeout=None):
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
        self.ensure_local_installation()
        payload = await self.body()
        async with self.route(resources=False, timeout=None):
            self.ensure_no_upgrade()
            self.check_revision(payload)
            state = self.require_managed()
            if payload.get("delete_memory_data") is not True:
                raise PanelError("请确认删除这套配套服务及全部记忆数据。")
            return uninstallation(state)

    async def save(self):
        payload = await self.body()
        async with self.route(resources=False, timeout=None):
            self.ensure_no_upgrade()
            self.check_revision(payload)
            revision = self.config_revision()
            return await self._save(payload, revision)

    async def save_parameters(self):
        from astrbot.api.web import request

        payload = await self.body()
        async with self.route(resources=False, timeout=None):
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
        async with self.route(resources=False, timeout=None):
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
            client = self.new_client(url, key)
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
            if mode == "managed" and ("mode" in payload or not local_change):
                self.ensure_local_installation()
            if not local_change:
                if self.supports_installation() and mode != "existing" and state.get("management_url") and self.managed_connection(
                    state, api_url=client.url, api_key=key,
                ):
                    status = await self.managed_request(state)
                    if not status["configured"] or not status["ready"]:
                        raise PanelError("请先选择模型并点击「保存模型」，服务启动成功后再保存连接。")
                async with asyncio.timeout(10):
                    await client.ensure_auth(force=True)
                    self.remember_auth(client)
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
        state = self.state()
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
            self.managed_connections.persist(job, expected_revision=self.config_revision())
        updates["service_id"] = service
        plugin.stopping = True
        write_started = False
        applied_config = None
        try:
            await plugin._close_resources()
            self.ensure_open()
            if job is not None:
                self.managed_connections.check(job)
            write_started = True
            self.write_config(updates)
            if job is not None:
                self.managed_connections.persist(job, expected_revision=self.config_revision())
            applied_config = copy.deepcopy(dict(plugin.config))
            plugin.permissions = None
            plugin.stopping = False
            await plugin.initialize()
            self.ensure_open()
            if job is not None:
                self.managed_connections.check(job)
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
                self.managed_connections.persist(job, expected_revision=self.config_revision())
            plugin.permissions = None
            self.ensure_open()
            plugin.stopping = False
            await plugin.initialize()
            if configuration_changed:
                raise PanelError("接入期间配置已变化，请重新保存并连接。") from None
            raise PanelError("配置应用失败，已恢复原有服务配置，请检查服务与数据目录。") from None
        return self.settings_saved()
