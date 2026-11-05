"""Compare panel polling and pause recovery against a committed browser baseline."""

import asyncio
import json
from pathlib import Path
import sqlite3

from live_e2e import ALICE, NAME, ROOT, until


async def verify_panel(acceptance, page, context, frame, scope):
    identifier = scope["scope_id"]
    configuration = await acceptance.config()
    original_mode = scope["mode"]
    observations = {}
    server_responses = {}

    async def selected():
        state = await acceptance.bootstrap()
        return next(row for row in state["scopes"] if row["scope_id"] == identifier)

    async def reload_panel():
        await page.reload()
        await acceptance.dismiss_host_notice(page)
        await frame.locator("#startup").wait_for(state="hidden", timeout=60000)
        await frame.locator("#tab-memories").click()
        await frame.locator("#scope-picker").select_option(identifier)

    async def ready():
        return await frame.locator("#maintenance-resume").is_enabled()

    async def refresh():
        await frame.locator("body").evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")

    async def observe(label):
        await until(ready, timeout=180)
        responses = []

        async def capture(response):
            if "maintenance_status" in response.url and response.ok:
                value = await response.json()
                state = value.get("data", value).get("maintenance", {})
                responses.append({key: state.get(key) for key in
                                  ("phase", "ready", "pending", "in_flight", "remote_active")})

        page.on("response", capture)
        await frame.locator("body").evaluate("""() => {
          window.pauseObservation = [];
          const record = () => {
            const value = {
              text: document.getElementById("maintenance-state").textContent,
              disabled: document.getElementById("maintenance-resume").disabled,
            };
            const previous = window.pauseObservation.at(-1);
            if (!previous || previous.text !== value.text || previous.disabled !== value.disabled)
              window.pauseObservation.push({...value, at: performance.now()});
          };
          record();
          window.pauseObserver = new MutationObserver(record);
          window.pauseObserver.observe(document.getElementById("user-controls"), {
            subtree: true, childList: true, characterData: true, attributes: true,
          });
        }""")
        await page.wait_for_timeout(32000)
        page.remove_listener("response", capture)
        assert len(responses) >= 3 and all(row["ready"] for row in responses)
        server_responses[label] = responses
        observations[label] = await frame.locator("body").evaluate("""() => {
          window.pauseObserver.disconnect();
          return window.pauseObservation;
        }""")
        (ROOT / "panel-observations.json").write_text(json.dumps({
            "baseline_ref": acceptance.settings["panel_baseline_ref"], "seconds_per_observation": 32,
            "scope_id": identifier, "bank": scope["bank"], "observations": observations,
            "server_responses": server_responses,
        }, indent=2) + "\n")

    # Removing the account creates a real historical management range.
    await acceptance.config(allowed_accounts=[])
    await acceptance.usable()
    assert (await selected())["historical"]
    await acceptance.action(identifier, "maintenance_begin")
    await reload_panel()
    await observe("baseline")
    assert any(row["disabled"] for row in observations["baseline"]), "Baseline flicker was not reproduced"
    acceptance.passed("committed_baseline_historical_range_reproduces_ready_polling_flicker_over_32_seconds")

    await acceptance.install("candidate-upgrade.zip")
    acceptance.report["candidate_tree_sha256"] = json.loads(
        (ROOT / "package-inventory.json").read_text())["tree_sha256"]
    await acceptance.usable()
    await reload_panel()
    await observe("candidate")
    assert all(not row["disabled"] and row["text"] == "可以修改记忆"
               for row in observations["candidate"])
    assert await frame.locator("#scope-mode-field").is_hidden()
    assert await frame.locator("#save-mode").is_hidden()
    assert scope["bot_id"] in await frame.locator("#historical-mode").inner_text()
    assert await frame.locator("#scope-bank").input_value() == scope["bank"]
    assert await frame.locator("#background-page").input_value() == scope["background"]["page_id"]
    assert await frame.locator("#background-enabled").is_checked() == scope["background"]["enabled"]
    assert (await selected())["mode"] == original_mode
    acceptance.passed("candidate_historical_range_ready_dom_and_resume_stable_over_32_seconds")

    control_path = Path(configuration.get("control_state_dir") or
                        str(acceptance.runtime.parent.parent / (NAME + "_control"))) / "control.sqlite3"
    with sqlite3.connect(control_path) as control:
        control.execute("CREATE TRIGGER panel_status_failure BEFORE UPDATE OF phase ON maintenance "
                        "BEGIN SELECT RAISE(ABORT,'panel status write failure'); END")
    try:
        await refresh()
        await frame.locator("#maintenance-retry").wait_for(state="visible", timeout=30000)
        assert await frame.locator("#maintenance-resume").is_disabled()
        assert await frame.locator("#maintenance-retry").is_visible()
        assert "可以修改记忆" not in await frame.locator("#maintenance-state").inner_text()
    finally:
        with sqlite3.connect(control_path) as control:
            control.execute("DROP TRIGGER panel_status_failure")
    await frame.locator("#maintenance-retry").click()
    await until(ready, timeout=30)
    acceptance.passed("real_control_database_failure_disables_resume_and_retry_recovers")

    await acceptance.control("managed_pause")
    try:
        await refresh()
        await frame.locator("#maintenance-retry").wait_for(state="visible", timeout=60000)
        assert await frame.locator("#maintenance-resume").is_disabled()
        assert "可以修改记忆" not in await frame.locator("#maintenance-state").inner_text()
        assert await frame.locator("#maintenance-retry").is_visible()
    finally:
        await acceptance.control("managed_unpause")
    await acceptance.backend_ready()
    await acceptance.usable()
    await refresh()
    await until(ready, timeout=60)
    acceptance.passed("real_hindsight_request_timeout_clears_confirmed_ready_and_recovers")

    with sqlite3.connect(control_path) as control:
        control.execute("UPDATE banks SET state='missing' WHERE bank=?", (scope["bank"],))
    try:
        await refresh()
        await until(lambda: frame.locator("#maintenance-resume").is_disabled(), timeout=30)
        assert await frame.locator("#maintenance-resume").is_disabled()
        assert "找不到" in await frame.locator("#maintenance-state").inner_text()
    finally:
        with sqlite3.connect(control_path) as control:
            control.execute("UPDATE banks SET state='established' WHERE bank=?", (scope["bank"],))
    await refresh()
    await until(ready, timeout=30)
    acceptance.passed("real_missing_bank_control_state_invalidates_ready")

    for label, width, height in [("desktop", 1280, 900), ("phone", 390, 844)]:
        await page.set_viewport_size({"width": width, "height": height})
        await frame.locator("#tab-memories").click()
        assert await frame.locator("#service-version").is_hidden()
        assert await frame.get_by_role("heading", name="服务连接状态", exact=True).count() == 0
        assert await frame.locator("#maintenance-status").count() == 0
        assert await frame.locator("#service-alert").is_hidden()
        assert await frame.locator("#background-native-help").count() == 0
        assert await frame.locator("body").evaluate("(node) => node.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(ROOT / ("panel-" + label + ".png")), full_page=True)
        await frame.locator("#tab-setup").click()
        assert await frame.locator("#service-version-check").is_visible()
        assert await frame.locator("#reinstall").is_visible()
        assert await frame.locator("#prepare-uninstall").is_visible()
        assert await frame.locator("body").evaluate("(node) => node.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(ROOT / ("panel-setup-" + label + ".png")), full_page=True)
    acceptance.passed("desktop_and_phone_homepage_settings_and_historical_controls_match_without_overflow")
    await page.set_viewport_size({"width": 1280, "height": 900})

    version_override = {}

    async def version_fixture(route):
        response = await route.fetch()
        value = await response.json()
        state = value.get("data", value)
        state["config"]["managed"] = version_override["managed"]
        state["backend"] = {
            "version": version_override["version"], "managed": version_override["managed"],
            "upgrade_available": version_override["version"] == "1.1.0", "pending": False,
            "upgrade_paused": version_override["finishing"], "resume_available": version_override["finishing"],
        }
        await route.fulfill(response=response, json=value)

    pattern = "**" + acceptance.prefix + "/bootstrap*"
    await page.route(pattern, version_fixture)
    try:
        for version, managed, finishing in [
            ("1.1.0", True, False), ("1.1.0", False, False),
            ("1.2.0", True, False), (None, True, False), ("1.2.0", True, True),
        ]:
            version_override.update(version=version, managed=managed, finishing=finishing)
            await reload_panel()
            await frame.locator("#tab-setup").click()
            available = version == "1.1.0" or finishing
            assert await frame.locator("#setup-update").is_visible() == available
            assert await frame.locator("#go-upgrade").is_visible() == available
            assert await frame.locator("#service-upgrade").is_visible() == (managed and available)
            if available:
                await frame.locator("#go-upgrade").click()
                assert await frame.locator("#service-upgrade" if managed else "#external-upgrade").is_visible()
    finally:
        await page.unroute(pattern, version_fixture)
        await reload_panel()
    acceptance.passed("real_dom_version_entry_matrix_managed_external_unknown_and_upgrade_completion")

    await frame.locator("#tab-setup").click()
    await frame.locator("#control-plane-url").fill("https://draft-panel.invalid")
    await frame.locator('[name="connection-mode"][value="existing"]').check()
    await frame.locator("#api-key").fill("owned-unsaved-key")
    await frame.locator(".account-user").first.fill("123777")
    await refresh()
    await page.wait_for_timeout(1500)
    assert await frame.locator("#control-plane-url").input_value() == "https://draft-panel.invalid"
    assert await frame.locator("#api-key").input_value() == "owned-unsaved-key"
    assert await frame.locator(".account-user").first.input_value() == "123777"
    stale_revision = (await acceptance.bootstrap())["config_revision"]
    await acceptance.config(control_plane_url="http://127.0.0.1:9988")
    await refresh()
    await frame.locator("#settings-conflict").wait_for(state="visible", timeout=30000)
    assert await frame.locator("#control-plane-url").input_value() == "https://draft-panel.invalid"
    async with acceptance.session.post(
        acceptance.dashboard + acceptance.prefix + "/settings/save",
        json={"config_revision": stale_revision},
        headers={"Authorization": "Bearer " + acceptance.token},
    ) as response:
        result = await response.json()
        assert result.get("status") == "error" and "配置已被更新" in json.dumps(result, ensure_ascii=False)

    async def fail_bootstrap(route):
        await route.abort("failed")

    await page.route(pattern, fail_bootstrap)
    try:
        await frame.locator("#reload-settings").click()
        await frame.locator("#feedback.error").wait_for(state="visible", timeout=30000)
        assert await frame.locator("#settings-conflict").is_visible()
        assert await frame.locator("#control-plane-url").input_value() == "https://draft-panel.invalid"
    finally:
        await page.unroute(pattern, fail_bootstrap)
    await until(lambda: frame.locator("#reload-settings").is_enabled(), timeout=30)
    await frame.locator("#reload-settings").click()
    await frame.locator("#settings-conflict").wait_for(state="hidden", timeout=30000)
    assert await frame.locator("#control-plane-url").input_value() == "http://127.0.0.1:9988"
    assert await frame.locator("#api-key").input_value() == ""
    await acceptance.config(control_plane_url=configuration.get("control_plane_url", ""))
    acceptance.passed("unsaved_settings_survive_polling_and_conflict_failed_reload_recovers_and_stale_save_is_rejected")

    # Reauthorizing the same robot retains its Bank and original memory mode.
    await acceptance.config(allowed_accounts=configuration["allowed_accounts"])
    state = await acceptance.bootstrap()
    payload = {"action": "prepare", "platform_instance_id": scope["platform_instance_id"],
               "user_id": scope["user_id"], "config_revision": state["config_revision"]}
    prepared = await acceptance.http("POST", acceptance.prefix + "/accounts/authorize", payload=payload)
    await acceptance.http("POST", acceptance.prefix + "/accounts/authorize",
                          payload={**payload, "action": "confirm", "confirmation": prepared["confirmation"]})
    await reload_panel()
    await until(ready, timeout=30)
    await frame.locator("#maintenance-resume").click()
    await frame.locator("#maintenance-begin").wait_for(state="visible", timeout=30000)
    await frame.locator("#scope-mode").select_option("readonly")
    await frame.locator("#save-mode").click()
    await until(lambda: frame.locator("#save-mode").is_enabled(), timeout=30)
    await frame.locator("#maintenance-begin").click()
    await until(ready, timeout=60)
    await frame.locator("#maintenance-resume").click()
    await frame.locator("#maintenance-begin").wait_for(state="visible", timeout=30000)
    assert (await selected())["mode"] == "readonly"
    assert not (await selected())["summary"]["maintenance"]
    await frame.locator("#scope-mode").select_option(original_mode)
    await frame.locator("#save-mode").click()
    await until(lambda: frame.locator("#save-mode").is_enabled(), timeout=30)
    acceptance.passed("browser_pause_checks_automatically_and_resume_preserves_original_readonly_mode")

    await acceptance.control("hold_extraction")
    try:
        await asyncio.sleep(1.1)
        await acceptance.bot.chat(ALICE, "Remember: the owned panel test color is amber. Reply briefly.")
        await until(lambda: (ROOT / "held-extraction-trace.jsonl").exists(), timeout=90)
        await frame.locator("#maintenance-begin").click()
        await until(lambda: frame.locator("#maintenance-begin").is_hidden(), timeout=30)
        assert await frame.locator("#maintenance-resume").is_disabled()
        assert "可以修改记忆" not in await frame.locator("#maintenance-state").inner_text()
    finally:
        await acceptance.control("release_extraction")
    discarded_unsent = False

    async def finish_pause():
        nonlocal discarded_unsent
        row = await selected()
        if row["summary"].get("can_discard") and not discarded_unsent:
            await frame.locator("#discard-records").wait_for(state="visible", timeout=15000)
            await frame.locator("#discard-records summary").click()
            await frame.locator("#discard-pending").check()
            await frame.locator("#maintenance-discard").click()
            discarded_unsent = True
        return await ready()

    await until(finish_pause, timeout=180)
    if discarded_unsent:
        assert (await selected())["summary"]["counts"].get("discarded", 0) > 0
        acceptance.passed("real_browser_explicit_secondary_discard_releases_unsent_records_after_pause")
    await frame.locator("#maintenance-resume").click()
    await frame.locator("#maintenance-begin").wait_for(state="visible", timeout=30000)
    await acceptance.drained()
    acceptance.passed("real_inflight_retain_waits_and_enters_ready_automatically_after_completion")

    (ROOT / "panel-observations.json").write_text(json.dumps({
        "baseline_ref": acceptance.settings["panel_baseline_ref"], "seconds_per_observation": 32,
        "scope_id": identifier, "bank": scope["bank"], "observations": observations,
        "server_responses": server_responses,
    }, indent=2) + "\n")
    assert (await selected())["mode"] == original_mode
    assert (await selected())["bank"] == scope["bank"]
