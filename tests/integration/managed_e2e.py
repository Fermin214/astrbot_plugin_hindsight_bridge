"""Real browser wizard -> generated host command -> managed Hindsight -> chat."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import aiohttp
from playwright.async_api import async_playwright

from live_e2e import Acceptance, ALICE, NAME, OneBot, ROOT, TIMEOUT, until


def private_command(name, command):
    path = ROOT / (name + "-command.txt")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(command)
    os.chown(path, ROOT.stat().st_uid, ROOT.stat().st_gid)


class Managed(Acceptance):
    async def recall_notebook(self, question, phase, *, tea=False):
        for attempt in range(2):
            await self.bot.chat(ALICE, "/new")
            answer = await self.bot.chat(ALICE, question)
            matches = "Hazel-63" in answer and (
                not tea or "oolong" in answer.lower() or "\u4e4c\u9f99" in answer
            )
            if matches:
                return
            state = await self.bootstrap()
            recall = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))["summary"]["recall"]
            if (
                attempt or TIMEOUT not in self.latest_wire() or not recall
                or recall["outcome"] != "error" or "timeout" not in recall["code"]
            ):
                raise AssertionError("Notebook recall failed: " + phase)
            self.report.setdefault("transient_recall_timeouts", []).append({
                "phase": phase, "code": recall["code"], "elapsed_ms": recall["elapsed_ms"],
                "recall_timeout": (await self.config())["recall_timeout"], "recovery_attempts": 1,
            })
            self.save()

    async def run(self):
        self.report.update(acceptance_scope="actual managed-service browser installation, reinstallation and full uninstall",
                           not_covered=["real QQ account and platform delivery"])
        async with aiohttp.ClientSession() as session, async_playwright() as playwright:
            self.session = session
            self.bot = OneBot(session, self.settings)
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            errors = []
            page = None
            try:
                login = await self.http("POST", "/api/v1/auth/login", payload={"username": "astrbot", "password": self.settings["initial_password"]}, auth=False)
                self.token = login["token"]
                login = await self.http("POST", "/api/v1/auth/setup", payload={"username": self.settings["admin_name"], "password": self.settings["admin_password"],
                                                                                "confirm_password": self.settings["admin_password"]})
                self.token = login["token"]
                await self.bot.connect()
                core = ROOT / "verification/data/cmd_config.json"
                core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
                await self.install("baseline-install.zip" if self.settings.get("upgrade_from_1_1") else "candidate-install.zip")
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
                await frame.locator("#tab-setup").wait_for(state="visible", timeout=60000)
                await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
                await frame.locator("#tab-setup").click()
                if not self.settings.get("upgrade_from_1_1"):
                    await frame.locator("#chat-provider").select_option("delivery-chat")
                    await frame.locator("#embedding-provider").select_option("delivery-embedding")
                    await frame.locator("#save-models").click()
                    await frame.locator("#feedback.error").filter(has_text="请先生成并执行").wait_for()
                    assert await frame.locator("#prepare").is_enabled()
                    assert await frame.locator("#save-models").is_enabled()
                    assert (await self.http("GET", self.prefix + "/bootstrap"))["model_job"] is None
                    self.passed("saving_models_before_installation_shows_install_step_and_keeps_setup_available")
                await frame.locator("#prepare").click()
                await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
                private_command("managed_install", await frame.locator("#install-command").input_value())
                await self.control("managed_install")
                self.settings = json.loads((ROOT / "live-settings.json").read_text())
                self.report.update(hindsight=self.settings["hindsight_version"], hindsight_image_id=self.settings["hindsight_image_id"])
                self.passed("actual_browser_generated_unmodified_installer_starts_owned_compose_resources")
                if not self.settings.get("upgrade_from_1_1"):
                    await frame.locator('[data-close="deployment-dialog"]').click()
                await frame.locator("#chat-provider").select_option("delivery-chat")
                await frame.locator("#embedding-provider").select_option("delivery-embedding")
                await frame.locator("#rerank-provider").select_option("delivery-rerank")
                installation_id = (await self.bootstrap()).get("installation_id")
                async def models_ready():
                    if self.settings.get("upgrade_from_1_1"):
                        status = await self.http("GET", self.prefix + "/models/status")
                    else:
                        status = await self.http("POST", self.prefix + "/managed/status", payload={
                            "mode": "managed", "installation_id": installation_id,
                        })
                    if status.get("status") in {"failed", "interrupted"}:
                        raise RuntimeError("Actual wizard model validation failed: " + status.get("message", ""))
                    return status if status.get("status") == "succeeded" else None
                for attempt in range(3):
                    await frame.locator("#save-models").click()
                    try:
                        await until(models_ready, timeout=300)
                        break
                    except RuntimeError as error:
                        if "连接检查超时" not in str(error) or attempt == 2:
                            raise
                        self.report.setdefault("transient_model_probe_failures", []).append(str(error))
                        await until(lambda: frame.locator("#save-models").is_enabled(), timeout=30)
                        await asyncio.sleep(3)
                await until(lambda: frame.locator("#save-models").is_enabled(), timeout=20)
                if self.settings.get("upgrade_from_1_1"):
                    if not await frame.locator(".account-user").count():
                        await frame.locator("#add-account").click()
                    await frame.locator(".account-platform").last.select_option("delivery-onebot")
                    await frame.locator(".account-user").last.fill(str(ALICE))
                    await frame.locator("#save-settings").click()
                    async def account_saved():
                        return any(row["platform_instance_id"] == "delivery-onebot" and row["user_id"] == str(ALICE)
                                   for row in (await self.config())["allowed_accounts"])
                    await until(account_saved, timeout=30)
                    await frame.locator('#tab-memories[aria-selected="true"]').wait_for()
                    await frame.locator("#tab-setup").click()
                    await frame.get_by_role("button", name="确认账号授权", exact=True).click()
                    await frame.locator("#authorization-dialog").wait_for(state="visible")
                    await frame.locator("#confirm-authorization").click()
                else:
                    assert (await models_ready()).get("stage") == "complete"
                    assert (await self.config())["api_url"].startswith("http://memory-")
                    assert await frame.locator("#save-settings").is_hidden()
                    self.passed("single_model_save_completes_and_persists_managed_plugin_connection")
                    await frame.locator("#tab-memories").click()
                    await frame.locator("#add-account").click()
                    await frame.locator("#account-platform").select_option("delivery-onebot")
                    await frame.locator("#account-user").fill(str(ALICE))
                    await frame.locator("#save-account").click()
                    await frame.locator("#authorization-dialog").wait_for(state="visible", timeout=30000)
                    await frame.locator("#confirm-authorization").click()
                await self.usable()
                await self.backend_ready()
                configuration = await self.config()
                assert configuration["api_url"].startswith("http://memory-") and configuration["service_id"]
                assert configuration["recall_timeout"] == 10
                assert all(account["__template_key"] == "account" for account in configuration["allowed_accounts"])
                self.passed("actual_wizard_native_provider_validation_real_models_startup_and_connection_save")

                # OneBot seconds cannot order a message inside the authorization second.
                await asyncio.sleep(1.1)
                await self.bot.chat(ALICE, "Remember: I prefer oolong tea and my managed-install notebook code is Hazel-63. Reply briefly.")
                await self.drained()
                scope = next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(ALICE))
                assert scope["summary"]["saved_count"] > 0
                assert "Hazel-63" in json.dumps(await self.memories(scope["bank"]))
                await self.recall_notebook(
                    "Which tea do I prefer and what is my notebook code? Answer briefly.",
                    "before_upgrade", tea=True,
                )
                await self.drained()
                self.passed("managed_service_real_embedding_rerank_retain_new_conversation_recall_and_reply")
                if self.settings.get("verify_panel") and not self.settings.get("upgrade_from_1_1"):
                    from panel_e2e import verify_panel
                    await verify_panel(self, page, context, frame, scope)
                if self.settings.get("upgrade_from_1_1"):
                    await self.install("candidate-upgrade.zip")
                    await page.reload()
                    await frame.locator("#startup").wait_for(state="hidden", timeout=60000)
                    await self.usable()
                    assert (await self.http("GET", self.prefix + "/service/status"))["version"] == "1.1.0"
                    bob_id, carol_id = "123099", "123098"
                    await self.config(allowed_accounts=(await self.config())["allowed_accounts"] + [
                        {"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": user_id}
                        for user_id in (bob_id, carol_id)
                    ])
                    for user_id in (bob_id, carol_id):
                        authorization = {"action": "prepare", "platform_instance_id": "delivery-onebot", "user_id": user_id,
                                         "config_revision": (await self.bootstrap())["config_revision"]}
                        prepared = await self.http("POST", self.prefix + "/accounts/authorize", payload=authorization)
                        await self.http("POST", self.prefix + "/accounts/authorize", payload={
                            **authorization, "action": "confirm", "confirmation": prepared["confirmation"],
                        })
                    authorized = (await self.usable())["scopes"]
                    bob = next(row for row in authorized if row["user_id"] == bob_id)
                    carol = next(row for row in authorized if row["user_id"] == carol_id)
                    await self.action(bob["scope_id"], "mode", mode="readonly")
                    await self.action(bob["scope_id"], "maintenance_begin")
                    configuration = await self.config()
                    await page.reload()
                    await self.dismiss_host_notice(page)
                    await frame.locator("#startup").wait_for(state="hidden", timeout=60000)
                    await frame.locator("#tab-setup").click()
                    await frame.locator("#go-upgrade").click()
                    async def maintenance_ready():
                        for row in (await self.bootstrap())["scopes"]:
                            if row.get("pending"):
                                continue
                            value = await self.http("GET", self.prefix + "/scopes/" + row["scope_id"] + "/action?action=maintenance_status")
                            if not value["maintenance"]["ready"]:
                                return False
                        return True
                    await frame.locator("#upgrade-begin").click()
                    await until(lambda: frame.locator("#upgrade-cancel").is_enabled(), timeout=30)
                    await until(maintenance_ready, timeout=180)
                    await frame.locator('[data-close="upgrade-dialog"]').click()
                    await frame.locator("#tab-memories").click()
                    await frame.locator("#scope-picker").select_option(json.dumps([carol["platform_instance_id"], carol["user_id"]], separators=(",", ":")))
                    stale_revision = (await self.bootstrap())["config_revision"]
                    control_path = Path(configuration.get("control_state_dir") or str(directory.parent / (NAME + "_control"))) / "control.sqlite3"
                    with sqlite3.connect(control_path) as control:
                        control.execute("CREATE TRIGGER e2e_pause_failure BEFORE UPDATE OF discard_pending ON maintenance "
                                        "BEGIN SELECT RAISE(ABORT,'injected pause write failure'); END")
                    try:
                        try:
                            await self.action(carol["scope_id"], "maintenance_begin", config_revision=stale_revision)
                        except RuntimeError:
                            pass
                        await frame.locator("#settings-conflict").wait_for(state="visible", timeout=30000)
                        await until(lambda: frame.locator("#maintenance-view").is_visible(), timeout=30)
                    finally:
                        with sqlite3.connect(control_path) as control:
                            control.execute("DROP TRIGGER e2e_pause_failure")
                    await frame.locator("#reload-settings").click()
                    await frame.locator("#settings-conflict").wait_for(state="hidden", timeout=30000)
                    try:
                        await self.http("POST", self.prefix + "/service/upgrade", payload={
                            "action": "cancel", "config_revision": stale_revision,
                        })
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("A stale page revision was accepted after reloading settings")
                    await self.action(carol["scope_id"], "maintenance_begin",
                                      config_revision=(await self.bootstrap())["config_revision"])
                    async def personal_pause():
                        return carol["bank"] not in {
                            row["bank"] for row in json.loads((directory / "setup.json").read_text())["upgrade_pauses"]["banks"]
                        }
                    await until(personal_pause, timeout=30)
                    await until(lambda: frame.locator("#maintenance-view").is_visible(), timeout=30)
                    await frame.locator("#tab-setup").click()
                    await frame.locator("#view-upgrade").click()
                    await frame.locator("#upgrade-cancel").click()
                    async def abandoned():
                        rows = (await self.bootstrap())["scopes"]
                        return (not next(row for row in rows if row["user_id"] == str(ALICE))["summary"]["maintenance"]
                                and next(row for row in rows if row["user_id"] == bob_id)["summary"]["maintenance"]
                                and next(row for row in rows if row["user_id"] == carol_id)["summary"]["maintenance"])
                    await until(abandoned, timeout=30)
                    self.passed("browser_abandon_old_version_restores_only_upgrade_pauses")
                    self.passed("browser_personal_pause_updates_revision_without_reloading_and_is_not_bulk_resumed")
                    self.passed("browser_personal_pause_failure_reloads_settings_and_rejects_stale_page")
                    await until(lambda: frame.locator("#upgrade-begin").is_enabled(), timeout=30)
                    await frame.locator("#upgrade-begin").click()
                    await until(lambda: frame.locator("#upgrade-cancel").is_enabled(), timeout=30)
                    await until(maintenance_ready, timeout=180)
                    configuration = await self.config()
                    subprocess.run([sys.executable, str(ROOT / "verification/data/plugins" / NAME / "scripts/backup.py"),
                                    "--data-dir", str(directory),
                                    "--control-dir", configuration.get("control_state_dir") or str(directory.parent / (NAME + "_control")),
                                    "--output", str(ROOT / "upgrade-plugin-backup"),
                                    "--latest-control", str(ROOT / "upgrade-control-latest.sqlite3"),
                                    "--backend-version", "1.1.0"], check=True, stdout=subprocess.DEVNULL)
                    await frame.locator("#upgrade-backup-confirmed").check()
                    await frame.locator("#upgrade-prepare").click()
                    await until(lambda: frame.locator("#upgrade-command").input_value(), timeout=60)
                    private_command("managed_upgrade", await frame.locator("#upgrade-command").input_value())
                    await self.http("POST", self.prefix + "/service/upgrade", payload={
                        "action": "begin", "config_revision": (await self.bootstrap())["config_revision"],
                    })
                    await self.action(scope["scope_id"], "mode", mode="paused")
                    blocked = await self.bot.chat(ALICE, "/hindsight forget")
                    assert "服务升级期间" in blocked
                    assert next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(ALICE))["bank"] == scope["bank"]
                    self.passed("browser_old_version_reminder_all_banks_maintenance_plugin_backup_and_upgrade_command")
                    await self.control("managed_upgrade")
                    self.settings = json.loads((ROOT / "live-settings.json").read_text())
                    self.report.update(hindsight=self.settings["hindsight_version"], hindsight_image_id=self.settings["hindsight_image_id"])
                    await self.backend_ready()
                    blocked = await self.bot.chat(ALICE, "/hindsight forget")
                    assert "服务升级期间" in blocked
                    assert next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(ALICE))["bank"] == scope["bank"]
                    self.passed("native_forget_rejected_after_preparation_and_after_container_upgrade_before_verification")
                    await frame.locator("#upgrade-verify").click()
                    async def verified():
                        value = await self.http("GET", self.prefix + "/service/status")
                        return value["version"] == "1.2.0" and not value["pending"]
                    await until(verified, timeout=60)
                    current = next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(ALICE))
                    assert current["bank"] == scope["bank"]
                    assert "Hazel-63" in json.dumps(await self.memories(scope["bank"]))
                    self.passed("unmodified_upgrade_command_creates_private_backups_and_preserves_old_bank_and_memories")
                    await until(lambda: frame.locator("#upgrade-resume").is_visible(), timeout=30)
                    assert not await frame.locator("#upgrade-abandon").is_visible()
                    await frame.locator("#upgrade-resume").click()
                    async def resumed():
                        rows = (await self.bootstrap())["scopes"]
                        alice_state = next(row for row in rows if row["user_id"] == str(ALICE))
                        bob_state = next(row for row in rows if row["user_id"] == bob_id)
                        return (not alice_state["summary"]["maintenance"] and alice_state["mode"] == "paused"
                                and bob_state["summary"]["maintenance"] and bob_state["mode"] == "readonly"
                                and next(row for row in rows if row["user_id"] == carol_id)["summary"]["maintenance"])
                    await until(resumed, timeout=30)
                    self.passed("browser_bulk_resume_preserves_preexisting_pause_and_original_modes")
                    await frame.locator('[data-close="upgrade-dialog"]').click()
                    await frame.locator("#tab-memories").click()
                    await frame.locator("#scope-picker").select_option(json.dumps([scope["platform_instance_id"], scope["user_id"]], separators=(",", ":")))
                    assert await frame.locator("#scope-picker").count() == 1
                    assert await frame.locator("#maintenance-dialog #maintenance-resume").count() == 1
                    assert await frame.locator("#scope-statuses").count() == 0
                    assert await frame.get_by_role("button", name="管理此用户", exact=True).count() == 0
                    await self.usable()
                    await self.action(scope["scope_id"], "mode", mode="active")
                    await asyncio.sleep(1.1)
                    await self.recall_notebook("What was my notebook code? Reply briefly.", "after_upgrade")
                    await self.drained()
                    self.passed("browser_upgrade_verification_resume_and_new_session_recall_on_1_2")
                await page.screenshot(path=str(ROOT / "managed-wizard.png"), full_page=True)
                await frame.locator("#tab-setup").click()
                await frame.locator("#reinstall").click()
                await frame.locator("#generate-deployment").click()
                await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
                private_command("managed_reinstall", await frame.locator("#install-command").input_value())
                await self.control("managed_reinstall")
                await self.backend_ready()
                await self.usable()
                assert next(row for row in (await self.bootstrap())["scopes"] if row["user_id"] == str(ALICE))["bank"] == scope["bank"]
                assert "Hazel-63" in json.dumps(await self.memories(scope["bank"]))
                assert configuration == await self.config()
                self.passed("unmodified_redeployment_preserves_model_settings_bank_credentials_and_memories")
                await frame.locator('[data-close="deployment-dialog"]').click()

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
                await frame.locator("#prepare-uninstall").click()
                await frame.locator("#delete-memory-data").check()
                await frame.locator("#generate-uninstall").click()
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
            except Exception:
                if page:
                    await page.screenshot(path=str(ROOT / "managed-failure.png"), full_page=True)
                    (ROOT / "managed-failure-visible-text.txt").write_text(await page.locator("body").inner_text())
                raise
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
