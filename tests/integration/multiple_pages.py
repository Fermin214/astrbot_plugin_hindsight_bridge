"""Native ordered selection, shared budgets and upgrade using synthetic peers."""

import json
import time

from live_e2e import ALICE, NAME, database, until
from parameter_panel import save_parameters


async def verify_recovery(acceptance, page, frame, bank, short):
    initial = await acceptance.scope()
    route = acceptance.prefix + "/scopes/" + initial["scope_id"] + "/background"
    ids = [short, await acceptance.page(bank, "Recovery second page"),
           await acceptance.page(bank, "Recovery third page")]
    await frame.locator("#background-recheck").click()

    async def selection_is(expected):
        value = (await acceptance.scope())["background"]
        return value if [item["page_id"] for item in value["pages"]] == expected else None

    for position, identity in enumerate(ids[1:], 1):
        await frame.locator(f'#background-page option[value="{identity}"]').wait_for(state="attached", timeout=20000)
        await until(lambda: frame.locator("#background-page").is_enabled(), timeout=20)
        await frame.locator("#background-page").select_option(identity)
        await frame.locator("#background-save").click()
        await until(lambda: selection_is(ids[:position + 1]), timeout=20)
    await frame.locator("#background-selected > li").nth(2).wait_for()
    for identity in ids:
        await acceptance.native("DELETE", bank + "/knowledge-base/nodes/" + identity)
    await frame.locator("#background-selected > li").nth(0).get_by_role("button", name="移除第 1 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selection_is(ids[1:]), timeout=20)
    assert (await acceptance.scope())["background"]["enabled"]
    acceptance.passed("enabled_selection_can_remove_one_deleted_page_while_other_pages_are_also_deleted")

    await until(lambda: frame.locator("#background-page").is_enabled(), timeout=20)
    await frame.locator("#background-page").select_option("")
    await frame.locator("#background-save").click()

    async def disabled():
        return not (await acceptance.scope())["background"]["enabled"]
    await until(disabled, timeout=20)
    await frame.locator("#background-selected > li").nth(0).get_by_role("button", name="移除第 1 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selection_is(ids[2:]), timeout=20)
    assert not (await acceptance.scope())["background"]["enabled"]
    acceptance.passed("disabled_selection_can_remove_one_deleted_page_without_reading_or_dropping_the_other_reference")

    before = await acceptance.http("GET", route)
    try:
        await acceptance.http("POST", route, payload={
            "page_ids": ["unknown-page"], "enabled": True,
            "revision": before["selection"]["revision"], "scope_revision": before["scope_revision"],
            "config_revision": before["config_revision"],
        })
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unknown page was accepted")
    assert (await acceptance.http("GET", route))["selection"] == before["selection"]
    await until(lambda: frame.locator("#background-page").is_enabled(), timeout=20)
    await frame.locator("#background-selected > li").nth(0).get_by_role("button", name="移除第 1 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selection_is([]), timeout=20)
    latest = await acceptance.scope()
    assert not latest["background"]["enabled"]
    assert all(latest[key] == initial[key] for key in ("bank", "owner_id", "generation", "mode"))
    assert (await acceptance.native("GET", bank + "/knowledge-base/tree"))["roots"] == []
    acceptance.report["measurements"]["deleted_reference_recovery"] = {
        "original_page_ids": ids, "final_selection": latest["background"], "bank": bank,
    }
    acceptance.passed("last_missing_reference_can_be_removed_unknown_references_are_rejected_and_native_pages_stay_deleted")

    eight = []
    for position in range(8):
        identity, _ = await acceptance.page(bank, f"Eight-page identity timing {position}", wait=False)
        eight.append(identity)
    measurements = []
    acceptance.bot.login_info_delay = 0.3
    try:
        for identities in (eight, list(reversed(eight))):
            snapshot = await acceptance.http("GET", route)
            started = time.monotonic()
            saved = await acceptance.http("POST", route, payload={
                "page_ids": identities, "enabled": True,
                "revision": snapshot["selection"]["revision"], "scope_revision": snapshot["scope_revision"],
                "config_revision": snapshot["config_revision"],
            })
            measurements.append(round(time.monotonic() - started, 3))
            assert [item["page_id"] for item in saved["selection"]["pages"]] == identities
            assert len(saved["checks"]) == 8
    finally:
        acceptance.bot.login_info_delay = 0
    acceptance.report["measurements"]["eight_page_save_and_reorder_seconds_with_300ms_native_identity"] = measurements
    acceptance.passed("eight_pages_save_and_reorder_under_the_eight_second_api_deadline_with_slow_native_identity_queries")


async def verify(acceptance, page, frame, bank, short):
    scope = await acceptance.scope()
    identifier = scope["scope_id"]
    route = acceptance.prefix + "/scopes/" + identifier + "/background"
    selection = scope["background"]
    assert selection["pages"] == [{"page_id": short, "mental_model_id": selection["mental_model_id"]}]
    assert selection["revision"] == 1 and selection["enabled"]
    acceptance.passed("native_028_single_page_upgrade_preserves_bank_selection_and_revision")

    async def selected(ids):
        scope = await acceptance.scope()
        value = scope["background"]
        return value if [item["page_id"] for item in value["pages"]] == ids else None

    async def chat():
        before = (await acceptance.scope())["background"].get("status", {}).get("checked_at", 0)
        calls = len(acceptance.requests())
        await acceptance.bot.chat(ALICE, "A synthetic ordered-page conversation.")

        async def finished():
            status = (await acceptance.scope())["background"].get("status", {})
            return status if status.get("checked_at", 0) > before and len(acceptance.requests()) > calls else None
        status = await until(finished, timeout=35)
        return acceptance.latest_wire(), status

    medium = await acceptance.page(bank, "HB-BUDGET-MEDIUM")
    long = await acceptance.page(bank, "HB-FEEDBACK-LONG")
    await frame.locator("#background-recheck").click()
    await frame.locator(f'#background-page option[value="{medium}"]').wait_for(state="attached", timeout=20000)
    await frame.locator("#background-page").select_option(medium)
    await frame.locator("#background-save").click()
    await until(lambda: selected([short, medium]), timeout=20)
    await until(lambda: frame.locator("#background-page").is_enabled(), timeout=20)
    await frame.locator("#background-page").select_option(long)
    await frame.locator("#background-save").click()
    await until(lambda: selected([short, medium, long]), timeout=20)
    await frame.locator("#background-selected > li").nth(2).wait_for()
    await frame.locator("#background-selected > li").nth(1).get_by_role("button", name="上移第 2 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selected([medium, short, long]), timeout=20)
    checked = await acceptance.http("GET", route)
    assert [item["page_id"] for item in checked["checks"]] == [medium, short, long]
    assert checked["checks"][2]["code"] == "background_too_large"
    acceptance.report["measurements"]["ordered_selection"] = checked
    acceptance.passed("native_dashboard_adds_reorders_and_measures_each_page_without_storing_content")

    wire, status = await chat()
    by_id = {item["page_id"]: item for item in status["pages"]}
    assert by_id[medium]["used"] and by_id[short]["used"]
    assert not by_id[long]["used"] and by_id[long]["code"] == "background_too_large"
    selection = (await acceptance.scope())["background"]
    models = {item["page_id"]: item["mental_model_id"] for item in selection["pages"]}
    assert wire.index(models[medium]) < wire.index(models[short])
    assert "HB-FEEDBACK-LONG complete paragraph." not in wire
    await frame.locator("#background-selected > li").nth(2).filter(has_text="知识页太长").wait_for()
    acceptance.passed("real_chat_injects_whole_pages_in_saved_order_and_reports_skipped_page")

    await save_parameters(acceptance, frame, recall_max_tokens=256)
    wire, status = await chat()
    by_id = {item["page_id"]: item for item in status["pages"]}
    assert not by_id[medium]["used"] and by_id[medium]["code"] == "background_too_large"
    assert by_id[short]["used"]
    assert "neutral neutral" not in wire
    acceptance.passed("shared_budget_skips_large_earlier_page_and_keeps_later_fitting_page")

    await save_parameters(acceptance, frame, recall_max_tokens=1200, recall_timeout=0.4, background_timeout=10)
    await acceptance.control("delay_background", seconds=0.35)
    try:
        wire, status = await chat()
        assert all(item["code"] == "background_timeout" and not item["used"] for item in status["pages"]), status
        assert "Hindsight 知识页" not in wire
    finally:
        await acceptance.control("delay_background", seconds=0)
    await save_parameters(acceptance, frame, recall_timeout=10, background_timeout=1)
    acceptance.passed("all_page_reads_share_the_existing_quarter_deadline_and_leave_recall_available")

    await acceptance.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
    await acceptance.usable()
    assert await selected([medium, short, long])
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await frame.locator("#background-selected > li").nth(2).wait_for()
    await frame.locator("#background-selected > li").nth(2).get_by_role("button", name="移除第 3 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selected([medium, short]), timeout=20)
    retained = await acceptance.native("GET", bank + "/knowledge-base/pages/" + long)
    assert retained["id"] == long
    command = await acceptance.bot.chat(ALICE, "/hindsight background 2")
    assert short in command and "第 2/2 篇" in command
    acceptance.passed("reload_keeps_order_removal_preserves_native_content_and_personal_command_reads_by_position")

    await acceptance.native("DELETE", bank + "/knowledge-base/nodes/" + short)
    wire, status = await chat()
    by_id = {item["page_id"]: item for item in status["pages"]}
    assert by_id[medium]["used"] and by_id[short]["code"] == "background_missing"
    assert models[short] not in wire
    acceptance.passed("deleted_page_has_individual_missing_feedback_while_other_page_is_used")

    await frame.locator("#background-page").select_option("")
    await frame.locator("#background-save").click()

    async def closed():
        return not (await acceptance.scope())["background"]["enabled"]
    await until(closed, timeout=20)
    assert await selected([medium, short])
    await acceptance.bot.chat(ALICE, "A synthetic conversation without selected pages.")
    assert "Hindsight 知识页" not in acceptance.latest_wire()
    assert (await acceptance.native("GET", bank + "/knowledge-base/pages/" + medium))["id"] == medium
    acceptance.passed("disabling_keeps_order_and_native_pages_but_stops_automatic_injection")

    await acceptance.native("DELETE", bank + "/knowledge-base/nodes/" + medium)
    await frame.locator("#background-selected > li").nth(0).get_by_role("button", name="移除第 1 篇知识页", exact=True).click()
    await frame.locator("#background-save").click()
    await until(lambda: selected([short]), timeout=20)
    assert not (await acceptance.scope())["background"]["enabled"]
    await until(lambda: frame.locator("#background-page").is_enabled(), timeout=20)
    await frame.locator("#background-page").select_option(short)
    await frame.locator("#background-save").click()

    async def enabled_missing():
        return (await acceptance.scope())["background"]["enabled"]
    await until(enabled_missing, timeout=20)
    _, status = await chat()
    assert status["pages"][0]["code"] == "background_missing" and not status["pages"][0]["used"]
    acceptance.passed("multiple_deleted_pages_can_be_removed_individually_while_disabled_and_retained_missing_reference_stays_explicit")

    # Inspect only Bridge queues: automatic page bodies must never become material.
    payloads = json.dumps(database(acceptance.runtime, "SELECT payload FROM outbox"))
    assert "HB-FEEDBACK-LONG complete paragraph." not in payloads
    assert "neutral neutral" not in payloads
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await frame.locator("body").evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
    await page.screenshot(path=str(acceptance.runtime.parents[3] / "multiple-pages-phone.png"), full_page=True)
    acceptance.passed("native_phone_layout_has_no_horizontal_overflow_with_ordered_selection")
    await page.set_viewport_size({"width": 1280, "height": 900})
    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])

    async def cleared():
        scope = await acceptance.scope()
        return scope if scope["bank"] != bank and not scope["summary"].get("maintenance") else None
    erased = await until(cleared, timeout=90)
    assert not erased["background"]["pages"] and erased["background"]["invalidated"]
    assert erased["background"]["revision"] > selection["revision"]
    acceptance.passed("forget_clears_all_ordered_references_and_keeps_monotonic_invalidation")
