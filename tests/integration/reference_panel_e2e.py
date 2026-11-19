import json

import tiktoken

from live_e2e import BOB, until
from parameter_panel import save_parameters


async def verify_clipboard(page, frame, turn):
    await frame.locator("#usage-open").click()
    await frame.locator("#usage-turn").select_option(turn["id"])
    await frame.locator("#usage-copy-json").wait_for()
    option = await frame.locator("#usage-turn option:checked").inner_text()
    assert turn["conversation_id"] in option and turn["message_id"] in option
    assert await frame.locator(".usage-delivery").count() == len(turn["deliveries"])
    for index, delivery in enumerate(turn["deliveries"]):
        if delivery["text"]:
            row = frame.locator(".usage-delivery").nth(index)
            assert await row.locator(".usage-text").input_value() == delivery["text"].replace("\r\n", "\n").replace("\r", "\n")
            await row.get_by_role("button", name="复制原文").click()
            await frame.locator("#usage-feedback").filter(has_text="原文已复制").wait_for()
            assert await page.evaluate("navigator.clipboard.readText()") == delivery["text"]
    await frame.locator("#usage-copy-json").click()
    await frame.locator("#usage-feedback").filter(has_text="JSON 已复制").wait_for()
    assert json.loads(await page.evaluate("navigator.clipboard.readText()")) == turn


async def verify(acceptance, page, frame, turn, custom_prompt):
    scope = await acceptance.scope()
    route = acceptance.prefix + "/scopes/" + scope["scope_id"] + "/references"
    before = await acceptance.http("GET", route)
    encoding = tiktoken.get_encoding("o200k_base")
    prompt = frame.locator("#reference-prompt")
    feedback = frame.locator("#reference-feedback")
    await frame.locator("#tab-memories").click()
    account = json.dumps([scope["platform_instance_id"], scope["user_id"]], separators=(",", ":"))
    await frame.locator("#scope-picker").select_option(account)
    await until(lambda: prompt.is_enabled(), timeout=30)
    assert await prompt.input_value() == before["selection"]["prompt"]
    assert await frame.locator("#reference-tokens").inner_text() == f"{before['prompt_tokens']} / {before['prompt_limit']} token"

    for field_id, key in (("reference-limit", "detail_limit"), ("reference-history", "history_limit")):
        field = frame.locator("#" + field_id)
        await field.fill("")
        await frame.locator("#reference-save").click()
        assert await field.evaluate("(input) => input.validity.valueMissing && input.matches(':focus')")
        assert await frame.locator("#reference-form").evaluate("""form => {
            form.dispatchEvent(new Event("submit", {cancelable: true, bubbles: true}));
            return !document.getElementById("memory-fields").disabled;
        }""")
        assert (await acceptance.http("GET", route))["selection"] == before["selection"]
        await field.fill(str(before["selection"][key]))
    draft = "不同设置不应改变记忆提示的长度。"
    original = (await acceptance.bootstrap())["parameters"]["effective"]["query_tokenizer"]
    alternate = "cl100k_base" if original != "cl100k_base" else "o200k_base"
    tokens = len(encoding.encode(draft))
    assert len(tiktoken.get_encoding("cl100k_base").encode(draft)) != tokens
    counter = frame.locator("#reference-tokens").filter(has_text=f"{tokens} / {before['prompt_limit']} token")
    await prompt.fill(draft)
    await counter.wait_for()
    await save_parameters(acceptance, frame, query_tokenizer=alternate)
    try:
        await feedback.filter(has_text="重新载入").wait_for()
        assert await prompt.input_value() == draft and await frame.locator("#reference-save").is_disabled()
        assert (await acceptance.http("POST", route + "/count", payload={"prompt": draft}))["prompt_tokens"] == tokens
        await frame.locator("#reference-reload").click()
        await until(lambda: prompt.is_enabled(), timeout=30)
        assert await prompt.input_value() == before["selection"]["prompt"]
    finally:
        await save_parameters(acceptance, frame, query_tokenizer=original)
    await until(lambda: prompt.is_enabled(), timeout=30)
    await prompt.fill(draft)
    await page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=acceptance.dashboard)
    await verify_clipboard(page, frame, turn)
    other = await acceptance.scope(BOB)
    other_account = json.dumps([other["platform_instance_id"], other["user_id"]], separators=(",", ":"))
    cleared = await frame.locator("#scope-picker").evaluate("""(select, value) => {
        select.value = value;
        select.dispatchEvent(new Event("change", {bubbles: true}));
        return !document.getElementById("usage-dialog").open && !document.getElementById("usage-json").value
            && !document.getElementById("usage-turn").options.length && !document.getElementById("usage-deliveries").childElementCount
            && !document.getElementById("reference-prompt").value && document.getElementById("reference-fields").disabled;
    }""", other_account)
    assert cleared
    await until(lambda: prompt.is_enabled(), timeout=30)
    other_options = await acceptance.http("GET", acceptance.prefix + "/scopes/" + other["scope_id"] + "/references")
    assert await prompt.input_value() == other_options["selection"]["prompt"]
    await frame.locator("#scope-picker").select_option(account)
    await until(lambda: prompt.is_enabled(), timeout=30)
    assert await prompt.input_value() == before["selection"]["prompt"]
    assert await frame.locator("#reference-save").is_disabled()

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
