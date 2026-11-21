import asyncio
from pathlib import Path
import re
import socket
import time
from .client import HindsightError
from .identity import sha256
from .installer import SUPPORTED_VERSIONS, UPGRADE_PATHS
from .managed_connections import PanelError


class PanelUpgrade:
    async def service_status(self, *, force=True):
        plugin, state = self.plugin, self.upgrade_state()
        client = plugin.client
        local_installation = self.supports_installation()
        pending, paused = self.upgrade_progress(state)
        managed = (local_installation or pending or paused) and self.managed_connection(state) and (
            not client or self.connection_url(state["api_url"]) == client.url
        )
        ignored = state.get("ignored_upgrades", {})
        result = {
            "version": None, "target": self.target_version if managed else None,
            "managed": managed, "upgrade_available": False,
            "pending": managed and pending, "upgrade_paused": managed and paused,
            "resume_available": managed and paused and bool((state.get("upgrade_check") or {}).get("verified")),
            "ignored": bool(managed and isinstance(ignored, dict)
                            and ignored.get(self.connection_revision()) == self.target_version),
        }
        if (not client or not plugin.outbox or plugin.stopping
                or not isinstance(getattr(client, "api_key", None), str)
                or not callable(getattr(client, "request", None))):
            return result
        identity = (
            client.url, sha256(client.api_key), self.config_revision(),
            local_installation, pending, paused,
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
                          upgrade_available=local_installation and managed and UPGRADE_PATHS.get(version) == self.target_version)
        except (HindsightError, TimeoutError, PanelError):
            result["message"] = "暂时无法核对服务版本，请检查连接后重试。"
        if plugin.client is not client or plugin.stopping:
            return {"version": None, "target": self.target_version, "upgrade_available": False}
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
                payload.get("target") != self.target_version
                or payload.get("connection_revision") != self.connection_revision()
            ):
                raise PanelError("服务连接或目标版本已变化，请重新获取升级提示。")
            state = self.state()
            if not isinstance(state.get("ignored_upgrades"), dict):
                state["ignored_upgrades"] = {}
            state.setdefault("ignored_upgrades", {})[self.connection_revision()] = self.target_version
            self.store_state(state)
            self.version_cache = None
            return {"config_revision": self.config_revision(), "message": "已忽略这个版本的升级提示。"}

    async def upgrade(self):
        from astrbot.api.web import request

        payload = await self.body()
        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")
        action = payload.get("action")
        recovery = action in ("verify", "resume", "cancel")
        if not recovery:
            self.ensure_local_installation()
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(60):
            self.check_revision(payload)
            plugin, state = self.plugin, self.upgrade_state()
            status = await self.service_status()
            if not status.get("managed") or not plugin.outbox or plugin.outbox.recovering:
                raise PanelError("此入口只升级当前插件的配套服务；外部服务请由其管理员升级。")
            try:
                managed_status = await self.managed_request(state, recover_upgrade=recovery)
            except PanelError:
                if recovery and not self.supports_installation():
                    raise PanelError(
                        "无法核对原配套服务的管理接口。请回到原 Linux 部署环境，使用原连接完成或取消升级。"
                    ) from None
                raise
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

            if action in {"verify", "resume"}:
                check = state.get("upgrade_check")
                if not check or status["version"] != self.target_version:
                    raise PanelError(f"尚未确认服务升级到 {self.target_version}；保持保存和查询暂停，执行命令后再核对。")
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
            if UPGRADE_PATHS.get(status["version"]) != self.target_version:
                raise PanelError(f"此升级入口只支持实际运行的 Hindsight {'、'.join(UPGRADE_PATHS)}；请重新检查版本。")
            if action == "cancel":
                if state.get("upgrade_check"):
                    inspected = await verify_original(state["upgrade_check"], allow_running=True)
                else:
                    inspected = {row["bank"]: row for row in await inspect_banks(allow_running=True)}
                count = restore_upgrade_pauses(inspected)
                state.pop("upgrade_check", None)
                self.store_state(state)
                self.version_cache = None
                return {"message": f"继续使用 {status['version']}，已解除升级造成的 {count} 个用户暂停。原有暂停保留；已复制的升级命令不会撤销，请勿再执行。",
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
            result = self.installation_command(state,
                                  container_hostname=socket.gethostname() if Path("/.dockerenv").exists() else "",
                                  setup_path=str(self.directory() / "setup.json"),
                                  upgrade=True)
            state["upgrade_check"] = {"banks": banks, "verified": False, "from": status["version"], "to": self.target_version,
                                      "connection_id": queue.connection_id,
                                      "embedding_fingerprint": managed_status["embedding_fingerprint"]}
            self.store_state(state)
            self.version_cache = None
            return {"command": result["command"], "message": result["message"],
                    "config_revision": self.config_revision()}
