import asyncio
import hashlib
import hmac
import json
import re
import time

from ..deploy.model_transport import LEGACY_ENDPOINT, transport_environment


class PanelError(ValueError):
    pass


def configuration_fingerprint(environment, api_key):
    encoded = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(api_key.encode(), encoded, hashlib.sha256).hexdigest()


class ManagedConnections:
    def __init__(self, panel):
        self.panel = panel

    @staticmethod
    def public(job):
        result = {key: job[key] for key in ("request_id", "status", "stage", "message", "saved") if key in job}
        result["config_revision"] = job.get("config_revision", job.get("expected_revision", ""))
        return result

    def persist(self, job, **updates):
        panel = self.panel
        state = panel.state()
        stored = state.get("connection_job", {})
        if stored.get("request_id") != job["request_id"] or state.get("id") != job["installation_id"]:
            raise PanelError("配套接入任务已变化，请刷新页面。")
        job.update(updates)
        state["connection_job"] = dict(job)
        panel.store_state(state)
        panel.model_job = self.public(job)

    def check(self, job):
        panel = self.panel
        state = panel.managed_target({"mode": "managed", "installation_id": job["installation_id"]})
        if job["expected_revision"] != panel.config_revision():
            raise PanelError("接入期间配置已变化，请重新保存并连接。")
        target_service = state.get("connections", {}).get(panel.connection_url(state["api_url"]))
        if target_service and any((state.get(key) or {}).get("connection_id") == target_service
                                  for key in ("upgrade_check", "upgrade_pauses")):
            raise PanelError("配套升级尚未结束，请先核对升级结果并解除升级暂停。")
        return state

    def start(self, job, message):
        self.persist(job, status="running", message=message)
        self.panel.model_selection = job["selection"]
        self.panel.model_task = asyncio.create_task(self.run(job), name="hindsight-managed-connect")

    async def connect(self, payload):
        panel = self.panel
        panel.managed_target(payload)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
            raise PanelError("请求标识无效，请重新保存并连接。")
        selection = {key: payload.get(key, "") for key in ("chat_provider_id", "embedding_provider_id", "rerank_provider_id")}
        if panel.model_task and not panel.model_task.done():
            if selection != panel.model_selection:
                raise PanelError("模型配置正在处理，请等待完成后再更换模型。")
            return dict(panel.model_job)
        async with panel.lock:
            state = panel.managed_target(payload)
            old = state.get("connection_job")
            if isinstance(old, dict) and old.get("request_id") == request_id:
                if old.get("selection") != selection:
                    raise PanelError("同一请求的模型选择已变化，请重新提交。")
                if old.get("status") == "succeeded":
                    self.check(old)
                    return self.public(old)
                job = dict(old)
            else:
                panel.check_revision(payload)
                job = {"request_id": request_id, "installation_id": state["id"], "selection": selection,
                       "expected_revision": panel.config_revision(), "fingerprint": "", "stage": "validating"}
                state["connection_job"] = job
                panel.store_state(state)
            self.check(job)
            self.start(job, "正在检查模型与配套服务…")
            return dict(panel.model_job)

    async def status(self, payload):
        panel = self.panel
        state = panel.managed_target(payload)
        job = state.get("connection_job")
        if not isinstance(job, dict):
            return {"status": "idle"}
        if job.get("status") in {"running", "interrupted"} and (not panel.model_task or panel.model_task.done()):
            async with panel.lock:
                state = panel.managed_target(payload)
                job = state.get("connection_job")
                if (not isinstance(job, dict) or job.get("status") not in {"running", "interrupted"}
                        or panel.model_task and not panel.model_task.done()):
                    return self.public(job) if isinstance(job, dict) else {"status": "idle"}
                self.check(job)
                self.start(job, "正在核对接入结果…")
        return self.public(panel.state()["connection_job"])

    async def wait_for_configuration(self, job, state, status):
        panel = self.panel
        if status.get("configuration_fingerprint") is None:
            if status["ready"] and await panel.managed_backend_version(state) == "1.1.0":
                raise PanelError("请从「服务管理」升级 Hindsight 1.1.0。")
            raise PanelError("配套组件不支持接入结果核对，请先重新部署配套服务文件，再保存并连接。")
        deadline = time.monotonic() + 360
        while status.get("applying"):
            if not job["fingerprint"] or status.get("applying_fingerprint") != job["fingerprint"]:
                raise PanelError("配套服务正在应用其他模型配置，请等待完成后重试。")
            if time.monotonic() >= deadline:
                raise PanelError("模型应用仍在进行，请稍后重新打开页面核对结果。")
            await asyncio.sleep(2)
            self.check(job)
            status = await panel.managed_request(state)
        return status

    async def run(self, job):
        panel = self.panel
        plugin = panel.plugin
        closed_active = False
        try:
            async with panel.lock:
                state = self.check(job)
                previous_url = state["api_url"]
                status = await panel.managed_request(state)
                self.check(job)
                if state["api_url"] != previous_url or state.get("management_url") != panel.state().get("management_url"):
                    panel.store_state(state)
                    self.persist(job, expected_revision=panel.config_revision())
                status = await self.wait_for_configuration(job, state, status)
                ready_fingerprint = status.get("configuration_fingerprint") if status["ready"] else ""
                if not job["fingerprint"] or ready_fingerprint != job["fingerprint"]:
                    environment = await panel.providers().resolve_checked(job["selection"])
                    self.check(job)
                    fingerprint = configuration_fingerprint(environment, state["api_key"])
                    descriptors = json.loads(environment.get("ASTRBOT_MEMORY_MODEL_CONFIG", "{}"))
                    legacy = transport_environment(environment, descriptors, LEGACY_ENDPOINT)
                    compatible = {fingerprint, configuration_fingerprint(legacy, state["api_key"])}
                    if job["fingerprint"] and job["fingerprint"] not in compatible:
                        raise PanelError("模型供应商配置已变化，请重新提交保存并连接。")
                    if ready_fingerprint in compatible:
                        self.persist(job, fingerprint=ready_fingerprint)
                    else:
                        self.persist(job, fingerprint=fingerprint, stage="applying", message="正在验证模型并启动记忆服务…")
                        if (panel.connection_url(plugin.config.get("api_url", "")) == panel.connection_url(state["api_url"])
                                and plugin.config.get("api_key") == state["api_key"] and plugin.outbox):
                            plugin.stopping = True
                            closed_active = True
                            await plugin._close_resources()
                            self.check(job)
                        status = await panel.managed_request(state, environment)
                        self.check(job)
                        if not status["ready"] or status.get("configuration_fingerprint") != fingerprint:
                            raise PanelError("模型应用结果尚未确认，请稍后重试核对。")
                state = self.check(job)
                state.update(job["selection"])
                panel.store_state(state)
                self.persist(job, expected_revision=panel.config_revision(), stage="connecting", message="模型已保存，正在连接插件…")
                client = plugin.client
                active = (
                    panel.managed_connection(state) and plugin.outbox and not plugin.stopping and client
                    and client.url == panel.connection_url(state["api_url"])
                    and client.api_key == state["api_key"]
                    and plugin.outbox.connection_id == plugin.config.get("service_id")
                )
                if closed_active or not active:
                    await panel._save({"api_url": state["api_url"], "api_key": state["api_key"], "mode": "managed"},
                                      panel.config_revision(), force_initialize=True, job=job)
                else:
                    async with plugin.using_resources(), asyncio.timeout(10):
                        await plugin.client.ensure_auth(force=True)
                    self.check(job)
                self.persist(job, expected_revision=panel.config_revision(), status="succeeded", stage="complete", saved=True,
                             config_revision=panel.config_revision(), message="模型已保存，插件已连接。")
        except asyncio.CancelledError:
            self.persist(job, status="interrupted", message="接入已中断，重新打开页面可核对结果。")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, (PanelError, ValueError)) else "接入失败，请检查配套服务和插件数据目录后重试。"
            self.persist(job, status="failed", message=message)
        finally:
            if closed_active and not plugin.outbox and not panel.closed and not asyncio.current_task().cancelling():
                plugin.permissions = None
                plugin.stopping = False
                await plugin.initialize()
