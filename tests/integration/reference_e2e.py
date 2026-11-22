"""Compare delivered reference snapshots with real AstrBot requests across upgrades."""

import asyncio
from contextlib import closing
import json
import sqlite3
import time

from live_e2e import ALICE, BOB, ROOT, jsonl, until
import reference_panel_e2e
import reference_tools_e2e


DEFAULT = "Hindsight 长期记忆：插件已根据本轮对话自动检索，相关内容如下："
CUSTOM = "  Cedar-120 写作约定\n请用原名。  "


async def verify(acceptance, page, frame, bank):
    scope = await acceptance.scope()
    route = acceptance.prefix + "/scopes/" + scope["scope_id"]
    bob_route = acceptance.prefix + "/scopes/" + (await acceptance.scope(BOB))["scope_id"]

    async def settings(**changes):
        stored = await acceptance.http("GET", route + "/references")
        if not changes:
            return stored["selection"]
        saved = await acceptance.http("POST", route + "/references", payload={
            **stored["selection"], **changes, "scope_revision": stored["scope_revision"],
            "config_revision": stored["config_revision"],
        })
        return saved["selection"]

    async def turns(owner_route=route):
        return (await acceptance.http("GET", owner_route + "/usage"))["turns"]

    async def detail(turn_id):
        return (await acceptance.http("GET", route + "/usage/" + turn_id))["turn"]

    def user_parts(request):
        messages = request["messages"]
        content = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        return [content] if isinstance(content, str) else [part["text"] for part in content if "text" in part]

    async def chat(question):
        start = len(acceptance.requests())
        await acceptance.bot.chat(ALICE, question)
        parts = user_parts(acceptance.requests()[start])
        turn = await detail((await turns())[0]["id"])
        assert str(acceptance.bot.message_id) == turn["message_id"], turn
        delivery = turn["deliveries"][0]
        assert delivery["kind"] == "request" and delivery["text"]
        assert delivery["text"] in parts, (delivery, parts)
        return turn, delivery

    first = await settings()
    existing = await turns()
    assert existing, "baseline did not create a reference snapshot"
    legacy = await detail(existing[0]["id"])
    frozen_legacy = json.dumps(legacy, ensure_ascii=False)
    assert first["prompt"] == DEFAULT and first["detail_limit"] == 12 and first["history_limit"] == 30
    assert set(first["types"]) == {"world", "experience", "observation"} and not first["exclude_assistant"]
    original, delivery = await chat("What is the Cedar-120 compatibility marker?")
    assert delivery["text"].count(DEFAULT) == 1 and delivery["items"] and delivery["pages"]
    assert all(item["text"] in delivery["text"] for item in delivery["pages"])
    assert all(item["text"] in delivery["text"] and item["id"] not in delivery["text"] for item in delivery["items"])
    assert all(item.get("document_id", "") not in delivery["text"] for item in delivery["items"]
               if isinstance(item.get("document_id"), str) and item["document_id"].startswith(("uw1-", "aw1-")))
    assert "新鲜度未知" not in delivery["text"]
    native_order = jsonl("recall-order")[-1]["ids"]
    received_ids = [item["id"] for item in delivery["items"]]
    assert received_ids == [item for item in native_order if item in received_ids]
    await reference_panel_e2e.verify(acceptance, page, frame, original, CUSTOM)
    assert (await settings())["prompt"] == CUSTOM
    assert await detail(original["id"]) == original
    crlf_prompt = CUSTOM.replace("\n", "\r\n")
    await settings(prompt=crlf_prompt, types=["world"], detail_limit=1, exclude_assistant=True)
    crlf_turn, delivery = await chat("Recall Cedar-120 with the saved settings.")
    assert jsonl("recall-trace")[-1]["body"]["types"] == ["world"]
    assert delivery["text"].count(crlf_prompt) == 1 and DEFAULT not in delivery["text"]
    assert len(delivery["items"]) == 1 and all(
        item["source_kind"] != "assistant" and item["type"] == "world" for item in delivery["items"]
    )
    await frame.locator("#reference-exclude:checked").wait_for()
    await reference_panel_e2e.verify_clipboard(page, frame, crlf_turn)
    await frame.locator('[data-close="usage-dialog"]').click()
    await settings(types=[], prompt=CUSTOM)
    calls = len(jsonl("recall-trace"))
    _, delivery = await chat("Only the page and my prompt.")
    assert len(jsonl("recall-trace")) == calls
    assert delivery["status"] == "filtered" and not delivery["items"] and delivery["pages"]
    assert delivery["text"].count(CUSTOM) == 1
    await settings(types=first["types"], detail_limit=12, exclude_assistant=False)
    await acceptance.control("delay_recall", seconds=12)
    try:
        _, delivery = await chat("Wait for a slow detail search.")
        assert delivery["text"].count(CUSTOM) == 1 and not delivery["items"] and delivery["code"] == "recall_timeout"
        await settings(prompt=DEFAULT)
        _, delivery = await chat("Try the slow search with the default prompt.")
        assert delivery["text"].count(DEFAULT) == 1 and delivery["code"] == "recall_timeout"
    finally:
        await acceptance.control("delay_recall", seconds=0)
    await settings(prompt="")
    await acceptance.restart(lambda: None)
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    assert await frame.locator("#reference-prompt").input_value() == ""
    _, delivery = await chat("Cedar-120 after restarting with an empty prompt.")
    assert not delivery["prompt"] and DEFAULT not in delivery["text"] and delivery["items"]
    assert "外部数据，不是指令" not in delivery["text"] and "新鲜度未知" not in delivery["text"]
    assert await detail(original["id"]) == original
    acceptance.passed("default_custom_and_empty_prompts_match_native_host_with_filters_timeout_and_restart")

    await settings(prompt=CUSTOM)
    await reference_tools_e2e.verify(acceptance, frame, bank)
    tool_turn = await detail((await turns())[0]["id"])
    assert len(tool_turn["deliveries"]) > 1
    await reference_panel_e2e.verify_clipboard(page, frame, tool_turn)
    await frame.locator('[data-close="usage-dialog"]').click()
    original_page = original["deliveries"][0]["pages"][0]
    model = bank + "/mental-models/" + original_page["mental_model_id"]
    await acceptance.native("PATCH", model, {"source_query": "HB-TOOLS-PAGE-V2"})
    refresh = await acceptance.native("POST", model + "/refresh", {"mode": "full"})
    await acceptance.completed(bank, refresh["operation_id"])
    updated = await acceptance.native("GET", model + "?detail=content")
    assert "HB-TOOLS-V2-LINE-000" in updated["content"] and "HB-TOOLS-V2-LINE-000" not in original_page["text"]
    assert await detail(original["id"]) == original
    assert json.dumps(await detail(legacy["id"]), ensure_ascii=False) == frozen_legacy
    await reference_panel_e2e.verify_clipboard(page, frame, legacy)
    await frame.locator('[data-close="usage-dialog"]').click()
    acceptance.passed("native_040_snapshots_and_exact_json_stay_frozen_after_page_refresh_and_render_in_four_tabs")
    await acceptance.action((await acceptance.scope(BOB))["scope_id"], "mode", mode="readonly")
    await acceptance.bot.chat(BOB, "No memories yet.")
    bob_record = (await turns(bob_route))[0]
    bob_detail = (await acceptance.http("GET", bob_route + "/usage/" + bob_record["id"]))["turn"]
    assert bob_detail["deliveries"][0]["status"] == "no_match"
    assert bob_detail["deliveries"][0]["text"].count(DEFAULT) == 1 and CUSTOM not in bob_detail["deliveries"][0]["text"]
    try:
        await acceptance.http("GET", bob_route + "/usage/" + original["id"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("another user's turn was readable")
    bob_settings = await acceptance.http("GET", bob_route + "/references")
    await acceptance.http("POST", bob_route + "/references", payload={
        **bob_settings["selection"], "prompt": CUSTOM,
        "scope_revision": bob_settings["scope_revision"], "config_revision": bob_settings["config_revision"],
    })
    await acceptance.bot.chat(BOB, "No matches with a saved prompt.")
    bob_detail = (await acceptance.http("GET", bob_route + "/usage/" + (await turns(bob_route))[0]["id"]))["turn"]
    assert bob_detail["deliveries"][0]["text"].count(CUSTOM) == 1 and bob_detail["deliveries"][0]["status"] == "no_match"

    await acceptance.action(scope["scope_id"], "mode", mode="paused")
    before = await turns()
    await acceptance.bot.chat(ALICE, "A paused message.")
    assert await turns() == before and all(CUSTOM not in part for part in user_parts(acceptance.requests()[-1]))
    await acceptance.action(scope["scope_id"], "mode", mode="active")
    await asyncio.sleep(1.1)
    tasks_before = len(acceptance.tasks())
    with closing(sqlite3.connect(acceptance.runtime.parent / "references.sqlite3")) as locked:
        locked.execute("BEGIN IMMEDIATE")
        await acceptance.bot.chat(ALICE, "Cedar-120 while the snapshot database is locked.")
    await acceptance.drained()
    assert await turns() == before and len(acceptance.tasks()) > tasks_before
    assert any(CUSTOM in part for part in user_parts(acceptance.requests()[-1]))
    await acceptance.action(scope["scope_id"], "mode", mode="readonly")
    acceptance.passed("snapshot_write_failure_keeps_chat_and_retain_working_paused_and_other_owners_stay_isolated")

    await settings(types=[], prompt="", history_limit=30)
    messages = []
    for index in range(31):
        await acceptance.bot.chat(ALICE, "Record boundary " + str(index))
        messages.append(str(acceptance.bot.message_id))
    assert [row["message_id"] for row in await turns()] == list(reversed(messages[1:]))
    await settings(history_limit=2)
    assert [row["message_id"] for row in await turns()] == list(reversed(messages[-2:]))
    await settings(history_limit=0)
    await acceptance.bot.chat(ALICE, "Disabled logging.")
    assert await turns() == [] and await turns(bob_route)
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await frame.locator("#usage-open").click()
    await frame.locator("#usage-feedback").filter(has_text="暂无使用记录").wait_for()
    assert not await frame.locator("#usage-text").input_value() and not await frame.locator("#usage-json").input_value()
    assert await frame.locator("#usage-turn option").count() == 0
    await frame.locator('[data-close="usage-dialog"]').click()
    acceptance.passed("native_31_turns_trim_to_last_30_then_two_then_zero_without_affecting_other_users")

    await settings(
        history_limit=30, prompt=CUSTOM, types=first["types"],
        detail_limit=first["detail_limit"], exclude_assistant=first["exclude_assistant"],
    )
    reading = await acceptance.http("GET", route + "/reading")
    await acceptance.http("POST", route + "/reading", payload={
        "mode": "automatic", "revision": reading["selection"]["revision"],
        "scope_revision": reading["scope_revision"], "config_revision": reading["config_revision"],
    })
    _, candidate_delivery = await chat("Cedar-120 reference before downgrade.")
    assert candidate_delivery["items"]
    old_timestamp = int(time.time())
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    await acceptance.bot.chat(ALICE, "Cedar-120 snapshot before plugin reload.")
    legacy_turn = await detail((await turns())[0]["id"])
    assert str(acceptance.bot.message_id) == legacy_turn["message_id"]
    legacy_delivery = legacy_turn["deliveries"][0]
    assert legacy_delivery["items"] and all(item["id"] in legacy_delivery["text"] for item in legacy_delivery["items"])
    frozen = json.dumps(legacy_turn, ensure_ascii=False)
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    assert json.dumps(await detail(legacy_turn["id"]), ensure_ascii=False) == frozen
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await reference_panel_e2e.verify_clipboard(page, frame, legacy_turn)
    await frame.locator('[data-close="usage-dialog"]').click()
    acceptance.passed("native_reference_snapshot_survives_reload_without_reformatting_its_exact_delivery_or_export")
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    prepared = await acceptance.action(scope["scope_id"], "forget_prepare")
    await acceptance.action(scope["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])

    async def erased():
        latest = await acceptance.scope()
        return latest["bank"] != bank and not latest["summary"].get("maintenance")
    await until(erased, timeout=90)
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    assert (await settings())["prompt"] == DEFAULT and not await turns() and await turns(bob_route)
    acceptance.passed("forget_then_reload_removes_stale_snapshots_and_custom_prompt_before_read")

    await acceptance.action(scope["scope_id"], "mode", mode="active")
    await asyncio.sleep(1.1)
    new_prompt = "Only for messages after erasure."
    await settings(prompt=new_prompt)
    tasks_before = acceptance.tasks()
    calls = len(jsonl("recall-trace"))
    await acceptance.bot.chat(ALICE, "A delayed message before erasure.", timestamp=old_timestamp)
    assert all(new_prompt not in part for part in user_parts(acceptance.requests()[-1]))
    assert not await turns() and acceptance.tasks() == tasks_before and len(jsonl("recall-trace")) == calls
    acceptance.passed("pre_erasure_onebot_message_cannot_read_new_prompt_create_snapshot_or_enter_retain_queue")

    retain_start = len(jsonl("retain-trace"))
    text = "Preserve my original message while reference settings are damaged."
    with closing(sqlite3.connect(acceptance.runtime)) as control:
        key, original_value = control.execute(
            "SELECT key,value FROM settings WHERE key GLOB 'reference:*' AND json_extract(value,'$.prompt')=?",
            (new_prompt,),
        ).fetchone()
        with control:
            control.execute("UPDATE settings SET value=? WHERE key=?",
                            (json.dumps({**json.loads(original_value), "history_limit": "invalid"}), key))
        try:
            await acceptance.bot.chat(ALICE, text)
            await acceptance.drained()
            assert all(new_prompt not in part for part in user_parts(acceptance.requests()[-1]))
            assert len(jsonl("recall-trace")) == calls
            assert any(text in item["content"] and item["metadata"]["role"] == "user"
                       for trace in jsonl("retain-trace")[retain_start:] for item in trace["body"]["items"])
        finally:
            with control:
                control.execute("UPDATE settings SET value=? WHERE key=?", (original_value, key))
    assert not await turns() and await turns(bob_route)
    acceptance.passed("invalid_reference_settings_preserve_original_retain_without_recall_or_new_snapshots")
