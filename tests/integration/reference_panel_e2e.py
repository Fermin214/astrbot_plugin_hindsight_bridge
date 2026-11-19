import json

import tiktoken

from live_e2e import until


async def verify(acceptance, page, frame, turn, custom_prompt):
    scope = await acceptance.scope()
    route = acceptance.prefix + "/scopes/" + scope["scope_id"] + "/references"
    before = await acceptance.http("GET", route)
    encoding = tiktoken.get_encoding((await acceptance.bootstrap())["parameters"]["effective"]["query_tokenizer"])
    prompt = frame.locator("#reference-prompt")
    feedback = frame.locator("#reference-feedback")
    await frame.locator("#tab-memories").click()
    account = json.dumps([scope["platform_instance_id"], scope["user_id"]], separators=(",", ":"))
    await frame.locator("#scope-picker").select_option(account)
    await until(lambda: prompt.is_enabled(), timeout=30)
    assert await prompt.input_value() == before["selection"]["prompt"]
    assert await frame.locator("#reference-tokens").inner_text() == f"{before['prompt_tokens']} / {before['prompt_limit']} token"

    await frame.locator("#usage-open").click()
    await frame.locator("#usage-turn").select_option(turn["id"])
    await frame.locator("#usage-copy-json").wait_for()
    option = await frame.locator("#usage-turn option:checked").inner_text()
    assert turn["conversation_id"] in option and turn["message_id"] in option
    assert await frame.locator(".usage-delivery").count() == len(turn["deliveries"])
    await page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=acceptance.dashboard)
    for index, delivery in enumerate(turn["deliveries"]):
        if delivery["text"]:
            row = frame.locator(".usage-delivery").nth(index)
            assert await row.locator(".usage-text").input_value() == delivery["text"]
            await row.get_by_role("button", name="复制原文").click()
            await frame.locator("#usage-feedback").filter(has_text="原文已复制").wait_for()
            assert await page.evaluate("navigator.clipboard.readText()") == delivery["text"]
    await frame.locator("#usage-copy-json").click()
    await frame.locator("#usage-feedback").filter(has_text="JSON 已复制").wait_for()
    assert json.loads(await page.evaluate("navigator.clipboard.readText()")) == turn
    await frame.locator('[data-close="usage-dialog"]').click()

    over = " Bridge" * (before["prompt_limit"] + 4)
    await prompt.fill(over)
    tokens = len(encoding.encode(over, disallowed_special=()))
    assert tokens > before["prompt_limit"]
    await frame.locator("#reference-tokens").filter(has_text=f"{tokens} / {before['prompt_limit']} token").wait_for()
    await frame.locator("#reference-save").click()
    await feedback.filter(has_text="请缩短").wait_for()
    assert (await acceptance.http("GET", route))["selection"] == before["selection"]
    await prompt.fill(custom_prompt)
    await frame.locator("#reference-save").click()
    await feedback.filter(has_text="已保存").wait_for()
    saved = await acceptance.http("GET", route)
    assert saved["selection"]["prompt"] == custom_prompt and saved["prompt_tokens"] == len(
        encoding.encode(custom_prompt, disallowed_special=()),
    )
    acceptance.passed("native_reference_form_token_count_limit_and_exact_save_and_round_clipboard_exports")
