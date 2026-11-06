"""Focused real-host checks for active model edits and connection ownership."""

import asyncio
import json

import aiohttp
from playwright.async_api import async_playwright

from live_e2e import Acceptance, ALICE, NAME, OneBot, ROOT, until
from managed_e2e import private_command


class Connection(Acceptance):
    async def connect_models(self, frame, installation_id, *, retry_probe=True):
        target = {"mode": "managed", "installation_id": installation_id}
        old = await self.http("POST", self.prefix + "/managed/status", payload=target)
        await frame.locator("#save-models").click()

        async def completed():
            job = await self.http("POST", self.prefix + "/managed/status", payload=target)
            if job.get("request_id") == old.get("request_id"):
                return None
            if job.get("status") == "failed":
                raise RuntimeError(job.get("message", "Managed connection failed"))
            return job if job.get("status") == "succeeded" else None

        try:
            await until(completed, timeout=360)
        except RuntimeError as error:
            if not retry_probe or "连接检查超时" not in str(error):
                raise
            self.report.setdefault("transient_model_probe_timeouts", []).append(str(error))
            await until(lambda: frame.locator("#save-models").is_enabled(), timeout=30)
            return await self.connect_models(frame, installation_id, retry_probe=False)
        await until(lambda: frame.locator("#save-models").is_enabled(), timeout=30)
        await self.usable()

    async def managed(self, page, frame):
        await frame.locator("#prepare").click()
        await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
        private_command("managed_install", await frame.locator("#install-command").input_value())
        await self.control("managed_install")
        self.settings = json.loads((ROOT / "live-settings.json").read_text())
        self.report["hindsight_image_id"] = self.settings["hindsight_image_id"]
        await frame.locator('[data-close="deployment-dialog"]').click()
        installation_id = (await self.bootstrap())["installation_id"]
        for kind in ("chat", "embedding", "rerank"):
            await frame.locator("#" + kind + "-provider").select_option("delivery-" + kind)
        await self.connect_models(frame, installation_id)
        await frame.locator("#tab-memories").click()
        await frame.locator("#add-account").click()
        await frame.locator("#account-platform").select_option("delivery-onebot")
        await frame.locator("#account-user").fill(str(ALICE))
        await frame.locator("#save-account").click()
        await frame.locator("#authorization-dialog").wait_for(state="visible", timeout=30000)
        await frame.locator("#confirm-authorization").click()
        await self.usable()
        mode_only = self.settings.get("connection_check_step") == "mode"
        if not mode_only:
            await asyncio.sleep(1.1)
            await self.bot.chat(ALICE, "An isolated model-edit fixture. Reply briefly.")
            await self.drained()
        before = await self.bootstrap()
        original = next(row for row in before["scopes"] if row["user_id"] == str(ALICE))
        service_id = (await self.config())["service_id"]

        async def same_scope():
            state = await self.usable()
            scope = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
            assert scope["bank"] == original["bank"]
            assert scope["summary"]["saved_count"] == original["summary"]["saved_count"]
            assert (await self.config())["service_id"] == service_id
            return state

        await frame.locator("#tab-setup").click()
        edited = before
        if not mode_only:
            await frame.locator("#rerank-provider").select_option("")
            await self.connect_models(frame, installation_id)
            edited = await same_scope()
            assert edited["managed_service"]["configuration_fingerprint"] != before["managed_service"]["configuration_fingerprint"]
            self.passed("active_model_edit_changes_actual_configuration_without_changing_service_bank_or_saved_count")

        await page.screenshot(path=str(ROOT / "mode-managed.png"), full_page=True)
        await frame.locator('[name="connection-mode"][value="existing"]').check()
        await frame.locator("#save-settings").click()
        await until(lambda: frame.locator("#save-settings").is_enabled(), timeout=30)
        existing = await same_scope()
        assert not existing["config"]["managed"] and not existing["backend"]["managed"]
        assert existing["providers"] == {} and existing["managed_service"] is None
        assert await frame.locator("#models-settings").is_hidden()
        assert await frame.locator("#maintenance").is_hidden()
        await page.screenshot(path=str(ROOT / "mode-existing.png"), full_page=True)

        await frame.locator('[name="connection-mode"][value="managed"]').check()
        await until(lambda: frame.locator("#chat-provider").input_value(), timeout=15)
        await self.connect_models(frame, installation_id)
        restored = await same_scope()
        assert restored["config"]["managed"] and restored["backend"]["managed"]
        assert restored["managed_service"]["configuration_fingerprint"] == edited["managed_service"]["configuration_fingerprint"]
        self.passed("same_address_existing_mode_revokes_management_and_explicit_managed_reconnect_restores_it")

    async def existing(self, frame):
        await frame.locator('[name="connection-mode"][value="existing"]').check()
        await frame.locator("#api-url").fill(self.settings["memory_url"])
        await frame.locator("#api-key").fill(self.settings["memory_key"])
        await frame.locator("#save-settings").click()
        await until(lambda: frame.locator("#save-settings").is_enabled(), timeout=30)
        state = await self.usable()
        assert not state["config"]["managed"] and not state["backend"]["managed"]
        await frame.locator("#service-version-check").click()
        await until(lambda: frame.locator("#service-version-check").is_enabled(), timeout=15)
        message = await frame.locator("#version-check-result").inner_text()
        assert "检查完成" in message and self.settings["hindsight_version"] in message
        assert "升级" not in message and "配套版本" not in message
        assert await frame.locator("#models-settings").is_hidden()
        assert await frame.locator("#maintenance").is_hidden()
        self.passed("existing_service_connection_and_read_only_version_feedback")

    async def run(self):
        managed = self.settings.get("connection_check_mode") == "managed"
        self.report["acceptance_scope"] = ("explicit mode switching" if self.settings.get("connection_check_step") == "mode"
                                           else "active model edits and explicit mode switching") if managed else "existing-service connection and read-only version feedback"
        self.report["not_covered"] = ["full installation, chat, upgrade and recovery suites"]
        async with aiohttp.ClientSession() as session, async_playwright() as playwright:
            self.session = session
            self.bot = OneBot(session, self.settings)
            login = await self.http("POST", "/api/v1/auth/login", payload={
                "username": "astrbot", "password": self.settings["initial_password"],
            }, auth=False)
            self.token = login["token"]
            login = await self.http("POST", "/api/v1/auth/setup", payload={
                "username": self.settings["admin_name"], "password": self.settings["admin_password"],
                "confirm_password": self.settings["admin_password"],
            })
            self.token = login["token"]
            if managed:
                await self.bot.connect()
            await self.install("candidate-install.zip")
            if managed:
                state_path = self.runtime.parent / "setup.json"
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps({"control_plane_port": self.settings["control_plane_port"]}))
                state_path.chmod(0o600)
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_init_script("if (window.top === window) localStorage.setItem('token', " + json.dumps(self.token) + ");")
            page = await context.new_page()
            try:
                await page.goto(self.dashboard + "/#/plugin-view/" + NAME + "/memory")
                await self.dismiss_host_notice(page)
                frame = page.frame_locator("iframe")
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await frame.locator("#tab-setup").click()
                if managed:
                    await self.managed(page, frame)
                else:
                    await self.existing(frame)
                self.report["complete"] = True
                self.save()
            finally:
                await self.bot.close()
                await context.close()
                await browser.close()


async def main():
    acceptance = Connection()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
