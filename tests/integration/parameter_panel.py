"""Native iframe parameter writes, readback and identity continuity."""

import json

from live_e2e import ALICE, database, jsonl, until


async def save_parameters(acceptance, frame, **updates):
    before = await acceptance.config()
    await frame.locator("#tab-setup").click()
    await frame.locator("#reload-parameters").click()
    await until(lambda: frame.locator("#reload-parameters").is_enabled(), timeout=30)
    if set(updates) & {"recall_query_tokens", "query_tokenizer", "queue_max_tasks", "queue_max_mib", "command_prefixes"}:
        details = frame.locator(".advanced-settings")
        if not await details.evaluate("(node) => node.open"):
            await details.locator("summary").click()
    for key, value in updates.items():
        await frame.locator("#memory-" + key).fill("\n".join(value) if isinstance(value, list) else str(value))
    await frame.locator("#save-parameters").click()
    await until(lambda: frame.locator("#save-parameters").is_enabled(), timeout=40)
    feedback = await frame.locator("#parameters-feedback").inner_text()
    assert "已保存" in feedback, feedback
    stored = await acceptance.config()
    snapshot = await acceptance.bootstrap()
    for key, value in updates.items():
        assert stored[key] == value, (key, stored[key], value)
        assert snapshot["parameters"]["effective"][key] == value
        assert "生效值" in await frame.locator("#effective-" + key).inner_text()
    for key in ("api_url", "api_key", "service_id", "agent_id", "allowed_accounts",
                "service_address_revision", "control_state_dir"):
        assert stored.get(key) == before.get(key), key
    acceptance.report.setdefault("parameter_saves", []).append({
        "updates": updates, "runtime": snapshot["parameters"]["effective"],
        "managed": snapshot["config"]["managed"],
    })
    await frame.locator("#tab-memories").click()
    return snapshot


async def verify_advanced(acceptance, frame):
    await acceptance.drained()
    scope = await acceptance.scope()
    route = acceptance.prefix + "/scopes/" + scope["scope_id"] + "/background"
    before = await acceptance.http("GET", route)
    await save_parameters(acceptance, frame, recall_query_tokens=32, query_tokenizer="cl100k_base",
                          queue_max_tasks=1, queue_max_mib=1, command_prefixes=["HB-IGNORE:"])
    after = await acceptance.http("GET", route)
    assert before["check"]["tokens"] != after["check"]["tokens"], (before["check"], after["check"])
    await acceptance.bot.chat(ALICE, "HB-IGNORE: this synthetic message must never become a saved material.")
    await acceptance.bot.chat(ALICE, "/hindsight status")
    assert "Hindsight 知识页" not in acceptance.latest_wire()
    await acceptance.drained()
    assert "HB-IGNORE:" not in json.dumps(database(acceptance.runtime, "SELECT payload FROM outbox"))
    await acceptance.control("hold_extraction")
    held_before = len(jsonl("held-extraction-trace"))
    try:
        await acceptance.bot.chat(ALICE, "A synthetic capacity message. " * 100)
        await until(lambda: len(jsonl("held-extraction-trace")) > held_before, timeout=45)
        before_tasks = len(acceptance.tasks())
        await acceptance.bot.chat(ALICE, "This synthetic second message exceeds the one-record queue limit.")
        assert len(acceptance.tasks()) == before_tasks
    finally:
        await acceptance.control("release_extraction")
        await save_parameters(acceptance, frame, queue_max_tasks=10000, queue_max_mib=64)
    await acceptance.drained()
    if not acceptance.settings.get("parameter_managed"):
        import tiktoken

        queries = jsonl("recall-trace")
        assert queries
        query = queries[-1]["body"]["query"]
        assert len(tiktoken.get_encoding("cl100k_base").encode(query, disallowed_special=())) <= 32
    await save_parameters(acceptance, frame, recall_query_tokens=380, query_tokenizer="o200k_base", command_prefixes=[])
    acceptance.passed("native_advanced_settings_change_token_counts_query_budget_queue_admission_and_command_exclusions")
