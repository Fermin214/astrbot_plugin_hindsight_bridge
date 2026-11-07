"""Native Dashboard, upgrade, control migration and page feedback acceptance."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import aiohttp
from playwright.async_api import async_playwright

from live_e2e import Acceptance, ALICE, BOB, NAME, OneBot, ROOT, database, until


class Feedback(Acceptance):
    def __init__(self):
        super().__init__()
        self.report.update(
            acceptance_scope="native Dashboard feedback, 0.2.6 upgrade, control migration and snapshot recovery",
            chat_model="synthetic OpenAI fixture", embedding_model="synthetic 64-dimension fixture", rerank_model="rrf",
            not_covered=["real QQ login", "production model quality"],
        )
        if self.settings.get("feedback_only"):
            self.report["acceptance_scope"] = "native Dashboard page feedback, identity invalidation and snapshot recovery"
            self.report["not_covered"].append("0.2.6 upgrade and external control migration")
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

    async def switch_bot(self, frame, old_scope, old_page):
        old_bot = self.bot.bot_id
        await self.bot.close()
        self.bot = OneBot(self.session, {**self.settings, "bot_id": old_bot + 1})
        await self.bot.connect()
        await self.bot.chat(ALICE, "/hindsight status")
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
        retained = await self.native("GET", old_scope["bank"] + "/knowledge-base/pages/" + old_page)
        assert retained["id"] == old_page
        self.report["measurements"]["bot_switch"] = {
            "old_bot": old_bot, "new_bot": self.bot.bot_id, "old_bank": old_scope["bank"],
            "new_bank": current["bank"], "selection_revision": selection["revision"], "old_page_retained": True,
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

    async def confirm(self, user):
        state = await self.bootstrap()
        prepared = await self.http("POST", self.prefix + "/accounts/authorize", payload={
            "action": "prepare", "platform_instance_id": "delivery-onebot", "user_id": str(user),
            "config_revision": state["config_revision"],
        })
        await self.http("POST", self.prefix + "/accounts/authorize", payload={
            "action": "confirm", "platform_instance_id": "delivery-onebot", "user_id": str(user),
            "config_revision": state["config_revision"], "confirmation": prepared["confirmation"],
        })

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
            "page_id": page_id, "enabled": True, "revision": value["selection"]["revision"],
            "scope_revision": value["scope_revision"], "config_revision": value["config_revision"],
        })

    async def restart(self, mutation):
        await self.bot.close()
        await self.control("stop_astrbot")
        mutation()
        await self.control("start_astrbot")
        self.bot = OneBot(self.session, self.settings)
        await self.bot.connect()
        await self.bot.chat(ALICE, "/hindsight status")
        await self.usable()

    async def single_page(self, page, frame, bank, short):
        from parameter_panel import save_parameters

        async def configure(**updates):
            return await save_parameters(self, frame, **updates)

        async def selected_check():
            scope = await self.scope()
            return await self.http("GET", self.prefix + "/scopes/" + scope["scope_id"] + "/background")

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
        await frame.locator("#background-check").filter(has_text="知识页太长").wait_for(timeout=15000)
        await frame.locator("#background-index").filter(has_text="目录标记").wait_for()
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
        await frame.locator("#background-check").filter(has_text="长度检查通过").wait_for(timeout=15000)
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
                assert "Hindsight 知识页" not in wire
                await configure(background_timeout=0.8)
                await self.usable()
                assert "Hindsight 知识页" in await chat(True)
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
        await frame.locator("#background-check").filter(has_text="长度检查通过").wait_for(timeout=15000)
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
                await self.install("baseline-install.zip" if full or self.settings.get("single_page") else "candidate-install.zip")
                legacy = ROOT / "verification/legacy-control"
                await self.config(api_url=self.settings["memory_url"], api_key=self.settings["memory_key"],
                                  service_id=self.settings["owner"], control_state_dir=str(legacy) if full else "", allowed_accounts=[
                                      {"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(user)}
                                      for user in (ALICE, BOB)
                                  ])
                for user in (ALICE, BOB):
                    await self.confirm(user)
                await self.usable()
                await asyncio.sleep(1.1)
                await self.bot.chat(ALICE, "A synthetic baseline conversation.")
                await self.drained()
                scope = await self.scope()
                bank, identifier = scope["bank"], scope["scope_id"]
                short = await self.page(bank, "Short background")
                await self.select(short)
                if self.settings.get("single_page"):
                    await self.install("candidate-upgrade.zip")
                    await self.usable()
                    assert (await self.scope())["bank"] == bank
                if full:
                    settings = database(legacy / "control.sqlite3", "SELECT key,value FROM settings ORDER BY key")
                    mode = legacy.stat().st_mode
                    await self.install("candidate-upgrade.zip")
                    rejected = await self.bootstrap()
                    assert "control_directory_outside_plugin" in rejected["status"]["label"], rejected["status"]
                    assert database(legacy / "control.sqlite3", "SELECT key,value FROM settings ORDER BY key") == settings
                    assert legacy.stat().st_mode == mode
                    target = ROOT / "control-volume"
                    assert not (target / "control.sqlite3").exists()
                    self.passed("native_026_upgrade_rejects_external_path_without_replacement_journal_or_external_mutation")

                    config_file = ROOT / "verification/data/config" / (NAME + "_config.json")
                    def migrate():
                        assert not list(target.iterdir())
                        shutil.copytree(legacy, target, dirs_exist_ok=True)
                        value = json.loads(config_file.read_text(encoding="utf-8-sig"))
                        value["control_state_dir"] = ""
                        config_file.write_text(json.dumps(value, indent=2))
                    await self.restart(migrate)
                    migrated = await self.scope()
                    assert migrated["bank"] == bank and migrated["mode"] == scope["mode"]
                    assert migrated["background"]["page_id"] == short and migrated["background"]["enabled"]
                    latest = dict((row["key"], row["value"]) for row in database(target / "control.sqlite3", "SELECT key,value FROM settings"))
                    original = dict((row["key"], row["value"]) for row in settings)
                    assert all(latest[key] == original[key] for key in ("deployment_id", "control_id", "revision"))
                    self.passed("stopped_full_control_copy_to_dedicated_mount_preserves_identity_revision_bank_mode_selection")
                await self.action(identifier, "mode", mode="readonly")
                await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
                await self.dismiss_host_notice(page)
                frame = page.frame_locator("iframe")
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
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
                    assert after_alias["background"]["page_id"] == before_alias["background"]["page_id"]
                    self.passed("native_business_directory_alias_preserves_default_control_allocation_and_bank")

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

                snapshot = ROOT / "pre-forget.sqlite3"
                with sqlite3.connect(self.runtime) as source, sqlite3.connect(snapshot) as destination:
                    source.backup(destination)
                prepared = await self.action(identifier, "forget_prepare")
                await self.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
                async def erased():
                    value = await self.scope()
                    return value if value["bank"] != bank and not value["summary"].get("cleanup") else None
                latest = await until(erased, timeout=90)
                assert latest["background"]["invalidated"] and not latest["background"]["page_id"]
                def restore_business():
                    self.runtime.unlink()
                    self.runtime.with_name("runtime.sqlite3-wal").unlink(missing_ok=True)
                    self.runtime.with_name("runtime.sqlite3-shm").unlink(missing_ok=True)
                    shutil.copyfile(snapshot, self.runtime)
                await self.restart(restore_business)
                restored = await self.scope()
                assert restored["bank"] == latest["bank"] and restored["generation"] == latest["generation"]
                assert restored["background"]["invalidated"] and not restored["background"]["page_id"]
                assert not (await self.memories(bank))["total"]
                for absent_bank in (bank, restored["bank"]):
                    async with self.session.get(
                        self.settings["hindsight_url"] + "/v1/default/banks/" + absent_bank + "/config",
                        headers={"Authorization": "Bearer " + self.settings["memory_key"]},
                    ) as response:
                        assert response.status == 404, (absent_bank, response.status)
                self.report["measurements"]["bank_config_after_restore"] = {"retired": 404, "new": 404}
                assert all(row["state"] == "discarded" for row in database(self.runtime, "SELECT state FROM outbox WHERE bank=?", (bank,)))
                await page.reload()
                await self.dismiss_host_notice(page)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await frame.locator("#background-status").filter(has_text="选择已失效").wait_for(timeout=15000)
                self.passed("forget_then_old_business_restore_preserves_latest_migrated_journal_and_cannot_revive_bank_or_page")
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
        acceptance.report.update(failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
