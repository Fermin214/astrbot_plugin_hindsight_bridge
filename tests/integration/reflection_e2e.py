"""Native Reflect, per-user settings, source evidence and durable erasure barriers."""

import asyncio
import json

from live_e2e import ALICE, BOB, NAME, ROOT, database, jsonl, until
from reading_e2e import results


async def verify(acceptance, page, frame, bank):
    acceptance.report["tool_schema_mode"] = acceptance.settings.get("tool_schema_mode", "full")
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
    await until(lambda: frame.locator("#reflect-effective").filter(has_text="开启").is_visible(), timeout=15)
    current = await acceptance.scope()
    assert current["bank"] == bank and current["mode"] == scope["mode"]
    assert current["reflect"]["enabled"] and current["reflect"]["timeout"] == 8
    assert current["reading"]["mode"] == "automatic"
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await frame.locator("body").evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    await page.screenshot(path=str(ROOT / "reflect-phone.png"), full_page=True)
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
    acceptance.passed("native_reflect_pause_revokes_late_data_without_losing_the_execution_barrier")

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
    await acceptance.bot.chat(BOB, "Ordinary chat continues during an outstanding Reflect request.")
    await acceptance.control("release_reflect")
    await until(lambda: no_pending(acceptance), timeout=15)
    assert (await acceptance.scope())["reflect"]["status"]["code"] == "reflect_timeout"
    acceptance.passed("native_reflect_deadline_returns_to_chat_and_late_response_only_closes_its_barrier")

    await configure(True, timeout=8)
    await acceptance.control("hold_reflect")
    start = len(jsonl("reflect-trace"))
    held = asyncio.create_task(chat("HB-REFLECT-FORGET"))
    await until(lambda: len(jsonl("reflect-trace")) == start + 1, timeout=15)
    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
    await asyncio.sleep(0.7)
    assert (await acceptance.native("GET", bank + "/config")) is not None
    assert database(ROOT / "control-volume/control.sqlite3", "SELECT phase FROM erasures WHERE old_bank=?", (bank,))[0]["phase"] != "verified"
    await acceptance.control("release_reflect")
    assert not any(item.get("kind") == "reflect" for item in results(await held))

    async def erased():
        rows = database(ROOT / "control-volume/control.sqlite3", "SELECT phase FROM erasures WHERE old_bank=?", (bank,))
        return rows and rows[0]["phase"] == "verified"
    await until(erased, timeout=90)
    current = await acceptance.scope()
    assert current["bank"] != bank and not current["reflect"]["enabled"] and current["reflect"]["invalidated"]
    acceptance.passed("native_forget_waits_for_dispatched_reflect_then_erases_old_bank_and_resets_options")

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
    assert (await acceptance.scope())["reflect"]["unknown"] == 1
    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
    await acceptance.install("baseline-install.zip")
    old = await acceptance.bootstrap()
    assert "unsupported_control_version" in json.dumps(old)
    assert database(ROOT / "control-volume/control.sqlite3", "SELECT COUNT(*) count FROM reflection_requests WHERE bank=?", (new_bank,))[0]["count"] == 1
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    assert (await acceptance.scope())["reflect"]["unknown"] == 1
    assert (await acceptance.native("GET", new_bank + "/config")) is not None
    rows = database(ROOT / "control-volume/control.sqlite3", "SELECT phase FROM erasures WHERE old_bank=?", (new_bank,))
    assert rows[0]["phase"] != "verified"
    await acceptance.bot.chat(BOB, "The other owner recovers normally after the Reflect connection loss.")
    acceptance.report["reflect_unknown_recovery"] = "requires an independently verifiable native execution endpoint; not exposed by the supported versions"
    acceptance.passed("lost_reflect_response_persists_a_barrier_through_downgrade_and_reload_without_claiming_erasure_or_blocking_other_owners")


async def no_pending(acceptance):
    return not (await acceptance.scope())["reflect"]["pending"]


async def reflect_enabled(acceptance):
    return (await acceptance.scope())["reflect"]["enabled"]
