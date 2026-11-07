"""Actual Agent tool loops, native read APIs, persistence and owner modes."""

import asyncio
import json
from pathlib import Path
import zipfile

from live_e2e import ALICE, BOB, NAME, ROOT, database, jsonl, until

PREFIX = "hindsight_bridge_"


def results(requests):
    values = []
    for request in requests:
        messages = request.get("messages", [])
        last_user = next((index for index in range(len(messages) - 1, -1, -1)
                          if messages[index].get("role") == "user"), -1)
        for message in messages[last_user + 1:]:
            if message.get("role") != "tool":
                continue
            raw = message.get("content", "")
            if isinstance(raw, list):
                raw = "".join(part.get("text", "") for part in raw if isinstance(part, dict))
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value not in values:
                values.append(value)
    return values


async def verify(acceptance, page, frame, bank):
    scope = await acceptance.scope()
    identifier = scope["scope_id"]
    before_options = scope["reading"]
    assert before_options["mode"] == "automatic"
    await acceptance.bot.chat(ALICE, "A synthetic capability observation after upgrade.")

    async def available():
        return (await acceptance.scope())["reading"]["observed_available"]
    await until(available, timeout=15)
    assert not any(tool["function"]["name"].startswith(PREFIX)
                   for tool in acceptance.requests()[-1].get("tools", []))
    acceptance.passed("native_upgrade_keeps_automatic_recall_and_hides_owned_tools_by_default")

    selected = await acceptance.page(bank, "HB-TOOLS-PAGE")
    tree = await acceptance.native("GET", bank + "/knowledge-base/tree")
    nodes = list(tree["roots"])
    model_id = None
    while nodes:
        node = nodes.pop()
        if node["id"] == selected:
            model_id = node["mental_model_id"]
        nodes.extend(node.get("children", []))
    assert model_id
    native = await acceptance.native("GET", bank + "/mental-models/" + model_id + "?detail=content")
    body = native["content"]
    assert "HB-TOOLS-V1-LINE-000" in body and len(body.splitlines()) >= 200
    assert (await acceptance.scope())["background"]["page_id"] != selected
    await frame.locator("#reading-mode option[value='tools']").wait_for(state="attached")
    await until(lambda: frame.locator("#reading-mode option[value='tools']").is_enabled(), timeout=15)
    await frame.locator("#reading-mode").select_option("tools")

    async def mode_is_tools():
        return (await acceptance.scope())["reading"]["mode"] == "tools"
    await until(mode_is_tools, timeout=15)
    assert (await acceptance.scope())["bank"] == bank
    acceptance.passed("native_panel_enables_owner_tool_mode_without_changing_bank_or_memory_mode")

    async def chat(text):
        start = len(acceptance.requests())
        answer = await acceptance.bot.chat(ALICE, text)
        assert "HB-TOOLS-ANSWER" in answer, answer
        return acceptance.requests()[start:]

    pending = len(acceptance.tasks())
    recalls = len(jsonl("recall-trace"))
    flow = await chat("HB-TOOLS-FLOW")
    values = results(flow)
    read = next(value for value in values if value.get("kind") == "page")
    assert read["page_id"] == selected and read["start_line"] == 1 and read["next_line"] is not None
    assert read["content"] == "".join(body.splitlines(keepends=True)[:read["end_line"]])
    assert any(value.get("kind") == "search_pages" for value in values)
    assert any(value.get("kind") == "recall" for value in values)
    assert len(jsonl("recall-trace")) == recalls + 1
    assert (await acceptance.scope())["reading"]["status"]["calls"] == 3
    assert len(acceptance.tasks()) == pending
    acceptance.report["measurements"]["first_excerpt"] = {key: read[key] for key in (
        "page_id", "version", "start_line", "end_line", "total_lines", "next_line",
    )}
    acceptance.passed("real_agent_searches_unselected_native_page_reads_whole_lines_and_recalls_without_automatic_recall_or_readonly_capture")

    continued = results(await chat("HB-TOOLS-CONTINUE"))
    chunks = [value for value in continued if value.get("kind") == "page" and value["start_line"] > 1]
    assert len(chunks) == 2 and chunks[0]["start_line"] == read["next_line"]
    assert chunks[1]["start_line"] == chunks[0]["next_line"]
    assert all(value["version"] == read["version"] for value in chunks)
    acceptance.passed("actual_agent_continuation_reuses_version_and_next_line_under_one_round_budget")

    await acceptance.native("PATCH", bank + "/mental-models/" + model_id, {"source_query": "HB-TOOLS-PAGE-V2"})
    refreshed = await acceptance.native("POST", bank + "/mental-models/" + model_id + "/refresh", {"mode": "full"})
    await acceptance.completed(bank, refreshed["operation_id"])
    updated = await acceptance.native("GET", bank + "/mental-models/" + model_id + "?detail=content")
    assert "HB-TOOLS-V2-LINE-000" in updated["content"]
    stale = results(await chat("HB-TOOLS-STALE"))
    assert any(value.get("error") == "read_version_changed" for value in stale)
    assert not any(value.get("kind") == "page" for value in stale)
    acceptance.passed("native_refresh_changes_content_version_and_actual_agent_cannot_append_old_version")

    sources = results(await chat("HB-TOOLS-SOURCES"))
    source = next(value for value in sources if value.get("kind") == "page_sources")
    history = next(value for value in sources if value.get("kind") == "page_history")
    assert source["page_id"] == selected and history["page_id"] == selected
    assert history["complete_history"] is False and history["retained_count"] >= 1
    assert all(item.get("kind") in {"version", "refresh_failed"} for item in history["items"])
    assert "previous_reflect_response" not in json.dumps(history) and "error_message" not in json.dumps(history)
    acceptance.passed("agent_reads_native_sources_and_bounded_history_with_explicit_scope_and_no_raw_failure_payload")

    quota = await chat("HB-TOOLS-QUOTA")
    status = (await acceptance.scope())["reading"]["status"]
    assert status["state"] == "budget" and status["calls"] <= 4, status
    assert any(value.get("error") == "tool_budget_exhausted" for value in results(quota))
    assert not any(tool["function"]["name"].startswith(PREFIX) for tool in quota[-1].get("tools", []))
    acceptance.passed("all_native_tools_share_the_original_inbound_call_quota")

    probe_name = "astrbot_plugin_bridge_reading_guard_probe"
    with zipfile.ZipFile(ROOT / "reading-guard-probe.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(probe_name + "/main.py", Path(__file__).with_name("reading_probe.py").read_bytes())
        archive.writestr(probe_name + "/metadata.yaml", "name: " + probe_name + "\ndesc: Native reading boundary probe\nversion: 1.0.0\nauthor: fixture\n")
    await acceptance.install("reading-guard-probe.zip")
    try:
        boundary_recalls = len(jsonl("recall-trace"))
        guarded = await chat("HB-TOOLS-GUARDS")
        guard_results = results(guarded)
        assert any(value.get("error") == "tool_context_unavailable" for value in guard_results)
        assert any(value.get("kind") == "page" for value in guard_results)
        assert len(jsonl("recall-trace")) == boundary_recalls + 1
        probe = json.loads((ROOT / "reading-guard-probe.json").read_text())
        assert probe["secondary_agent_cannot_claim_or_consume_main_ledger"]
        assert probe["finished_agent_cannot_reuse_ledger"]
        await chat("HB-TOOLS-IDLE")
        probe = json.loads((ROOT / "reading-guard-probe.json").read_text())
        assert probe["unused_agent_ledger_is_closed_by_host_completion"]
        assert (await acceptance.scope())["reading"]["status"]["calls"] == 0
        assert len(jsonl("recall-trace")) == boundary_recalls + 1
        acceptance.passed("native_secondary_agent_cannot_claim_main_ledger_and_finished_agent_cannot_reuse_it")

        boundary_recalls = len(jsonl("recall-trace"))
        changed = await chat("HB-TOOLS-REQUEST-MODEL")
        assert changed[-1]["model"] == "fixture-late-request-model"
        assert not any(tool["function"]["name"].startswith(PREFIX) for tool in changed[-1].get("tools", []))
        assert len(jsonl("recall-trace")) == boundary_recalls
        assert (await acceptance.scope())["reading"]["status"]["code"] == "reading_changed"
        assert not (await acceptance.scope())["reading"]["observed_available"]
        acceptance.passed("late_native_request_hook_model_change_revokes_tools_without_memory_delivery")
    finally:
        await acceptance.http("DELETE", "/api/v1/plugins/by-id?plugin_id=" + probe_name)

    await acceptance.bot.chat(BOB, "A synthetic second user remains in automatic mode.")
    assert not any(tool["function"]["name"].startswith(PREFIX) for tool in acceptance.requests()[-1].get("tools", []))
    assert (await acceptance.scope(BOB))["reading"]["mode"] == "automatic"
    assert (await acceptance.scope())["reading"]["mode"] == "tools"
    await acceptance.action(identifier, "mode", mode="paused")
    await acceptance.bot.chat(ALICE, "A synthetic paused conversation.")
    assert not any(tool["function"]["name"].startswith(PREFIX) for tool in acceptance.requests()[-1].get("tools", []))
    await acceptance.action(identifier, "mode", mode="readonly")
    acceptance.passed("per_user_modes_and_pause_filter_only_owned_request_tools")

    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await frame.locator("#reading-mode").select_option("automatic")

    async def automatic():
        return (await acceptance.scope())["reading"]["mode"] == "automatic"
    await until(automatic, timeout=15)
    await acceptance.bot.chat(ALICE, "A synthetic automatic recall after disabling tools.")
    assert not any(tool["function"]["name"].startswith(PREFIX) for tool in acceptance.requests()[-1].get("tools", []))
    await frame.locator("#reading-mode").select_option("tools")
    await until(mode_is_tools, timeout=15)
    acceptance.passed("native_panel_can_switch_back_to_automatic_and_reenable_tools_without_reloading")

    await acceptance.action(identifier, "mode", mode="active")
    await asyncio.sleep(1.1)
    start = len(jsonl("retain-trace"))
    await chat("HB-TOOLS-FLOW")
    await acceptance.drained()
    additions = [item for trace in jsonl("retain-trace")[start:] for item in trace["body"]["items"]]
    assert {item["metadata"]["role"] for item in additions} == {"user", "assistant"}
    assert any("HB-TOOLS-FLOW" in item["content"] for item in additions if item["metadata"]["role"] == "user")
    await acceptance.action(identifier, "mode", mode="readonly")

    stored = json.dumps(database(acceptance.history, "SELECT content FROM conversations"))
    assert "HB-TOOLS-V1-LINE-000" in stored and PREFIX + "read_page" in stored
    retained = json.dumps(jsonl("retain-trace"))
    assert "HB-TOOLS-V1-LINE-000" not in retained
    assert "HB-TOOLS-V2-LINE-000" not in retained
    await acceptance.control("collect_astrbot_logs")
    native_logs = (ROOT / "astrbot-current.log").read_text()
    assert "HB-TOOLS-V1-LINE-000" in native_logs and "Tool `" in native_logs
    acceptance.report["measurements"]["host_copies"] = {
        "conversation_tool_results": True, "host_tool_logs": True,
        "log_scope": "Docker stdout/stderr; external collectors are outside this fixture",
    }
    acceptance.passed("native_host_persists_tool_history_and_logs_while_bridge_retains_only_original_material")

    await acceptance.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
    await acceptance.usable()
    assert (await acceptance.scope())["reading"]["mode"] == "tools"
    await chat("HB-TOOLS-FLOW")
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    assert await frame.locator("#reading-mode").input_value() == "tools"
    acceptance.passed("native_reload_keeps_owner_setting_and_rebinds_working_tools")

    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])

    async def erased():
        value = await acceptance.scope()
        return value if value["bank"] != bank and not value["summary"].get("maintenance") else None
    latest = await until(erased, timeout=90)
    assert latest["reading"]["mode"] == "automatic" and latest["reading"]["invalidated"]
    assert latest["reading"]["revision"] > before_options["revision"]
    assert "HB-TOOLS-V1-LINE-000" in json.dumps(database(acceptance.history, "SELECT content FROM conversations"))
    acceptance.passed("forget_resets_tool_settings_and_keeps_host_tool_history_explicitly_outside_erasure")
