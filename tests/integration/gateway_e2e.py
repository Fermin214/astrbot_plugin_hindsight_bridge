import asyncio
import hashlib
import json
import time

from live_e2e import ALICE, BOB, NAME, ROOT, database, jsonl, until
from reading_e2e import results


IMAGE = "base64://iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDa8AAAAASUVORK5CYII="


async def verify(acceptance, page, frame, bank):
    scope = await acceptance.scope()
    identifier = scope["scope_id"]
    route = acceptance.prefix + "/scopes/" + identifier
    upgraded = await acceptance.config()
    assert upgraded["recall_timeout"] == 4 and upgraded["background_timeout"] == 1
    acceptance.passed("v050_explicit_timeout_values_survive_candidate_upgrade")
    await acceptance.action(identifier, "mode", mode="active")
    await asyncio.sleep(1.1)

    async def references(**changes):
        stored = await acceptance.http("GET", route + "/references")
        if not changes:
            return stored["selection"]
        return await acceptance.http("POST", route + "/references", payload={
            **stored["selection"], **changes, "scope_revision": stored["scope_revision"],
            "config_revision": stored["config_revision"],
        })

    async def reading(mode):
        stored = await acceptance.http("GET", route + "/reading")
        await acceptance.http("POST", route + "/reading", payload={
            "mode": mode, "revision": stored["selection"]["revision"],
            "scope_revision": stored["scope_revision"], "config_revision": stored["config_revision"],
        })

    async def background(enabled):
        stored = await acceptance.http("GET", route + "/background")
        await acceptance.http("POST", route + "/background", payload={
            "enabled": enabled, "page_ids": [entry["page_id"] for entry in stored["selection"]["pages"]],
            "revision": stored["selection"]["revision"], "scope_revision": stored["scope_revision"],
            "config_revision": stored["config_revision"],
        })

    async def usage():
        return await acceptance.http("GET", route + "/usage")

    async def latest():
        turns = (await usage())["turns"]
        return (await acceptance.http("GET", route + "/usage/" + turns[0]["id"]))["turn"]

    async def reload_summary(expected):
        await acceptance.install("candidate-upgrade.zip")
        await acceptance.usable()
        value = (await acceptance.scope())["summary"]["recall"]
        assert value["persistent"] and value["outcome"] == expected, value
        return value

    async def document(message_id):
        identity = json.loads(database(acceptance.runtime, "SELECT identity_json FROM scopes WHERE scope_id=?", (identifier,))[0]["identity_json"])
        event_key = hashlib.sha256(json.dumps([*identity[:5], str(message_id)], ensure_ascii=False,
                                             separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        task = database(acceptance.runtime, "SELECT o.document_id FROM outbox o JOIN events e ON e.task_id=o.id "
                        "WHERE e.event_key=? AND o.bank=? ORDER BY o.id DESC LIMIT 1", (event_key, bank))[0]
        return await acceptance.native("GET", bank + "/documents/" + task["document_id"])

    await acceptance.bot.chat(ALICE, "HB-GATEWAY capability Cedar-120")
    assert "recall_on_quoted_or_link" not in await references()
    await reading("tools")
    await background(False)
    start = len(acceptance.requests())
    text = "HB-TOOLS-PROBE-gateway Cedar-120 我喜欢无糖咖啡😀"
    await acceptance.bot.chat(ALICE, text, segments=[
        {"type": "text", "data": {"text": text}}, {"type": "face", "data": {"id": "14"}},
        {"type": "image", "data": {"file": IMAGE}},
    ])
    mixed_id = acceptance.bot.message_id
    flow = acceptance.requests()[start:]
    assert results(flow) and any(item.get("kind") == "recall" for item in results(flow)), results(flow)
    await acceptance.drained()
    stored = await document(mixed_id)
    content = json.dumps(stored, ensure_ascii=False)
    assert text in content and "[表情][图片]" in content and "切勿凭空臆测媒体内容" in content
    replies = database(acceptance.runtime, "SELECT r.text FROM replies r JOIN window w ON w.task_id=r.user_task_id "
                       "WHERE w.scope_id=? AND w.text=?", (identifier, text + "[表情][图片]"))
    assert replies and replies[0]["text"] == "HB-TOOLS-ANSWER", replies
    acceptance.passed("native_image_and_face_round_keeps_user_document_tool_calls_and_associated_assistant")

    await acceptance.bot.chat(ALICE, "HB-GATEWAY 引用新话 https://example.invalid @朋友", segments=[
        {"type": "reply", "data": {"id": str(mixed_id)}},
        {"type": "text", "data": {"text": "HB-GATEWAY 引用新话 https://example.invalid @朋友"}},
    ])
    quoted_id = acceptance.bot.message_id
    await acceptance.drained()
    quoted = json.dumps(await document(quoted_id), ensure_ascii=False)
    assert "【引用背景（仅用于理解指代，勿重复抽取）】" in quoted and text in quoted
    assert "UNTRUSTED_NATIVE_QUOTE" not in quoted
    await references(capture_assistant=False)
    await acceptance.bot.chat(ALICE, "HB-GATEWAY 缺失引用仅保存新话", segments=[
        {"type": "reply", "data": {"id": "999999999"}},
        {"type": "text", "data": {"text": "HB-GATEWAY 缺失引用仅保存新话"}},
    ])
    missing_id = acceptance.bot.message_id
    await acceptance.drained()
    missing = json.dumps(await document(missing_id), ensure_ascii=False)
    assert "HB-GATEWAY 缺失引用仅保存新话" in missing and "【引用背景" not in missing
    assert "[assistant;" not in missing
    acceptance.passed("native_quote_and_link_save_new_text_and_only_verified_local_context_without_disabled_assistant")

    await acceptance.bot.chat(ALICE, "HB-GATEWAY 按需未调用")
    await reload_summary("on_demand")
    await background(True)
    await acceptance.bot.chat(ALICE, "HB-GATEWAY 仅知识页")
    await reload_summary("background_only")
    await reading("automatic")
    await background(False)
    await references(types=[])
    await acceptance.bot.chat(ALICE, "HB-GATEWAY 无匹配")
    await reload_summary("empty")
    await references(types=["world", "experience", "observation"])
    await acceptance.config(recall_timeout=0.5)
    await acceptance.usable()
    await acceptance.control("delay_recall", seconds=2)
    await acceptance.bot.chat(ALICE, "HB-GATEWAY 超时")
    await acceptance.control("delay_recall", seconds=0)
    await reload_summary("error")
    acceptance.passed("native_reload_preserves_on_demand_background_only_empty_and_failed_delivery_types")

    await acceptance.config(recall_timeout=15, background_timeout=3)
    await acceptance.usable()
    await background(True)
    measurements = []
    for label, mode, fact_delay, page_delay in (
        ("fast_details_slow_page", "automatic", 0, 4),
        ("slow_details_fast_page", "automatic", 15, 0),
        ("on_demand_page_only", "tools", 0, 2.5),
    ):
        await reading(mode)
        await acceptance.control("delay_recall", seconds=fact_delay)
        await acceptance.control("delay_background", seconds=page_delay)
        start = len(jsonl("recall-trace"))
        began = time.monotonic()
        await acceptance.bot.chat(ALICE, "HB-GATEWAY " + label)
        seconds = time.monotonic() - began
        delivery = (await latest())["deliveries"][0]
        if label == "fast_details_slow_page":
            assert delivery["items"] and not delivery["pages"] and seconds < 5, (seconds, delivery)
        else:
            assert delivery["pages"] and not delivery["items"], delivery
            assert seconds < (17 if mode == "automatic" else 4), seconds
        if mode == "tools":
            assert len(jsonl("recall-trace")) == start
        measurements.append({"scenario": label, "first_segment_seconds": round(seconds, 3),
                             "details": len(delivery["items"]), "pages": len(delivery["pages"]),
                             "recall_timeout": 15, "background_timeout": 3})
    await acceptance.control("delay_recall", seconds=0)
    await acceptance.control("delay_background", seconds=0)
    acceptance.report["measurements"]["deadline_combinations"] = measurements
    acceptance.passed("native_shared_deadline_delivers_timely_branch_and_on_demand_page_without_quarter_cap")

    await references(history_limit=0)
    await references(history_limit=30)
    await reading("automatic")
    await background(False)
    assert (await usage())["turns"] == []
    last_id = None
    for index in range(5):
        last_id = await acceptance.bot.send(ALICE, "", segments=[{"type": "image", "data": {"file": IMAGE}}])
        await asyncio.sleep(0.2)

    async def five_skips():
        value = await usage()
        return value if len(value["skips"]) == 5 else None
    skipped = await until(five_skips, timeout=20)
    assert not skipped["turns"] and all(set(item) == {"at", "code", "media_types"} for item in skipped["skips"])
    await acceptance.bot.send(ALICE, "", message_id=last_id, segments=[{"type": "image", "data": {"file": IMAGE}}])
    await acceptance.bot.chat(ALICE, (await acceptance.command_prefix()) + " status")
    assert len((await usage())["skips"]) == 5
    await page.reload()
    await acceptance.dismiss_host_notice(page)
    await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
    await frame.locator("#skip-warning-open").wait_for()
    await frame.locator("#skip-warning-open").click()
    await frame.locator("#usage-tab-skips[aria-selected=true]").wait_for()
    assert await frame.locator("#usage-skips .usage-card").count() == 5
    assert not await frame.locator("#usage-picker").is_visible()
    await frame.locator("#usage-tab-skips").focus()
    await page.keyboard.press("Home")
    await frame.locator("#usage-tab-content[aria-selected=true]").wait_for()
    await page.keyboard.press("End")
    await frame.locator("#usage-tab-skips[aria-selected=true]").wait_for()
    await acceptance.layout(page, frame)
    await page.screenshot(path=str(ROOT / "gateway-zero-turn-skips.png"), full_page=True)
    await frame.locator('[data-close="usage-dialog"]').click()
    await acceptance.bot.chat(ALICE, "HB-GATEWAY 有效文字重置预警")
    await acceptance.drained()
    assert not (await acceptance.scope())["summary"].get("skip_warning")
    await frame.locator("#skip-warning").wait_for(state="hidden", timeout=15000)
    acceptance.passed("native_zero_turns_five_unique_skips_keyboard_phone_and_first_enqueue_resets_warning")

    bob = await acceptance.scope(BOB)
    await acceptance.native("PUT", bob["bank"], {"name": "Synthetic empty Bank"})
    await acceptance.install("candidate-upgrade.zip")
    await acceptance.usable()
    async def zero_total():
        value = (await acceptance.scope(BOB))["summary"].get("bank_total")
        return value if value is not None else None
    empty = await until(zero_total, timeout=20)
    assert empty == {"documents": 0, "memories": 0}, empty
    await acceptance.control("stop_hindsight")
    try:
        await acceptance.install("candidate-upgrade.zip")
        latencies = []
        for _ in range(3):
            began = time.monotonic()
            state = await acceptance.bootstrap()
            latencies.append(round(time.monotonic() - began, 3))
            assert latencies[-1] < 5
            assert all(row["summary"].get("bank_total") is None for row in state["scopes"])
        acceptance.report["measurements"]["offline_poll_seconds"] = latencies
    finally:
        await acceptance.control("start_hindsight")
        await acceptance.usable()
    acceptance.passed("native_empty_bank_totals_are_zero_and_offline_cold_polls_finish_before_five_seconds")

    scope = await acceptance.scope()
    prepared = await acceptance.action(identifier, "forget_prepare")
    await acceptance.action(identifier, "forget_confirm", confirmation=prepared["confirmation"])
    async def clean():
        current = await acceptance.scope()
        return current if current["bank"] != scope["bank"] and not current["summary"].get("maintenance") else None
    await until(clean, timeout=120)
    assert (await usage())["skips"] == [] and (await usage())["turns"] == []
    await acceptance.switch_bot(frame, await acceptance.scope(), "")
    current = await acceptance.scope()
    fresh = await acceptance.http("GET", acceptance.prefix + "/scopes/" + current["scope_id"] + "/usage")
    assert fresh["skips"] == [] and not current["summary"].get("skip_warning")
    acceptance.passed("native_erasure_and_bot_switch_remove_old_skip_history_and_warning")
