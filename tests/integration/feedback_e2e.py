"""Native Dashboard, upgrade, control migration and page feedback acceptance."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
import shutil
import sqlite3
import sys

import aiohttp
from playwright.async_api import async_playwright

from live_e2e import Acceptance, ALICE, BOB, NAME, OneBot, ROOT, database, jsonl, until


class Feedback(Acceptance):
    def __init__(self):
        super().__init__()
        self.report.update(
            acceptance_scope="native Dashboard feedback, baseline upgrade and snapshot recovery",
            chat_model="synthetic OpenAI fixture", embedding_model="synthetic 64-dimension fixture", embedding_dimensions=64, rerank_model="rrf",
            not_covered=["real QQ login", "production model quality"],
        )
        if self.settings.get("feedback_only"):
            self.report["acceptance_scope"] = "native Dashboard page feedback, identity invalidation and snapshot recovery"
            self.report["not_covered"].append("baseline upgrade")
        if self.settings.get("view_only"):
            self.report["acceptance_scope"] = "native Dashboard responsive layout and invalidated selection"
            self.report["not_covered"].append("chat feedback and business snapshot recovery")
        if self.settings.get("recovery_only"):
            self.report["acceptance_scope"] = "native erasure and business snapshot recovery"
            self.report["not_covered"].append("chat feedback and responsive layout")

    async def layout(self, page, frame):
        await page.set_viewport_size({"width": 390, "height": 844})
        async def fits():
            return await frame.locator("body").evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        try:
            await until(fits, timeout=10)
        finally:
            self.report["measurements"]["phone_layout"] = await frame.locator("body").evaluate("""() => ({
              viewport: innerWidth, width: document.documentElement.scrollWidth,
              overflow: [...document.querySelectorAll('body *')].filter(el => {
                const box = el.getBoundingClientRect(); return box.width && box.right > innerWidth + 1;
              }).slice(0, 12).map(el => ({id: el.id, tag: el.tagName, right: el.getBoundingClientRect().right}))
            })""")
            await page.screenshot(path=str(ROOT / "feedback-phone.png"), full_page=True)
        self.passed("native_phone_layout_has_no_horizontal_overflow_with_invalidated_selection")

    async def plugin_descriptions(self, page):
        detail = await self.http("GET", "/api/v1/plugins/by-id?plugin_id=" + NAME)
        components = detail["components"]
        group = next(component for component in components if component.get("name") == "hb")
        commands = group.get("subcommands", [])
        assert {command["name"] for command in commands} == {"help", "status", "on", "readonly", "pause", "forget"}
        descriptions = [group, *commands, *(component for component in components if component.get("type") in {"hook", "listener"})]
        expected_handlers = {"observe_incoming", "bind_memory_tools", "remember_assistant", "add_memory"}
        assert expected_handlers <= {component.get("handler_name") for component in descriptions}
        assert all(component.get("description") and component["description"] != "无描述"
                   and re.search(r"[\u4e00-\u9fff]", component["description"]) for component in descriptions)
        await page.goto(self.dashboard + "/#/extension/plugins/" + NAME)
        await self.dismiss_host_notice(page)
        await page.locator("#plugin-components .handler-row__desc").first.wait_for(timeout=30000)
        command_group = page.locator("#plugin-components tr.command-row--group").filter(
            has=page.locator("code").filter(has_text=re.compile(r"^hb$")),
        )
        await command_group.locator("button.command-cell__toggle").click()
        child_rows = page.locator("#plugin-components tr.command-row--sub")
        await child_rows.nth(len(commands) - 1).wait_for(timeout=10000)
        assert await child_rows.count() == len(commands)
        for command in commands:
            child = child_rows.filter(has=page.locator("code").filter(has_text=re.compile("^" + re.escape(command["name"]) + "$")))
            assert await child.count() == 1, command["name"]
            assert await child.locator(".handler-row__desc").inner_text() == command["description"]
        rendered = await page.locator("#plugin-components .handler-row__desc").all_text_contents()
        assert len(rendered) >= len(descriptions), rendered
        assert all(text.strip() and "无描述" not in text for text in rendered), rendered
        self.report["measurements"]["plugin_descriptions"] = {
            "commands": sorted(command["name"] for command in commands),
            "handlers": sorted(expected_handlers), "rendered_descriptions": len(rendered),
        }
        self.passed("native_plugin_details_show_six_commands_listener_and_three_hooks_with_chinese_descriptions")

    async def switch_bot(self, frame, old_scope, old_page):
        old_bot = self.bot.bot_id
        await self.bot.close()
        self.bot = OneBot(self.session, {**self.settings, "bot_id": old_bot + 1})
        await self.bot.connect()
        await self.bot.chat(ALICE, (await self.command_prefix()) + " status")
        await self.confirm(ALICE)
        current = await self.scope()
        assert current["bot_id"] == str(old_bot + 1) and current["bank"] != old_scope["bank"]
        selection = current["background"]
        assert selection["revision"] == 0 and selection["invalidated"] and not selection["page_id"]
        assert not selection["mental_model_id"]
        await frame.locator("#bank-picker option[value='" + current["scope_id"] + "']").wait_for(state="attached", timeout=15000)
        await frame.locator("#bank-picker").select_option(current["scope_id"])
        await frame.locator("#background-status").filter(has_text="选择已失效").wait_for(timeout=15000)
        assert await frame.locator("#background-page").input_value() == ""
        if old_page:
            retained = await self.native("GET", old_scope["bank"] + "/knowledge-base/pages/" + old_page)
            assert retained["id"] == old_page
        self.report["measurements"]["bot_switch"] = {
            "old_bot": old_bot, "new_bot": self.bot.bot_id, "old_bank": old_scope["bank"],
            "new_bank": current["bank"], "selection_revision": selection["revision"], "old_page_retained": bool(old_page),
        }
        self.passed("native_new_bot_login_creates_revision_zero_scope_with_notice_without_copying_old_reference")

    async def native(self, method, path, body=None):
        async with self.session.request(method, self.settings["hindsight_url"] + "/v1/default/banks/" + path,
                                        json=body, headers={"Authorization": "Bearer " + self.settings["memory_key"]}) as response:
            assert response.status < 400, (method, path, response.status)
            return await response.json()

    async def completed(self, bank, operation):
        async def ready():
            value = await self.native("GET", bank + "/operations/" + operation)
            assert value["status"] not in {"failed", "cancelled"}, value
            return value["status"] == "completed"
        await until(ready, timeout=90)

    async def scope(self, user=ALICE):
        async def ready():
            return next((row for row in (await self.bootstrap())["scopes"]
                         if row["user_id"] == str(user) and not row["historical"] and not row["pending"]), None)
        return await until(ready, timeout=30)

    async def page(self, bank, query, *, wait=True):
        value = await self.native("POST", bank + "/knowledge-base/pages", {
            "name": query, "source_query": query, "max_tokens": 8192,
            "trigger": {"refresh_after_consolidation": False, "fact_types": ["world"]},
        })
        if not wait:
            return value["page_id"], value["operation_id"]
        await self.completed(bank, value["operation_id"])
        return value["page_id"]

    async def select(self, page_id):
        scope = await self.scope()
        route = self.prefix + "/scopes/" + scope["scope_id"] + "/background"
        value = await self.http("GET", route)
        await self.http("POST", route, payload={
            "page_ids": [page_id], "enabled": True, "revision": value["selection"]["revision"],
            "scope_revision": value["scope_revision"], "config_revision": value["config_revision"],
        })

    async def restart(self, mutation):
        await self.bot.close()
        await self.control("stop_astrbot")
        mutation()
        await self.control("start_astrbot")
        self.bot = OneBot(self.session, self.settings)
        await self.bot.connect()
        await self.bot.chat(ALICE, (await self.command_prefix()) + " status")
        await self.usable()

    async def single_page(self, page, frame, bank, short):
        from parameter_panel import save_parameters

        async def configure(**updates):
            if "context_max_chars" in updates:
                state = await self.bootstrap()
                await self.http("POST", self.prefix + "/parameters/save", payload={
                    "config_revision": state["config_revision"],
                    "parameters": {**state["parameters"]["values"], **updates},
                })
                await page.reload()
                await self.dismiss_host_notice(page)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                return
            return await save_parameters(self, frame, **updates)

        async def selected_check():
            scope = await self.scope()
            return await self.http("GET", self.prefix + "/scopes/" + scope["scope_id"] + "/background")

        async def check_row(value, page_id, *, exceeded=False):
            native = next(item for item in value["pages"] if item["page_id"] == page_id)
            check = next(item for item in value["checks"] if item["page_id"] == page_id)
            assert value["selection"]["page_id"] == page_id
            row = frame.locator(".knowledge-page").filter(
                has=frame.get_by_text("1. " + native["path"], exact=True)
            )
            metrics = f'{check["tokens"]} / {check["limits"]["tokens"]} token'
            if exceeded:
                metrics += "（超限）"
            await row.locator(".page-metrics").filter(
                has_text=re.compile("^" + re.escape(metrics) + "$")
            ).wait_for(timeout=15000)
            index = row.get_by_text("有新记忆待刷新", exact=True)
            if native.get("index_stale") is True:
                await index.wait_for()
            else:
                assert await index.count() == 0

        async def chat(used, code=""):
            before = (await self.scope())["background"].get("status", {}).get("checked_at", 0)
            count = len(self.requests())
            await self.bot.chat(ALICE, "A synthetic single page budget conversation.")
            async def finished():
                scope = await self.scope()
                status = scope["background"].get("status", {})
                return status if status.get("checked_at", 0) > before and len(self.requests()) > count else None
            status = await until(finished, timeout=35)
            assert status["used"] is used, status
            if code:
                assert code in status["message"], status
            return self.latest_wire()

        baseline = await selected_check()
        assert baseline["selection"]["page_id"] == short
        assert baseline["budget"] == {"tokens": 1200, "chars": 4000, "page_tokens": 400, "page_timeout": 1}
        await chat(True)
        self.passed("single_reference_and_default_budgets_and_wait_are_preserved")
        medium = await self.page(bank, "HB-BUDGET-MEDIUM")
        await configure(recall_max_tokens=600)
        await self.usable()
        await self.select(medium)
        checked = await selected_check()
        assert 200 < checked["check"]["tokens"] <= 400 and checked["check"]["code"] == "", checked
        assert "neutral neutral" in await chat(True)
        self.passed("independent_400_token_page_survives_shared_600_token_limit")

        long = await self.page(bank, "HB-FEEDBACK-LONG")
        await self.select(long)
        checked = await selected_check()
        assert checked["check"]["code"] == "background_too_large", checked
        await page.reload()
        await self.dismiss_host_notice(page)
        await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
        await check_row(checked, long, exceeded=True)
        assert "HB-FEEDBACK-LONG complete paragraph." not in await chat(False, "知识页太长")
        self.report["measurements"]["long_page_check"] = checked["check"]
        self.passed("selection_size_warning_and_index_state_survive_native_dashboard_reload")

        await configure(background_max_tokens=4096, recall_max_tokens=6000, context_max_chars=24000)
        await self.usable()
        checked = await selected_check()
        assert checked["check"]["code"] == "" and checked["check"]["tokens"] > 400
        wire = await chat(True)
        assert ("HB-FEEDBACK-LONG complete paragraph. " * 120).rstrip() in wire
        await self.drained()
        assert "HB-FEEDBACK-LONG complete paragraph." not in json.dumps(database(self.runtime, "SELECT payload FROM outbox"))
        saved_parameters = (await self.bootstrap())["parameters"]["values"]
        await self.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
        await self.usable()
        await page.reload()
        await self.dismiss_host_notice(page)
        await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
        assert (await self.bootstrap())["parameters"]["values"] == saved_parameters
        assert (await self.scope())["background"]["page_id"] == long
        await frame.locator("#background-recheck").click()
        await check_row(await selected_check(), long)
        assert ("HB-FEEDBACK-LONG complete paragraph. " * 120).rstrip() in await chat(True)
        self.passed("native_panel_expands_three_budgets_and_injects_entire_long_page_after_reload_without_material_backflow")
        for settings in ({"context_max_chars": 256}, {"context_max_chars": 24000, "recall_max_tokens": 256},
                         {"recall_max_tokens": 6000, "background_max_tokens": 400}):
            await configure(**settings)
            await self.usable()
            assert (await selected_check())["check"]["code"] == "background_too_large"
            wire = await chat(False, "知识页太长")
            assert "HB-FEEDBACK-LONG complete paragraph." not in wire
        self.passed("independent_page_shared_token_and_shared_character_ceilings_each_skip_whole_page")

        await configure(background_max_tokens=400, recall_max_tokens=1200, context_max_chars=4000,
                        recall_timeout=10, background_timeout=0.1)
        await self.usable()
        await self.select(short)
        if not self.settings.get("parameter_managed"):
            await self.control("delay_background", seconds=0.35)
            try:
                wire = await chat(False, "读取知识页超时")
                assert "【知识页" not in wire
                await configure(background_timeout=0.8)
                await self.usable()
                assert "【知识页" in await chat(True)
                await configure(recall_timeout=0.4, background_timeout=10)
                await self.usable()
                await chat(False, "读取知识页超时")
            finally:
                await self.control("delay_background", seconds=0)
            self.passed("configured_page_subdeadline_allows_slow_read_and_stays_within_shared_quarter_deadline")
        await configure(recall_timeout=10, background_timeout=1)
        await self.usable()
        await page.reload()
        await self.dismiss_host_notice(page)
        await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
        await check_row(await selected_check(), short)
        from parameter_panel import verify_advanced

        await verify_advanced(self, frame)
        await self.layout(page, frame)
        await frame.locator("#tab-setup").click()
        await self.layout(page, frame)
        self.report["acceptance_scope"] = "native panel parameter saves, long-page chat use, reload persistence, index state and read subdeadline"
        self.report["not_covered"] = ["real QQ login", "production model quality", "control migration and business snapshot recovery"]
        self.report["complete"] = True
        self.save()

    async def run(self):
        async with aiohttp.ClientSession() as session, async_playwright() as playwright:
            self.session = session
            login = await self.http("POST", "/api/v1/auth/login", payload={
                "username": "astrbot", "password": self.settings["initial_password"],
            }, auth=False)
            self.token = login["token"]
            login = await self.http("POST", "/api/v1/auth/setup", payload={
                "username": self.settings["admin_name"], "password": self.settings["admin_password"],
                "confirm_password": self.settings["admin_password"],
            })
            self.token = login["token"]
            self.bot = OneBot(session, self.settings)
            await self.bot.connect()
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_init_script("if (window.top === window) localStorage.setItem('token', " + json.dumps(self.token) + ");")
            page = await context.new_page()
            try:
                core = ROOT / "verification/data/cmd_config.json"
                core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
                full = not self.settings.get("feedback_only")
                candidate_upgrade = any(self.settings.get(key) for key in (
                    "single_page", "multi_page", "read_tools", "reflect", "references", "gateway_only", "upgrade_only",
                ))
                upgrading = full or candidate_upgrade
                if upgrading:
                    self.report["not_covered"] = [item for item in self.report["not_covered"] if item != "baseline upgrade"]
                await self.install("baseline-install.zip" if upgrading else "candidate-install.zip")
                await self.config(api_url=self.settings["memory_url"], api_key=self.settings["memory_key"],
                                  service_id=self.settings["owner"], allowed_accounts=[
                                      {"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(user)}
                                      for user in (ALICE, BOB)
                                  ])
                for user in (ALICE, BOB):
                    await self.confirm(user)
                if self.settings.get("gateway_only"):
                    await self.config(recall_timeout=4, background_timeout=1)
                await self.usable()
                await asyncio.sleep(1.1)
                await self.bot.chat(ALICE, "A synthetic baseline conversation.")
                await self.drained()
                scope = await self.scope()
                bank, identifier = scope["bank"], scope["scope_id"]
                short = await self.page(bank, "Short background")
                await self.select(short)
                if self.settings.get("upgrade_only"):
                    from upgrade_e2e import verify

                    await verify(self, page, bank, short)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(complete=True, acceptance_scope="v0.5.0 native migration interruptions, full snapshots and locked-history erasure")
                    self.save()
                    return
                if upgrading:
                    self.record_baseline()
                    await self.install("candidate-upgrade.zip")
                    await self.usable()
                    assert (await self.scope())["bank"] == bank
                await self.action(identifier, "mode", mode="readonly")
                await self.plugin_descriptions(page)
                await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
                await self.dismiss_host_notice(page)
                frame = page.frame_locator("iframe")
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                if self.settings.get("gateway_only"):
                    from gateway_e2e import verify

                    await verify(self, page, frame, bank)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(complete=True, acceptance_scope="v0.5.0 upgrade, native mixed-message capture, tools, quotes, skips, totals and deadlines")
                    self.save()
                    return
                if self.settings.get("references"):
                    from reference_e2e import verify

                    await verify(self, page, frame, bank)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(complete=True, acceptance_scope="native chat references, owner settings, delivery snapshots and rollback")
                    self.save()
                    return
                if self.settings.get("assistant_capture"):
                    from reading_e2e import verify_assistant_capture

                    await verify_assistant_capture(self, frame)
                    self.report.update(complete=True, acceptance_scope="native assistant retention, recall budget, and private skip diagnostics")
                    self.save()
                    return
                if self.settings.get("reflect"):
                    from reflection_e2e import verify

                    await verify(self, page, frame, bank)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(complete=True, acceptance_scope="native Reflect tools, independent settings, erasure barriers and uncertain execution recovery")
                    self.save()
                    return
                if self.settings.get("read_tools"):
                    from reading_e2e import verify

                    await verify(self, page, frame, bank)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(complete=True, acceptance_scope="native Agent read tools, page versions, mode settings, quotas and host history")
                    self.save()
                    return
                if self.settings.get("multi_page"):
                    from multiple_pages import verify, verify_recovery

                    recovery_only = self.settings.get("multi_page_recovery_only")
                    await (verify_recovery(self, page, frame, bank, short) if recovery_only
                           else verify(self, page, frame, bank, short))
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    self.report.update(
                        complete=True,
                        acceptance_scope="native recovery of multiple deleted page references" if recovery_only
                        else "0.2.8 upgrade, native ordered pages, shared budget, lifecycle and erasure",
                    )
                    self.save()
                    return
                if self.settings.get("parameters_only"):
                    from parameter_panel import verify_advanced

                    await verify_advanced(self, frame)
                    self.report["acceptance_scope"] = "native advanced parameter save, tokenizer, query budget, queue admission and exclusions"
                    self.report["complete"] = True
                    self.save()
                    return
                if self.settings.get("single_page"):
                    await self.single_page(page, frame, bank, short)
                    assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                    return
                if self.settings.get("view_only"):
                    await self.switch_bot(frame, scope, short)
                    await self.layout(page, frame)
                    self.report["complete"] = True
                    self.save()
                    return
                if self.settings.get("recovery_only"):
                    before_alias = await self.scope()
                    business = self.runtime.parent
                    def move_business():
                        destination = ROOT / "business-volume"
                        business.rename(destination)
                        business.symlink_to(destination, target_is_directory=True)
                    await self.restart(move_business)
                    after_alias = await self.scope()
                    assert after_alias["bank"] == before_alias["bank"]
                    assert after_alias["background"]["pages"] == before_alias["background"]["pages"]
                    self.passed("native_single_main_directory_alias_preserves_identity_and_bank")

                async def chat_status(expected, used):
                    before = (await self.scope())["background"].get("status", {}).get("checked_at", 0)
                    await self.bot.chat(ALICE, "An unrelated feedback test conversation.")
                    async def checked():
                        state = (await self.scope())["background"].get("status", {})
                        return state if state.get("checked_at", 0) > before else None
                    state = await until(checked, timeout=30)
                    assert state["used"] is used and expected in state["message"], state
                    await frame.locator("#background-status").filter(has_text=expected).wait_for(timeout=15000)
                    await page.wait_for_timeout(5500)
                    assert expected in await frame.locator("#background-status").inner_text()
                    await frame.locator("#scope-picker").select_option(json.dumps(["delivery-onebot", str(BOB)], separators=(",", ":")))
                    assert expected not in await frame.locator("#background-status").inner_text()
                    await frame.locator("#scope-picker").select_option(json.dumps(["delivery-onebot", str(ALICE)], separators=(",", ":")))
                    await frame.locator("#background-status").filter(has_text=expected).wait_for(timeout=15000)

                if not self.settings.get("recovery_only"):
                    await chat_status("上次聊天已使用知识页", True)
                    long = await self.page(bank, "HB-FEEDBACK-LONG")
                    await self.select(long)
                    await chat_status("知识页太长", False)
                    empty, empty_operation = await self.page(bank, "HB-FEEDBACK-EMPTY", wait=False)
                    await self.select(empty)
                    await chat_status("知识页还没有正文", False)
                    await self.completed(bank, empty_operation)
                    await self.select(short)
                    await self.control("delay_background", seconds=2)
                    try:
                        await chat_status("读取知识页超时", False)
                    finally:
                        await self.control("delay_background", seconds=0)
                    await self.native("DELETE", bank + "/knowledge-base/nodes/" + short)
                    await chat_status("找不到所选知识页", False)
                    self.passed("real_page_success_size_empty_missing_and_delayed_reads_remain_visible_after_index_polling_and_user_switch")

                    await self.confirm(ALICE)
                    async def invalidated():
                        return "选择已失效" in await frame.locator("#background-status").inner_text()
                    await until(invalidated, timeout=15)
                    assert (await self.scope())["background"]["invalidated"]
                    assert await frame.locator("#background-page").input_value() == ""
                    await self.select(long)
                    assert not (await self.scope())["background"].get("invalidated")
                    self.passed("identity_reconfirmation_invalidates_reference_and_explicit_reselection_clears_notice")

                from recovery_e2e import verify as verify_recovery

                await verify_recovery(self)
                await page.reload()
                await self.dismiss_host_notice(page)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await frame.locator("#background-status").filter(has_text="选择已失效").wait_for(timeout=15000)
                if not self.settings.get("recovery_only"):
                    await self.layout(page, frame)
                assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                self.report["complete"] = True
                self.save()
            finally:
                await self.bot.close()
                await context.close()
                await browser.close()


async def main():
    acceptance = Feedback()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(complete=False, failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
