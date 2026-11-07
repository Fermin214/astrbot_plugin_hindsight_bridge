"""Disposable native plugin for secondary Agent and late request-hook boundaries."""

import json
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Star, register
from astrbot.core.agent.tool import ToolSet


@register("astrbot_plugin_bridge_reading_guard_probe", "fixture", "Native reading boundary probe", "1.0.0")
class ReadingProbe(Star):
    def bridge(self):
        tool = self.context.get_llm_tool_manager().get_func("hindsight_bridge_recall")
        return tool.owner.plugin

    def save(self, key, value):
        path = Path("/delivery/reading-guard-probe.json")
        report = json.loads(path.read_text()) if path.exists() else {}
        report[key] = value
        path.write_text(json.dumps(report))

    @filter.on_llm_request(priority=-100)
    async def change_request_model(self, event, request):
        if event.message_str == "HB-TOOLS-REQUEST-MODEL":
            request.model = "fixture-late-request-model"

    @filter.on_agent_begin(priority=-100)
    async def independent_agent(self, event, run_context):
        if event.message_str != "HB-TOOLS-GUARDS":
            return
        bridge = self.bridge()
        turn = bridge.read_tools.turns[event]
        assert turn.context_ref() is run_context and turn.calls == 0
        provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        await self.context.tool_loop_agent(
            event=event, chat_provider_id=provider.provider_config["id"],
            prompt="HB-TOOLS-PROBE-BACKGROUND",
            tools=ToolSet(tools=[bridge.read_tools.owned["hindsight_bridge_recall"]]),
            max_steps=3,
        )
        assert turn.context_ref() is run_context and turn.calls == 0
        self.save("secondary_agent_cannot_claim_or_consume_main_ledger", True)

    @filter.on_agent_done(priority=-100)
    async def expired_agent(self, event, run_context, response):
        if event.message_str not in {"HB-TOOLS-GUARDS", "HB-TOOLS-IDLE", "HB-TOOLS-REQUEST-MODEL"}:
            return
        bridge = self.bridge()
        assert event not in bridge.read_tools.turns
        result = await bridge.read_tools.owned["hindsight_bridge_recall"].call(run_context, query="Cedar-120")
        assert json.loads(result)["error"] == "tool_context_unavailable"
        self.save("finished_agent_cannot_reuse_ledger", True)
        if event.message_str == "HB-TOOLS-IDLE":
            self.save("unused_agent_ledger_is_closed_by_host_completion", True)
