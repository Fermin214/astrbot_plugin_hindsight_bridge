"""Verify the real Dashboard panel, account lifecycle and pause recovery."""

import asyncio
import json
from pathlib import Path
import sqlite3

from live_e2e import NAME, ROOT, until


async def verify_panel(acceptance, page, context, frame, scope):
    identifier = scope["scope_id"]
    account_key = json.dumps([scope["platform_instance_id"], scope["user_id"]], separators=(",", ":"))
    configuration = await acceptance.config()
    original_mode = scope["mode"]
    pattern = "**" + acceptance.prefix + "/bootstrap*"

    async def selected():
        return next(row for row in (await acceptance.bootstrap())["scopes"] if row["scope_id"] == identifier)

    async def reload_panel():
        await page.reload()
        await acceptance.dismiss_host_notice(page)
        await frame.locator("#startup").wait_for(state="hidden", timeout=60000)
        await frame.locator("#tab-memories").click()
        if await frame.locator("#scope-picker option[value='" + account_key + "']").count():
            await frame.locator("#scope-picker").select_option(account_key)

    async def refresh():
        await frame.locator("body").evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")

    async def ready():
        return await frame.locator("#maintenance-resume").is_enabled()

    # Each model/mode control uses the server's saved value after a failed write.
    await reload_panel()
    for mode in ("readonly", "paused", original_mode):
        await frame.locator('[data-mode="' + mode + '"]').click()
        async def mode_saved():
            return (await selected())["mode"] == mode
        await until(mode_saved, timeout=30)
        await frame.locator('[data-mode="' + mode + '"][aria-pressed="true"]').wait_for()
    action_pattern = "**" + acceptance.prefix + "/scopes/" + identifier + "/action"
    async def reject_mode(route):
        await route.abort("failed")
    await page.route(action_pattern, reject_mode)
    try:
        await frame.locator('[data-mode="readonly"]').click()
        await frame.locator("#feedback.error").wait_for()
        assert await frame.locator('[data-mode="' + original_mode + '"]').get_attribute("aria-pressed") == "true"
    finally:
        await page.unroute(action_pattern, reject_mode)
    acceptance.passed("browser_mode_selection_saves_immediately_and_failed_save_keeps_server_mode")

    await frame.locator("#tab-setup").click()
    save_button = "#save-access" if (await acceptance.bootstrap())["config"]["managed"] else "#save-settings"
    await frame.locator(save_button).click()
    await until(lambda: frame.locator(save_button).is_enabled(), timeout=30)
    await frame.locator("#service-version-check").click()
    await until(lambda: frame.locator("#service-version-check").is_enabled(), timeout=15)
    version_result = await frame.locator("#version-check-result").inner_text()
    assert acceptance.settings["hindsight_version"] in version_result
    assert "插件支持的配套版本" in version_result
    await refresh()
    await page.wait_for_timeout(1500)
    assert await frame.locator("#version-check-result").inner_text() == version_result
    acceptance.passed("managed_manual_version_result_survives_real_bootstrap_refresh")
    assert (await acceptance.config())["allowed_accounts"] == configuration["allowed_accounts"]
    assert await frame.locator("#setup-intro").is_hidden()
    await frame.locator("#backup-guide").click()
    assert await frame.locator("#backup-dialog").is_visible()
    backup_command = await frame.locator("#backup-command").input_value()
    assert "scripts/backup.py" in backup_command and "--latest-control" in backup_command
    assert (await frame.locator("#backup-doc-url").input_value()).endswith("/docs/DATA-MAINTENANCE.md")
    await context.grant_permissions(["clipboard-read", "clipboard-write"], origin=acceptance.dashboard)
    await frame.locator('[data-copy="backup-command"]').click()
    assert await page.evaluate("navigator.clipboard.readText()") == backup_command
    await frame.locator('[data-close="backup-dialog"]').first.click()
    acceptance.passed("connection_save_preserves_users_and_service_buttons_open_real_workflows")

    # A retained scope must not bring a removed account back into the selector.
    await frame.locator("#tab-memories").click()
    await frame.locator("#remove-account").click()
    await frame.locator("#confirm-remove-account").click()
    await frame.locator("#no-scopes").wait_for(state="visible", timeout=30000)
    assert not (await acceptance.config())["allowed_accounts"]
    assert (await selected())["bank"] == scope["bank"]
    assert (await selected())["availability"] == "removed"
    assert await frame.locator("#scope-picker option").all_text_contents() == ["还没有用户"]
    await acceptance.action(identifier, "maintenance_begin")
    await refresh()
    await frame.locator("#show-paused-banks").wait_for(state="visible", timeout=30000)
    await frame.locator("#show-paused-banks").click()
    await until(ready, timeout=90)
    assert scope["bot_id"] in await frame.locator("#maintenance-scope").inner_text()
    responses = []
    async def capture(response):
        if "maintenance_status" in response.url and response.ok:
            value = await response.json()
            result = value.get("data", value).get("maintenance", {})
            responses.append({key: result.get(key) for key in ("phase", "ready", "pending", "in_flight", "remote_active")})
    page.on("response", capture)
    await frame.locator("body").evaluate("""() => {
      window.pauseObservation = [];
      const record = () => {
        const value = {text: document.getElementById('maintenance-detail').textContent,
          disabled: document.getElementById('maintenance-resume').disabled};
        const last = window.pauseObservation.at(-1);
        if (!last || last.text !== value.text || last.disabled !== value.disabled)
          window.pauseObservation.push({...value, at: performance.now()});
      };
      record(); window.pauseObserver = new MutationObserver(record);
      window.pauseObserver.observe(document.getElementById('maintenance-dialog'), {
        subtree: true, childList: true, characterData: true, attributes: true});
    }""")
    await page.wait_for_timeout(32000)
    page.remove_listener("response", capture)
    observations = await frame.locator("body").evaluate("""() => {
      window.pauseObserver.disconnect(); return window.pauseObservation;
    }""")
    assert len(responses) >= 3 and all(row["ready"] for row in responses), responses
    assert all(not row["disabled"] and row["text"] == "可以修改记忆" for row in observations), observations
    (ROOT / "panel-observations.json").write_text(json.dumps({
        "seconds": 32, "scope_id": identifier, "observations": observations, "server_responses": responses,
    }, indent=2) + "\n")
    acceptance.passed("removed_user_stays_removed_and_retained_bank_pause_ready_remains_stable_for_32_seconds")

    control_path = Path(configuration.get("control_state_dir") or
                        str(acceptance.runtime.parent.parent / (NAME + "_control"))) / "control.sqlite3"
    with sqlite3.connect(control_path) as control:
        control.execute("CREATE TRIGGER panel_status_failure BEFORE UPDATE OF phase ON maintenance "
                        "BEGIN SELECT RAISE(ABORT,'panel status write failure'); END")
    try:
        await refresh()
        await frame.locator("#maintenance-retry").wait_for(state="visible", timeout=30000)
        assert await frame.locator("#maintenance-resume").is_disabled()
        assert "可以修改记忆" not in await frame.locator("#maintenance-detail").inner_text()
    finally:
        with sqlite3.connect(control_path) as control:
            control.execute("DROP TRIGGER panel_status_failure")
    await frame.locator("#maintenance-retry").click()
    await until(ready, timeout=30)
    acceptance.passed("real_control_database_failure_invalidates_ready_and_retry_recovers")

    await acceptance.control("managed_pause")
    try:
        await refresh()
        await frame.locator("#maintenance-retry").wait_for(state="visible", timeout=60000)
        assert await frame.locator("#maintenance-resume").is_disabled()
    finally:
        await acceptance.control("managed_unpause")
    await acceptance.backend_ready()
    await refresh()
    await until(ready, timeout=60)
    with sqlite3.connect(control_path) as control:
        control.execute("UPDATE banks SET state='missing' WHERE bank=?", (scope["bank"],))
    await refresh()
    await until(lambda: frame.locator("#maintenance-resume").is_disabled(), timeout=30)
    assert "找不到" in await frame.locator("#maintenance-detail").inner_text()
    await frame.locator("#maintenance-bank-check").click()
    await until(ready, timeout=30)
    assert not (await selected())["summary"]["bank_missing"]
    await frame.locator("#maintenance-resume").click()
    await frame.locator('[data-close="maintenance-dialog"]').click()
    acceptance.passed("real_service_timeout_and_missing_bank_invalidate_old_ready_and_recover")

    await frame.locator("#add-account").click()
    await frame.locator("#account-platform").select_option(scope["platform_instance_id"])
    await frame.locator("#account-user").fill(scope["user_id"])
    await frame.locator("#save-account").click()
    await frame.locator("#authorization-dialog").wait_for(state="visible", timeout=30000)
    assert scope["bot_id"] in await frame.locator("#authorization-description").inner_text()
    await frame.locator("#confirm-authorization").click()
    await acceptance.usable()
    assert (await selected())["bank"] == scope["bank"]
    assert (await selected())["mode"] == original_mode
    await until(lambda: frame.locator('[data-mode="readonly"]').is_enabled(), timeout=30)
    await frame.locator('[data-mode="readonly"]').click()
    async def readonly():
        return (await selected())["mode"] == "readonly"
    await until(readonly, timeout=30)
    await acceptance.action(identifier, "maintenance_begin")
    await refresh()
    await frame.locator("#maintenance-view").wait_for(state="visible", timeout=30000)
    await frame.locator("#maintenance-view").click()
    await until(ready, timeout=60)
    await frame.locator("#maintenance-resume").click()
    async def resumed():
        return not (await selected())["summary"]["maintenance"]
    await until(resumed, timeout=30)
    assert (await selected())["mode"] == "readonly"
    await frame.locator('[data-close="maintenance-dialog"]').click()
    await frame.locator('[data-mode="' + original_mode + '"]').click()
    acceptance.passed("browser_readding_confirms_real_identity_preserves_bank_and_pause_resume_preserves_readonly")

    # A newly added user remains in the local list when the post-save read fails.
    failed_reads = 0
    async def fail_first_account_refresh(route):
        nonlocal failed_reads
        if not failed_reads:
            failed_reads += 1
            await route.abort("failed")
        else:
            await route.continue_()
    bob_id = "123997"
    bob_key = json.dumps([scope["platform_instance_id"], bob_id], separators=(",", ":"))
    await frame.locator("#add-account").click()
    await frame.locator("#account-platform").select_option(scope["platform_instance_id"])
    await frame.locator("#account-user").fill(bob_id)
    await page.route(pattern, fail_first_account_refresh)
    try:
        await frame.locator("#save-account").click()
        await frame.locator("#authorization-dialog").wait_for(state="visible", timeout=30000)
        assert bob_key in await frame.locator("#scope-picker option").evaluate_all("nodes => nodes.map(node => node.value)")
    finally:
        await page.unroute(pattern, fail_first_account_refresh)
    assert failed_reads == 1
    await frame.locator("#confirm-authorization").click()
    await acceptance.usable()
    bob = next(row for row in (await acceptance.bootstrap())["scopes"] if row["user_id"] == bob_id)
    native_headers = {"Authorization": "Bearer " + acceptance.settings["memory_key"]}
    bob_base = acceptance.settings["hindsight_url"] + "/v1/default/banks/" + bob["bank"]
    async def create_bob_bank():
        async with acceptance.session.put(bob_base, headers=native_headers, json={"name": "Owned recovery fixture"}) as response:
            assert response.status in (200, 201)
    await create_bob_bank()
    with sqlite3.connect(control_path) as control:
        control.execute("UPDATE banks SET state='established' WHERE bank=?", (bob["bank"],))
    async with acceptance.session.delete(bob_base, headers=native_headers) as response:
        assert response.status in (200, 204)
    await asyncio.sleep(1.1)
    await acceptance.bot.chat(int(bob_id), "An owned Bank recovery check. Reply briefly.")
    async def missing_bob():
        row = next(row for row in (await acceptance.bootstrap())["scopes"] if row["scope_id"] == bob["scope_id"])
        return row if row["summary"]["bank_missing"] else None
    detected = await until(missing_bob, timeout=60)
    assert not detected["summary"]["maintenance"]
    await refresh()
    await frame.locator("#scope-picker").select_option(bob_key)
    await frame.locator("#maintenance-view").wait_for(state="visible", timeout=30000)
    await frame.locator("#remove-account").click()
    await frame.locator("#confirm-remove-account").click()
    await frame.locator("#show-paused-banks").wait_for(state="visible", timeout=30000)
    assert bob_key not in await frame.locator("#scope-picker option").evaluate_all("nodes => nodes.map(node => node.value)")
    await frame.locator("#show-paused-banks").click()
    assert await frame.locator("#maintenance-bank-check").is_visible()
    assert await frame.locator("#maintenance-forget").is_visible()
    await frame.locator("#maintenance-bank-check").click()
    await frame.locator("#maintenance-feedback.error").wait_for(state="visible", timeout=30000)
    assert "原记忆库仍不存在" in await frame.locator("#maintenance-feedback").inner_text()
    assert (await missing_bob())["summary"]["bank_missing"]
    await create_bob_bank()
    await frame.locator("#maintenance-bank-check").click()
    async def recovered_bob():
        return not next(row for row in (await acceptance.bootstrap())["scopes"] if row["scope_id"] == bob["scope_id"])["summary"]["bank_missing"]
    await until(recovered_bob, timeout=30)
    assert await frame.locator("#maintenance-resume").is_hidden()
    await frame.locator("#maintenance-forget").click()
    await frame.locator("#forget-agree").check()
    await frame.locator("#confirm-forget").click()
    async def cleared_bob():
        return next(row for row in (await acceptance.bootstrap())["scopes"] if row["scope_id"] == bob["scope_id"])["bank"] != bob["bank"]
    await until(cleared_bob, timeout=60)
    await frame.locator('[data-close="maintenance-dialog"]').click()
    assert (await selected())["bank"] == scope["bank"]
    assert await acceptance.config() == configuration
    acceptance.passed("saved_user_survives_failed_read_and_removed_missing_bank_is_rechecked_and_cleared_without_reauthorization")

    # Generate a native Knowledge Page and save/disable its reference through the real panel.
    native_base = acceptance.settings["hindsight_url"] + "/v1/default/banks/" + scope["bank"]
    native_headers = {"Authorization": "Bearer " + acceptance.settings["memory_key"]}
    async with acceptance.session.post(native_base + "/knowledge-base/pages", headers=native_headers, json={
        "name": "Panel knowledge", "source_query": "Summarize the user's tea preference in one short sentence.",
        "max_tokens": 128, "trigger": {"refresh_after_consolidation": False, "fact_types": ["world"]},
    }) as response:
        assert response.status in (200, 201, 202), response.status
        created = await response.json()
    async def page_generated():
        async with acceptance.session.get(native_base + "/operations/" + created["operation_id"], headers=native_headers) as response:
            value = await response.json()
            return value.get("status") == "completed"
    await until(page_generated, timeout=180)
    await reload_panel()
    await frame.locator('#background-page option[value="' + created["page_id"] + '"]').wait_for(state="attached", timeout=30000)
    await frame.locator("#background-page").select_option(created["page_id"])
    async def knowledge_enabled():
        return (await selected())["background"]["enabled"]
    await until(knowledge_enabled, timeout=30)
    await until(lambda: frame.locator("#background-page").is_enabled(), timeout=30)
    await frame.locator("#background-page").select_option("")
    async def knowledge_disabled():
        saved = (await selected())["background"]
        return not saved["enabled"] and saved["page_id"] == created["page_id"]
    await until(knowledge_disabled, timeout=30)
    async with acceptance.session.get(native_base + "/knowledge-base/pages/" + created["page_id"], headers=native_headers) as response:
        assert response.status == 200
    acceptance.passed("native_knowledge_page_auto_load_selection_and_disable_keep_official_page")

    # Connection drafts survive polling and config conflicts; reload fails without losing them.
    await frame.locator("#tab-setup").click()
    managed = (await acceptance.bootstrap())["config"]["managed"]
    await frame.locator("#control-plane-url").fill("https://draft-panel.invalid")
    if managed:
        assert await frame.locator("#api-key").is_hidden()
    else:
        await frame.locator("#api-key").fill("owned-unsaved-key")
    await refresh()
    await page.wait_for_timeout(1500)
    assert await frame.locator("#control-plane-url").input_value() == "https://draft-panel.invalid"
    assert await frame.locator("#api-key").input_value() == ("" if managed else "owned-unsaved-key")
    stale_revision = (await acceptance.bootstrap())["config_revision"]
    await acceptance.config(control_plane_url="http://127.0.0.1:9988")
    await refresh()
    await frame.locator("#settings-conflict").wait_for(state="visible", timeout=30000)
    async with acceptance.session.post(acceptance.dashboard + acceptance.prefix + "/settings/save",
                                      json={"config_revision": stale_revision},
                                      headers={"Authorization": "Bearer " + acceptance.token}) as response:
        assert (await response.json()).get("status") == "error"
    async def fail_bootstrap(route):
        await route.abort("failed")
    await page.route(pattern, fail_bootstrap)
    try:
        await frame.locator("#reload-settings").click()
        await frame.locator("#feedback.error").wait_for(state="visible", timeout=30000)
        assert await frame.locator("#control-plane-url").input_value() == "https://draft-panel.invalid"
    finally:
        await page.unroute(pattern, fail_bootstrap)
    await until(lambda: frame.locator("#reload-settings").is_enabled(), timeout=30)
    await frame.locator("#reload-settings").click()
    await frame.locator("#settings-conflict").wait_for(state="hidden", timeout=30000)
    assert await frame.locator("#control-plane-url").input_value() == "http://127.0.0.1:9988"
    assert await frame.locator("#api-key").input_value() == ""
    await acceptance.config(control_plane_url=configuration.get("control_plane_url", ""))
    await reload_panel()
    acceptance.passed("connection_drafts_survive_polling_conflicts_failed_reload_and_stale_save_is_rejected")

    version_override = {}
    async def version_fixture(route):
        response = await route.fetch()
        value = await response.json()
        state = value.get("data", value)
        state["config"]["managed"] = version_override["managed"]
        ignored = state["backend"].get("ignored", False)
        if version_override.get("connection"):
            state["config"]["connection_revision"] = version_override["connection"]
            ignored = False
        state["backend"] = {
            "version": version_override["version"], "target": "1.2.0", "managed": version_override["managed"],
            "upgrade_available": version_override["version"] == "1.1.0", "pending": False,
            "upgrade_paused": version_override["finishing"], "resume_available": version_override["finishing"],
            "ignored": ignored,
        }
        await route.fulfill(response=response, json=value)
    await page.route(pattern, version_fixture)
    try:
        for version, managed, finishing in [("1.1.0", True, False), ("1.1.0", False, False),
                                             ("1.2.0", True, False), (None, True, False), ("1.2.0", True, True)]:
            version_override.update(version=version, managed=managed, finishing=finishing)
            await reload_panel()
            available = managed and (version == "1.1.0" or finishing)
            assert await frame.locator("#upgrade-notice").is_visible() == available
            assert await frame.locator("#ignore-upgrade").is_visible() == (available and not finishing)
            await frame.locator("#tab-setup").click()
            assert await frame.locator("#go-upgrade").is_visible() == (managed and available)
            if available:
                await frame.locator("#view-upgrade").click()
                assert await frame.locator("#upgrade-dialog").is_visible()
                await frame.locator('[data-close="upgrade-dialog"]').click()
        version_override.update(version="1.1.0", managed=True, finishing=False)
        await reload_panel()
        await frame.locator("#ignore-upgrade").click()
        await frame.locator("#upgrade-notice").wait_for(state="hidden", timeout=30000)
        await reload_panel()
        assert await frame.locator("#upgrade-notice").is_hidden()
        assert (await acceptance.http("GET", acceptance.prefix + "/service/status"))["ignored"]
        version_override.update(version="1.2.0", finishing=True)
        await reload_panel()
        assert await frame.locator("#upgrade-notice").is_visible()
        version_override.update(version="1.1.0", finishing=False, connection="owned-other")
        await reload_panel()
        assert await frame.locator("#upgrade-notice").is_visible()
    finally:
        await page.unroute(pattern, version_fixture)
        await reload_panel()
    acceptance.passed("real_dom_upgrade_matrix_ignore_is_connection_scoped_and_unfinished_upgrade_cannot_hide")

    for label, width, height in [("desktop", 1280, 900), ("phone", 390, 844)]:
        await page.set_viewport_size({"width": width, "height": height})
        await frame.locator("#tab-memories").click()
        for theme in ("light", "dark"):
            await frame.locator("body").evaluate("(_, theme) => document.documentElement.dataset.theme = theme", theme)
            for tab in ("memories", "setup"):
                await frame.locator("#tab-" + tab).click()
                if tab == "memories":
                    expected = (await selected())["summary"]["saved_count"]
                    assert await frame.locator("#saved-count").inner_text() == str(expected)
                assert await frame.locator("details").count() == 0
                assert await frame.locator("body").evaluate("node => node.scrollWidth <= innerWidth + 1")
                await page.screenshot(path=str(ROOT / ("panel-" + label + "-" + tab + "-" + theme + ".png")), full_page=True)
                if tab == "memories":
                    await frame.locator(".stats").scroll_into_view_if_needed()
                    await page.screenshot(path=str(ROOT / ("panel-" + label + "-counts-" + theme + ".png")), full_page=True)
    await page.set_viewport_size({"width": 1280, "height": 900})
    await frame.locator("body").evaluate("() => document.documentElement.dataset.theme = 'light'")
    assert (await selected())["bank"] == scope["bank"]
    assert (await selected())["mode"] == original_mode
    # Restore the disabled reference to the pre-test empty choice for deployment invariance checks.
    saved = (await selected())["background"]
    await acceptance.http("POST", acceptance.prefix + "/scopes/" + identifier + "/background", payload={
        "page_id": "", "enabled": False, "revision": saved["revision"], "scope_revision": saved["scope_revision"],
        "config_revision": (await acceptance.bootstrap())["config_revision"],
    })
    assert await acceptance.config() == configuration
    acceptance.passed("desktop_phone_light_dark_layout_has_no_collapsed_content_or_horizontal_overflow")
