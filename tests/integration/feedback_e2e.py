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
        return next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(user) and not row["historical"])

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

    async def page(self, bank, query):
        value = await self.native("POST", bank + "/knowledge-base/pages", {
            "name": query, "source_query": query, "max_tokens": 8192,
            "trigger": {"refresh_after_consolidation": False, "fact_types": ["world"]},
        })
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
        await self.usable()

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
                await self.install("baseline-install.zip")
                legacy = ROOT / "verification/legacy-control"
                await self.config(api_url=self.settings["memory_url"], api_key=self.settings["memory_key"],
                                  service_id=self.settings["owner"], control_state_dir=str(legacy), allowed_accounts=[
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
                settings = database(legacy / "control.sqlite3", "SELECT key,value FROM settings ORDER BY key")
                mode = legacy.stat().st_mode
                await self.install("candidate-upgrade.zip")
                rejected = await self.bootstrap()
                assert "control_directory_outside_plugin" in rejected["status"]["label"], rejected["status"]
                assert database(legacy / "control.sqlite3", "SELECT key,value FROM settings ORDER BY key") == settings
                assert legacy.stat().st_mode == mode
                target = self.runtime.parent.parent / (NAME + "_control")
                assert not target.exists()
                self.passed("native_026_upgrade_rejects_external_path_without_replacement_journal_or_external_mutation")

                config_file = ROOT / "verification/data/config" / (NAME + "_config.json")
                def migrate():
                    shutil.copytree(legacy, target)
                    value = json.loads(config_file.read_text())
                    value["control_state_dir"] = ""
                    config_file.write_text(json.dumps(value, indent=2))
                await self.restart(migrate)
                migrated = await self.scope()
                assert migrated["bank"] == bank and migrated["mode"] == scope["mode"]
                assert migrated["background"]["page_id"] == short and migrated["background"]["enabled"]
                latest = dict((row["key"], row["value"]) for row in database(target / "control.sqlite3", "SELECT key,value FROM settings"))
                original = dict((row["key"], row["value"]) for row in settings)
                assert all(latest[key] == original[key] for key in ("deployment_id", "control_id", "revision"))
                self.passed("stopped_full_control_directory_copy_and_default_path_preserve_identity_revision_bank_mode_selection")
                await self.action(identifier, "mode", mode="readonly")
                await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
                await self.dismiss_host_notice(page)
                frame = page.frame_locator("iframe")
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)

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

                await chat_status("上次聊天已使用知识页", True)
                long = await self.page(bank, "HB-FEEDBACK-LONG")
                await self.select(long)
                await chat_status("知识页太长", False)
                empty = await self.page(bank, "HB-FEEDBACK-EMPTY")
                await self.select(empty)
                await chat_status("知识页还没有正文", False)
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
                assert all(row["state"] == "discarded" for row in database(self.runtime, "SELECT state FROM outbox WHERE bank=?", (bank,)))
                await page.reload()
                await self.dismiss_host_notice(page)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await until(invalidated, timeout=15)
                self.passed("forget_then_old_business_restore_preserves_latest_migrated_journal_and_cannot_revive_bank_or_page")
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.frames[-1].evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                await page.screenshot(path=str(ROOT / "feedback-phone.png"), full_page=True)
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
