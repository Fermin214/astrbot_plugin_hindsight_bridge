"""Install actual managed resources, then exercise the shared native parameter flow."""

import asyncio
import hashlib
import json

import aiohttp
from playwright.async_api import async_playwright

from feedback_e2e import Feedback
from live_e2e import ALICE, NAME, OneBot, ROOT, until
from managed_e2e import private_command
from connection_e2e import Connection


class ManagedParameters(Feedback):
    connect_models = Connection.connect_models

    async def run(self):
        async with aiohttp.ClientSession() as session, async_playwright() as playwright:
            self.session, self.bot = session, OneBot(session, self.settings)
            login = await self.http("POST", "/api/v1/auth/login", payload={
                "username": "astrbot", "password": self.settings["initial_password"],
            }, auth=False)
            self.token = login["token"]
            login = await self.http("POST", "/api/v1/auth/setup", payload={
                "username": self.settings["admin_name"], "password": self.settings["admin_password"],
                "confirm_password": self.settings["admin_password"],
            })
            self.token = login["token"]
            await self.bot.connect()
            await self.install("candidate-install.zip")
            state_path = self.runtime.parent / "setup.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"control_plane_port": self.settings["control_plane_port"]}))
            state_path.chmod(0o600)
            core = ROOT / "verification/data/cmd_config.json"
            original = hashlib.sha256(core.read_bytes()).hexdigest()
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
                await frame.locator("#prepare").click()
                await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
                private_command("managed_install", await frame.locator("#install-command").input_value())
                await self.control("managed_install")
                self.settings = json.loads((ROOT / "live-settings.json").read_text())
                self.report["hindsight_image_id"] = self.settings["hindsight_image_id"]
                await frame.locator('[data-close="deployment-dialog"]').click()
                await frame.locator("#chat-provider").select_option("delivery-chat")
                await frame.locator("#embedding-provider").select_option("delivery-embedding")
                await self.connect_models(frame, (await self.bootstrap())["installation_id"], retry_probe=False)
                assert (await self.bootstrap())["config"]["managed"]
                await frame.locator("#tab-memories").click()
                await frame.locator("#add-account").click()
                await frame.locator("#account-platform").select_option("delivery-onebot")
                await frame.locator("#account-user").fill(str(ALICE))
                await frame.locator("#save-account").click()
                await frame.locator("#authorization-dialog").wait_for(state="visible", timeout=30000)
                await frame.locator("#confirm-authorization").click()
                await self.usable()
                await asyncio.sleep(1.1)
                await self.bot.chat(ALICE, "A synthetic managed baseline conversation.")
                await self.drained()
                bank = (await self.scope())["bank"]
                short = await self.page(bank, "Short background")
                await self.select(short)
                await page.reload()
                await self.dismiss_host_notice(page)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await self.single_page(page, frame, bank, short)
                self.report["not_covered"].append("injected page latency on the direct managed API")
                self.save()
                assert hashlib.sha256(core.read_bytes()).hexdigest() == original
            finally:
                await self.bot.close()
                await context.close()
                await browser.close()


async def main():
    acceptance = ManagedParameters()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(complete=False, failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
