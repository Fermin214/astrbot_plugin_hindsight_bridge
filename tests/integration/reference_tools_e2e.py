import json

from live_e2e import ALICE


def message_text(message):
    content = message.get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


async def verify(acceptance, frame, bank):
    scope = await acceptance.scope()
    assert scope["bank"] == bank and scope["mode"] == "readonly"
    route = acceptance.prefix + "/scopes/" + scope["scope_id"]
    prompt = (await acceptance.http("GET", route + "/references"))["selection"]["prompt"]
    assert prompt

    async def configure(path, **settings):
        before = await acceptance.http("GET", route + path)
        await acceptance.http("POST", route + path, payload={
            "scope_revision": before["scope_revision"], "config_revision": before["config_revision"],
            "revision": before["selection"]["revision"], **settings,
        })

    async def chat(scenario):
        offset = len(acceptance.requests())
        await acceptance.bot.chat(ALICE, scenario)
        requests = acceptance.requests()[offset:]
        turns = (await acceptance.http("GET", route + "/usage"))["turns"]
        assert turns and str(turns[0]["message_id"]) == str(acceptance.bot.message_id), turns
        snapshot = (await acceptance.http("GET", route + "/usage/" + turns[0]["id"]))["turn"]
        request, *tools = snapshot["deliveries"]
        initial = next(message_text(message) for message in reversed(requests[0]["messages"])
                       if message["role"] == "user" and scenario in message_text(message))
        assert request["prompt"] == prompt and request["text"] in initial
        assert initial.count(prompt) == 1 and request["status"] == "not_called"
        last = requests[-1]["messages"]
        start = max(index for index, message in enumerate(last)
                    if message["role"] == "user" and scenario in message_text(message))
        actual = [message_text(message) for message in last[start + 1:] if message["role"] == "tool"]
        assert [item["text"] for item in tools] == actual, (tools, actual)
        return tools, [json.loads(text) for text in actual]

    await configure("/reading", mode="tools")
    tools, _ = await chat("HB-TOOLS-IDLE reference snapshot")
    assert not tools
    acceptance.passed("on_demand_first_request_contains_one_custom_prompt_without_any_tool_calls")

    selected = await acceptance.page(bank, "HB-TOOLS-PAGE")
    tools, values = await chat("HB-TOOLS-FLOW reference snapshot")
    assert [item["kind"] for item in tools] == ["search_pages", "read_page", "recall"]
    excerpt = values[1]
    assert excerpt["page_id"] == selected and excerpt["next_line"] is not None
    native = await acceptance.native("GET", bank + "/knowledge-base/pages/" + selected)
    assert excerpt["content"] == "".join(native["body"].splitlines(keepends=True)[:excerpt["end_line"]])
    assert all("HB-TOOLS-V1-LINE-199" not in delivery["text"] for delivery in tools)
    assert values[2]["kind"] == "recall" and tools[2]["status"] != "error"
    acceptance.passed("tool_snapshot_matches_ordered_wire_and_keeps_only_delivered_page_lines")

    await configure("/reflect", enabled=True, timeout=8)
    tools, values = await chat("HB-REFLECT-COMPLEX reference snapshot")
    assert len(values) == 1 and tools[0]["kind"] == "reflect"
    answer = values[0]
    assert answer.get("kind") == "reflect" and "HB-REFLECT-ANSWER" in answer.get("text", ""), answer
    assert answer["sources"]["available"]
    assert any(answer["sources"][key] for key in ("memories", "mental_models", "directives"))
    acceptance.passed("reflect_snapshot_matches_wire_answer_and_its_native_sources")
    await configure("/reflect", enabled=False, timeout=8)

    tools, values = await chat("HB-TOOLS-QUOTA reference snapshot")
    assert values[-1] == {"error": "tool_budget_exhausted"} and tools[-1]["status"] == "budget"
    assert len(tools) >= 2
    acceptance.passed("budget_replacement_snapshot_contains_only_the_final_status")
    await configure("/reading", mode="automatic")
