"""Real browser wizard -> generated host command -> managed Hindsight -> chat."""

import asyncio
import hashlib
import json
import os

import aiohttp
from playwright.async_api import async_playwright

from live_e2e import Acceptance, ALICE, NAME, OneBot, ROOT, until


def private_command(name, command):
    path = ROOT / (name + "-command.txt")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(command)
    os.chown(path, ROOT.stat().st_uid, ROOT.stat().st_gid)


class Managed(Acceptance):
    async def run(self):
        self.report.update(acceptance_scope="actual managed-service browser installation, reinstallation and full uninstall",
                           not_covered=["real QQ account and platform delivery"])
        async with aiohttp.ClientSession() as session, async_playwright() as playwright:
            self.session = session
            self.bot = OneBot(session, self.settings)
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            errors = []
            try:
                login = await self.http("POST", "/api/v1/auth/login", payload={"username": "astrbot", "password": self.settings["initial_password"]}, auth=False)
                self.token = login["token"]
                login = await self.http("POST", "/api/v1/auth/setup", payload={"username": self.settings["admin_name"], "password": self.settings["admin_password"],
                                                                                "confirm_password": self.settings["admin_password"]})
                self.token = login["token"]
                await self.bot.connect()
                core = ROOT / "verification/data/cmd_config.json"
                core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
                await self.install("candidate-install.zip")
                directory = self.runtime.parent
                directory.mkdir(parents=True, exist_ok=True)
                # Use a free CP host port, leaving all installer code/materials unmodified.
                (directory / "setup.json").write_text(json.dumps({"control_plane_port": self.settings["control_plane_port"]}))
                (directory / "setup.json").chmod(0o600)
                await context.add_init_script("if (window.top === window) { localStorage.setItem('token', " + json.dumps(self.token) + "); localStorage.setItem('user','delivery-admin'); }")
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
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
                self.passed("actual_browser_generated_unmodified_installer_starts_owned_compose_resources")
                await frame.locator("#chat-provider").select_option("delivery-chat")
                await frame.locator("#embedding-provider").select_option("delivery-embedding")
                await frame.locator("#rerank-provider").select_option("delivery-rerank")
                await frame.locator("#save-models").click()
                async def models_ready():
                    status = await self.http("GET", self.prefix + "/models/status")
                    if status.get("status") in {"failed", "interrupted"}:
                        raise RuntimeError("Actual wizard model validation failed: " + status.get("message", ""))
                    return status if status.get("status") == "succeeded" else None
                await until(models_ready, timeout=300)
                await until(lambda: frame.locator("#save-models").is_enabled(), timeout=20)
                if not await frame.locator(".account-user").count():
                    await frame.locator("#add-account").click()
                await frame.locator(".account-platform").last.select_option("delivery-onebot")
                await frame.locator(".account-user").last.fill(str(ALICE))
                await frame.locator("#save-settings").click()
                await self.usable()
                configuration = await self.config()
                assert configuration["api_url"].startswith("http://memory-") and configuration["service_id"]
                assert configuration["recall_timeout"] == 10
                assert all(account["__template_key"] == "account" for account in configuration["allowed_accounts"])
                self.passed("actual_wizard_native_provider_validation_real_models_startup_and_connection_save")

                await self.bot.chat(ALICE, "Remember: I prefer oolong tea and my managed-install notebook code is Hazel-63. Reply briefly.")
                await self.drained()
                scope = (await self.bootstrap())["scopes"][0]
                assert "Hazel-63" in json.dumps(await self.memories(scope["bank"]))
                await self.bot.chat(ALICE, "/new")
                answer = await self.bot.chat(ALICE, "Which tea do I prefer and what is my notebook code? Answer briefly.")
                assert "oolong" in answer.lower() and "Hazel-63" in answer
                await self.drained()
                self.passed("managed_service_real_embedding_rerank_retain_new_conversation_recall_and_reply")
                await page.screenshot(path=str(ROOT / "managed-wizard.png"), full_page=True)
                await frame.locator("#tab-setup").click()
                await frame.locator("#reinstall").click()
                await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
                private_command("managed_reinstall", await frame.locator("#install-command").input_value())
                await self.control("managed_reinstall")
                await self.backend_ready()
                await self.usable()
                assert (await self.bootstrap())["scopes"][0]["bank"] == scope["bank"]
                assert "Hazel-63" in json.dumps(await self.memories(scope["bank"]))
                assert configuration == await self.config()
                self.passed("unmodified_redeployment_preserves_model_settings_bank_credentials_and_memories")

                cp = await context.new_page()
                cp_url = f'http://127.0.0.1:{self.settings["control_plane_port"]}'
                rejected = await context.request.post(cp_url + "/api/auth/login", data={"key": "invalid-owned-test-key"})
                assert rejected.status == 401
                env = ROOT / "managed-installations" / self.settings["managed_project"] / ".env"
                cp_key = next(line.partition("=")[2].strip().strip("'\"") for line in env.read_text().splitlines()
                              if line.startswith("HINDSIGHT_CP_ACCESS_KEY="))
                authenticated = await context.request.post(cp_url + "/api/auth/login", data={"key": cp_key})
                assert authenticated.status == 200
                banks = await context.request.get(cp_url + "/api/banks")
                assert banks.status == 200 and scope["bank"] in await banks.text()
                await cp.goto(cp_url, wait_until="networkidle", timeout=60000)
                assert await cp.locator("body").inner_text()
                (ROOT / "control-plane-visible-text.txt").write_text(await cp.locator("body").inner_text())
                await cp.screenshot(path=str(ROOT / "official-control-plane.png"), full_page=True)
                self.passed("managed_official_control_plane_authentication_bank_listing_and_real_browser_page")
                await cp.close()
                await frame.locator("#delete-memory-data").check()
                await frame.locator("#prepare-uninstall").click()
                await until(lambda: frame.locator("#uninstall-command").input_value(), timeout=20)
                private_command("managed_uninstall", await frame.locator("#uninstall-command").input_value())
                await self.control("managed_uninstall")
                assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                assert not errors, "Wizard browser JavaScript errors"
                self.passed("actual_full_uninstall_removes_only_owned_service_data_files_and_networks")
                await self.http("DELETE", "/api/v1/plugins/by-id?plugin_id=" + NAME)
                self.passed("fresh_astrbot_and_external_network_survive_without_global_configuration_changes")
                self.report["complete"] = True
                self.save()
            finally:
                await self.bot.close()
                await context.close()
                await browser.close()


async def main():
    acceptance = Managed()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
