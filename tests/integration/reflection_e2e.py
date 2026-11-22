"""Native Reflect settings, late-result isolation and format-seven upgrade recovery."""

import asyncio
import json
import sqlite3
import time

from live_e2e import ALICE, BOB, NAME, ROOT, database, jsonl, until
from reading_e2e import results


async def verify(acceptance, page, frame, bank):
    acceptance.report["tool_schema_mode"] = acceptance.settings.get("tool_schema_mode", "full")
    acceptance.report["upgrade_baseline"] = acceptance.settings.get("baseline_revision", "v0.4.0")
    scope = await acceptance.scope()
    identifier = scope["scope_id"]
    assert scope["reflect"]["enabled"] is False and scope["reading"]["mode"] == "automatic"
    await acceptance.bot.chat(ALICE, "A native Reflect capability observation.")
    await until(lambda: frame.locator("#reflect-enabled").is_enabled(), timeout=15)

    async def configure(enabled, timeout=8):
        snapshot = await acceptance.bootstrap()
        current = await acceptance.scope()
        return await acceptance.http("POST", acceptance.prefix + "/scopes/" + identifier + "/reflect", payload={
            "enabled": enabled, "timeout": timeout, "revision": current["reflect"]["revision"],
            "scope_revision": current["revision"], "config_revision": snapshot["config_revision"],
        })

    await frame.locator("#reflect-enabled").check()
    await frame.locator("#reflect-timeout").fill("8")
    await frame.locator("#reflect-save").click()
    await until(lambda: reflect_enabled(acceptance), timeout=15)
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await until(lambda: frame.locator("#reflect-enabled").is_checked(), timeout=15)
    assert await frame.locator("#reflect-timeout").input_value() == "8"
    current = await acceptance.scope()
    assert current["bank"] == bank and current["mode"] == scope["mode"]
    assert current["reflect"]["enabled"] and current["reflect"]["timeout"] == 8
    assert current["reading"]["mode"] == "automatic"
    await page.set_viewport_size({"width": 390, "height": 844})
    try:
        await until(lambda: frame.locator("body").evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), timeout=10)
    finally:
        await page.screenshot(path=str(ROOT / "reflect-phone.png"), full_page=True)
    assert await frame.locator("body").evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    await page.set_viewport_size({"width": 1280, "height": 900})
    acceptance.passed("native_reflect_panel_defaults_off_saves_independent_settings_and_fits_phone")

    async def chat(text):
        start = len(acceptance.requests())
        answer = await acceptance.bot.chat(ALICE, text)
        assert "HB-REFLECT-FINAL" in answer, answer
        return acceptance.requests()[start:]

    start = len(jsonl("reflect-trace"))
    recalls = len(jsonl("recall-trace"))
    flow = await chat("HB-REFLECT-COMPLEX")
    value = next(item for item in results(flow) if item.get("kind") == "reflect")
    assert "HB-REFLECT-ANSWER" in value["text"]
    native = jsonl("reflect-response-trace")[-1]["response"]
    assert value["usage"] == {key: native["usage"][key] for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "thoughts_tokens") if key in native["usage"]}
    assert any(value["sources"][key] for key in ("memories", "mental_models", "directives"))
    for key in ("memories", "mental_models", "directives"):
        native_ids = {item["id"] for item in native["based_on"][key]}
        assert all(item["id"] in native_ids for item in value["sources"][key])
    assert len(jsonl("reflect-trace")) == start + 1 and len(jsonl("recall-trace")) == recalls + 1
    assert {tool["function"]["name"] for tool in flow[0].get("tools", []) if tool["function"]["name"].startswith("hindsight_bridge_")} == {"hindsight_bridge_reflect"}
    request = jsonl("reflect-trace")[-1]
    assert request["path"].endswith("/" + bank + "/reflect")
    assert set(request["body"]) == {"query", "budget", "max_tokens", "include"}
    assert request["body"]["budget"] == "low" and request["body"]["include"] == {"facts": {}}
    assert (await acceptance.scope())["reflect"]["status"]["state"] == "success"
    assert not (await acceptance.scope())["reflect"]["pending"]
    acceptance.passed("actual_agent_reflects_with_native_answer_sources_usage_and_keeps_automatic_recall")

    await acceptance.bot.chat(BOB, "A second user keeps Reflect disabled.")
    assert not any(tool["function"]["name"] == "hindsight_bridge_reflect" for tool in acceptance.requests()[-1].get("tools", []))
    assert not (await acceptance.scope(BOB))["reflect"]["enabled"]
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await frame.locator("#reading-mode").select_option("tools")
    await frame.locator("#reading-save").click()
    start = len(jsonl("reflect-trace"))
    shared = results(await chat("HB-REFLECT-SHARED"))
    assert any(item.get("kind") == "reflect" for item in shared)
    assert any(item.get("error") == "tool_budget_exhausted" for item in shared)
    assert (await acceptance.scope())["reading"]["status"]["calls"] == 4
    assert (await acceptance.scope())["reflect"]["status"]["calls"] == 1
    assert len(jsonl("reflect-trace")) == start + 1
    await frame.locator("#reading-mode").select_option("automatic")
    await frame.locator("#reading-save").click()
    acceptance.passed("reflect_is_per_user_and_shares_the_original_round_budget_with_read_tools")

    await acceptance.control("hold_reflect")
    start = len(jsonl("reflect-trace"))
    held = asyncio.create_task(chat("HB-REFLECT-PAUSE"))
    await until(lambda: len(jsonl("reflect-trace")) == start + 1, timeout=15)
    await acceptance.action(identifier, "mode", mode="paused")
    await acceptance.control("release_reflect")
    dropped = results(await held)
    assert not any(item.get("kind") == "reflect" for item in dropped)
    assert any(item.get("error") == "reading_changed" for item in dropped)
    await until(lambda: no_pending(acceptance), timeout=15)
    await acceptance.action(identifier, "mode", mode="readonly")
    acceptance.passed("native_reflect_pause_revokes_late_data_and_readonly_resumes_without_a_lock")

    await configure(False)
    start = len(jsonl("reflect-trace"))
    await acceptance.bot.chat(ALICE, "An automatic recall with Reflect disabled.")
    assert len(jsonl("reflect-trace")) == start
    await configure(True)
    await acceptance.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
    await acceptance.usable()
    assert (await acceptance.scope())["reflect"]["enabled"]
    await chat("HB-REFLECT-COMPLEX")
    acceptance.passed("native_reflect_disable_and_reload_preserve_independent_reading_mode_and_bank")

    await configure(True, timeout=3)
    await acceptance.control("hold_reflect")
    timeout_flow = results(await chat("HB-REFLECT-TIMEOUT"))
    assert any(item.get("error") == "reflect_timeout" for item in timeout_flow)
    assert (await acceptance.scope())["reflect"]["pending"] == 1
    assert (await acceptance.scope())["reflect"]["status"]["code"] == "reflect_timeout"
    await acceptance.action(identifier, "maintenance_begin")
    pending = await acceptance.action(identifier, "maintenance_status")
    assert pending["maintenance"]["ready"], pending
    await acceptance.action(identifier, "maintenance_resume")
    assert (await acceptance.scope())["mode"] == "readonly"
    await acceptance.bot.chat(BOB, "Ordinary chat continues during an outstanding Reflect request.")
    start = len(jsonl("reflect-trace"))
    repeated = asyncio.create_task(chat("HB-REFLECT-AFTER-TIMEOUT"))
    await until(lambda: len(jsonl("reflect-trace")) == start + 1, timeout=15)
    await acceptance.control("release_reflect")
    assert any(item.get("kind") == "reflect" for item in results(await repeated))
    await until(lambda: no_pending(acceptance), timeout=15)
    assert (await acceptance.scope())["reflect"]["status"]["state"] == "success"
    acceptance.passed("native_reflect_timeout_keeps_diagnostics_without_blocking_maintenance_or_a_new_reflect")

    await configure(True, timeout=8)
    await acceptance.control("hold_reflect")
    start = len(jsonl("reflect-trace"))
    held = asyncio.create_task(chat("HB-REFLECT-FORGET"))
    await until(lambda: len(jsonl("reflect-trace")) == start + 1, timeout=15)
    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
    current = await acceptance.scope()
    assert current["bank"] != bank and not current["reflect"]["enabled"]
    await acceptance.control("release_reflect")
    assert not any(item.get("kind") == "reflect" for item in results(await held))

    async def erased(retired):
        rows = database(ROOT / "control-volume/control.sqlite3", "SELECT phase,cleanup_step FROM erasures WHERE old_bank=?", (retired,))
        if not rows or rows[0]["phase"] != "verified" or rows[0]["cleanup_step"] != 7:
            return False
        async with acceptance.session.get(acceptance.settings["hindsight_url"] + "/v1/default/banks/" + retired + "/config",
                                          headers={"Authorization": "Bearer " + acceptance.settings["memory_key"]}) as response:
            return response.status == 404
    await until(lambda: erased(bank), timeout=90)
    current = await acceptance.scope()
    assert current["bank"] != bank and not current["reflect"]["enabled"] and current["reflect"]["invalidated"]
    acceptance.passed("native_forget_invalidates_late_reflect_and_erases_old_bank_without_an_execution_lock")

    await acceptance.action(identifier, "mode", mode="active")
    await asyncio.sleep(1.1)
    await acceptance.bot.chat(ALICE, "A new generation has its own synthetic memory.")
    await acceptance.drained()
    new_bank = (await acceptance.scope())["bank"]
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await until(lambda: frame.locator("#reflect-enabled").is_enabled(), timeout=15)
    await frame.locator("#reflect-enabled").check()
    await frame.locator("#reflect-save").click()
    await until(lambda: reflect_enabled(acceptance), timeout=15)
    await acceptance.action(identifier, "mode", mode="readonly")
    await acceptance.control("drop_reflect_response")
    lost = results(await chat("HB-REFLECT-LOST"))
    assert not any(item.get("kind") == "reflect" for item in lost)
    assert any(item.get("error") in {"connection_error", "reflect_transport_timeout"} for item in lost), lost
    await until(lambda: no_pending(acceptance), timeout=15)
    assert "unknown" not in (await acceptance.scope())["reflect"]
    await acceptance.action(identifier, "maintenance_begin")
    pending = await acceptance.action(identifier, "maintenance_status")
    assert pending["maintenance"]["ready"], pending
    await acceptance.action(identifier, "maintenance_resume")
    assert (await acceptance.scope())["mode"] == "readonly"
    repeated = results(await chat("HB-REFLECT-AFTER-LOST-RESPONSE"))
    assert any(item.get("kind") == "reflect" for item in repeated)
    assert not (await acceptance.scope())["reflect"]["pending"]
    assert await frame.locator("#reflect-override, #reflect-override-dialog, #maintenance-reflect-override").count() == 0
    acceptance.passed("native_lost_reflect_response_allows_maintenance_resume_and_another_reflect_without_a_manual_override")

    await acceptance.install("baseline-install.zip")
    await acceptance.usable()
    await acceptance.control("drop_reflect_response")
    lost = results(await chat("HB-REFLECT-LEGACY-LOST"))
    assert not any(item.get("kind") == "reflect" for item in lost)
    assert (await acceptance.scope())["reflect"]["unknown"] == 1
    other_bank = (await acceptance.scope(BOB))["bank"]
    journal = ROOT / "control-volume/control.sqlite3"
    with sqlite3.connect(journal, timeout=10) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        for state, source_bank in (("prepared", bank), ("dispatched", other_bank)):
            owner = connection.execute("SELECT connection_id,owner_id,generation FROM banks WHERE bank=?", (source_bank,)).fetchone()
            assert owner is not None
            connection.execute(
                "INSERT INTO reflection_requests(request_id,connection_id,owner_id,bank,generation,state,created_at) VALUES (?,?,?,?,?,?,?)",
                ("hb-legacy-" + state, owner[0], owner[1], source_bank, owner[2], state, time.time()),
            )
    legacy = database(journal, "SELECT * FROM reflection_requests ORDER BY request_id")
    assert {row["state"] for row in legacy} == {"prepared", "dispatched", "unknown"}
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    assert (await acceptance.scope())["bank"] == new_bank
    assert "unknown" not in (await acceptance.scope())["reflect"]
    audit = database(journal, "SELECT * FROM reflection_requests ORDER BY request_id")
    assert [row["request_id"] for row in audit] == [row["request_id"] for row in legacy]
    assert all(row["state"] == "overridden" and row["overridden_at"] and row["overridden_by"] == "startup" for row in audit)
    assert database(journal, "PRAGMA user_version")[0]["user_version"] == 7
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    assert await frame.locator("#reflect-override, #reflect-override-dialog, #maintenance-reflect-override").count() == 0
    await acceptance.action(identifier, "maintenance_begin")
    pending = await acceptance.action(identifier, "maintenance_status")
    assert pending["maintenance"]["ready"], pending
    await acceptance.action(identifier, "maintenance_resume")
    assert any(item.get("kind") == "reflect" for item in results(await chat("HB-REFLECT-AFTER-UPGRADE")))
    acceptance.passed("native_040_to_050_upgrade_archives_all_format_seven_reflect_states_and_keeps_bank_and_reading_available")

    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
    await until(lambda: erased(new_bank), timeout=90)
    assert database(journal, "SELECT * FROM reflection_requests ORDER BY request_id") == audit
    await acceptance.bot.chat(BOB, "The other owner keeps its memory after the Reflect upgrade and erasure.")
    acceptance.report["reflect_upgrade_recovery"] = {
        "control_format": 7, "baseline": "v0.4.0",
        "legacy_states": sorted({row["state"] for row in legacy}),
        "legacy_unknown": "native Reflect response dropped while v0.4.0 was installed",
        "legacy_prepared_dispatched": "format-seven interruption fixtures inserted before candidate upgrade",
        "active_bank": "maintenance resumed and native Reflect completed",
        "old_bank": "all seven cleanup steps verified and native Bank returned 404",
        "archived_records_retained": len(audit),
    }
    acceptance.passed("native_erasure_after_legacy_reflect_recovery_retains_archives_and_deletes_the_entire_old_bank")


async def no_pending(acceptance):
    return not (await acceptance.scope())["reflect"]["pending"]


async def reflect_enabled(acceptance):
    return (await acceptance.scope())["reflect"]["enabled"]
