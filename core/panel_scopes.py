import asyncio
import json
import secrets
import time
from .client import HindsightError
from .background import MAX_BACKGROUND_PAGES, ReadGuard, check_page, context_limits, reference, save_reference
from .agent_tools import read_options, save_options
from . import reflection
from .identity import Incoming, Scope, sha256
from .managed_connections import PanelError
from .outbox import QueueError
from .permissions import pairs
from .source import management_bot_identity
from .materials import load_encoding, token_count
from .reference import PROMPT_TOKEN_LIMIT, reference_options, save_reference_options


class PanelScopes:
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
            account = (scope["platform_instance_id"], scope["user_id"])
            if account in accounts and (not scope.get("historical") or scope.get("availability") == "disconnected"):
                skipped = plugin.skip_status(*account)
                if skipped:
                    summary["skip"] = skipped
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
                if not scope.get("historical") and queue.can_recall(scope, manual=True):
                    try:
                        result[-1]["reference_revision"] = reference_options(queue, scope)["revision"]
                    except QueueError as exc:
                        result[-1]["reference_error"] = self.error_message(str(exc))
                if getattr(plugin, "read_tools", None):
                    try:
                        result[-1]["reading"] = {
                            **plugin.read_tools.public(scope), "scope_revision": scope["revision"],
                        }
                        result[-1]["reflect"] = {
                            **plugin.read_tools.reflect_public(scope), "scope_revision": scope["revision"],
                        }
                    except QueueError as exc:
                        result[-1]["reading"] = {"error": self.error_message(str(exc))}
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
        identity = await management_bot_identity(self.plugin, platform)
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

    async def reference_scope(self, scope_id):
        from astrbot.api.web import request

        if not request.username:
            raise PanelError("请使用 AstrBot 管理账号登录。")
        plugin = self.plugin
        await plugin.prepare_sources()
        scope = self.scope(scope_id)
        source = Scope(*json.loads(scope["identity_json"]))
        bot = await self.bot_id(source.platform_instance_id)
        scope = self.scope(scope_id)
        queue = plugin.outbox
        if bot != source.bot_id or not queue.access.permitted(source) or not queue.can_recall(scope, manual=True):
            raise PanelError("请连接并确认这个用户的机器人，解除记忆库暂停后重试。")
        return queue, scope

    async def references(self, scope_id):
        from astrbot.api.web import request

        posting = request.method == "POST"
        payload = await self.body() if posting else {}
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            await load_encoding()
            queue, scope = await self.reference_scope(scope_id)
            config_revision = self.config_revision()
            if posting:
                if (set(payload) - {"recall_budget", "capture_assistant", "recall_on_quoted_or_link"}
                    != {"revision", "prompt", "types", "exclude_assistant", "detail_limit",
                        "history_limit", "scope_revision", "config_revision"}
                    or type(payload["scope_revision"]) is not int or payload.pop("scope_revision") != scope["revision"]
                    or payload.pop("config_revision") != config_revision):
                    raise QueueError("reference_changed")
                selection = save_reference_options(queue, scope, **payload)
            else:
                selection = reference_options(queue, scope)
            return {"selection": selection, "scope_revision": scope["revision"], "config_revision": config_revision,
                    "prompt_tokens": token_count(selection["prompt"]), "prompt_limit": PROMPT_TOKEN_LIMIT}

    async def prompt_count(self, scope_id):
        payload = await self.body()
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            if set(payload) != {"prompt"} or not isinstance(payload["prompt"], str):
                raise PanelError("请填写有效的记忆提示。")
            await load_encoding()
            await self.reference_scope(scope_id)
            return {"prompt_tokens": token_count(payload["prompt"])}

    async def usage(self, scope_id, turn_id=None):
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            queue, scope = await self.reference_scope(scope_id)
            if turn_id is None:
                return {"turns": queue.references.list(scope)}
            return {"turn": queue.references.detail(scope, turn_id)}

    async def reading(self, scope_id):
        from astrbot.api.web import request

        posting = request.method == "POST"
        payload = await self.body() if posting else {}
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            plugin = self.plugin
            await plugin.prepare_sources()
            scope = self.scope(scope_id)
            source = Scope(*json.loads(scope["identity_json"]))
            if not scope["allowed"] or await self.bot_id(source.platform_instance_id) != source.bot_id:
                raise PanelError("请连接机器人并确认用户身份；旧记忆库不能更改读取方式。")
            guard = ReadGuard.freeze(plugin, scope, manual=True)
            revision = self.config_revision()
            options = read_options(guard.queue, scope)
            closing = posting and payload.get("mode") == "automatic"
            if not guard.valid(plugin, metadata=closing):
                raise QueueError("scope_not_available")
            if posting:
                if (
                    set(payload) != {"mode", "revision", "scope_revision", "config_revision"}
                    or not isinstance(payload.get("mode"), str) or payload["mode"] not in {"automatic", "tools"}
                    or type(payload.get("revision")) is not int or payload["revision"] != options["revision"]
                    or type(payload.get("scope_revision")) is not int or payload["scope_revision"] != scope["revision"]
                    or payload.get("config_revision") != revision
                ):
                    raise QueueError("reading_changed")
                await plugin.prepare_sources()
                if (
                    not guard.valid(plugin, metadata=closing) or self.config_revision() != revision
                    or await self.bot_id(source.platform_instance_id) != source.bot_id
                    or not guard.valid(plugin, metadata=closing)
                ):
                    raise QueueError("reading_changed")
                options = save_options(guard.queue, scope, revision=options["revision"], mode=payload["mode"])
            return {"selection": options, "scope_revision": scope["revision"], "config_revision": revision,
                    "message": "记忆读取方式已保存。"}

    async def reflect(self, scope_id):
        from astrbot.api.web import request

        posting = request.method == "POST"
        payload = await self.body() if posting else {}
        async with self.lock, self.plugin.using_resources(), asyncio.timeout(8):
            if not request.username:
                raise PanelError("请使用 AstrBot 管理账号登录。")
            plugin = self.plugin
            await plugin.prepare_sources()
            scope = self.scope(scope_id)
            source = Scope(*json.loads(scope["identity_json"]))
            if not scope["allowed"] or await self.bot_id(source.platform_instance_id) != source.bot_id:
                raise PanelError("请连接机器人并确认用户身份；旧记忆库不能更改 Reflect 设置。")
            guard = ReadGuard.freeze(plugin, scope, manual=True)
            revision = self.config_revision()
            options = reflection.read_options(guard.queue, scope)
            closing = posting and payload.get("enabled") is False
            if not guard.valid(plugin, metadata=closing):
                raise QueueError("scope_not_available")
            if posting:
                if (
                    set(payload) != {"enabled", "timeout", "revision", "scope_revision", "config_revision"}
                    or type(payload.get("enabled")) is not bool or type(payload.get("timeout")) is not int
                    or not 3 <= payload["timeout"] <= 30
                    or type(payload.get("revision")) is not int or payload["revision"] != options["revision"]
                    or type(payload.get("scope_revision")) is not int or payload["scope_revision"] != scope["revision"]
                    or payload.get("config_revision") != revision
                ):
                    raise QueueError("reflect_changed")
                await plugin.prepare_sources()
                if (
                    not guard.valid(plugin, metadata=closing) or self.config_revision() != revision
                    or reflection.read_options(guard.queue, scope) != options
                    or await self.bot_id(source.platform_instance_id) != source.bot_id
                    or not guard.valid(plugin, metadata=closing)
                ):
                    raise QueueError("reflect_changed")
                options = reflection.save_options(guard.queue, scope, revision=options["revision"],
                                                  enabled=payload["enabled"], timeout=payload["timeout"])
            return {"selection": options, "scope_revision": scope["revision"], "config_revision": revision,
                    "message": "Reflect 设置已保存。"}

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
            if action == "reflect_override":
                if not isinstance(payload.get("config_revision"), str):
                    raise PanelError("请重新获取用户状态后解除 Reflect 阻塞。")
                self.check_revision(payload)
                bank = payload.get("bank")
                if payload.get("confirm_override") is not True:
                    raise PanelError("请先确认 Hindsight 已重启或没有正在运行的 Reflect 请求。")
                allowed_banks = {scope["bank"], *(row["old_bank"] for row in queue.control.erasures(
                    queue.connection_id, owner_id=scope["owner_id"],
                ) if row["phase"] != "verified")}
                unknown_banks = {row["bank"] for row in queue.control.reflection_pending(
                    queue.connection_id, owner_id=scope["owner_id"],
                ) if row["state"] == "unknown"}
                if not isinstance(bank, str) or bank not in allowed_banks & unknown_banks:
                    raise PanelError("这个记忆库没有可解除的 Reflect 阻塞，请重新获取用户状态。")
                changed = queue.control.override_unknown_reflections(queue.connection_id, bank, actor=request.username)
                plugin.worker.wake.set()
                return {"overridden": changed, "message": "Reflect 阻塞已解除，清空中的旧记忆仍会继续核查和删除。"}
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
