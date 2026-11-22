"""Native Reflect settings and request-scoped results."""

import asyncio
import json

from .client import HindsightError
from .control import QueueError
from .identity import Scope, canonical
from .scope_settings import REFLECT, scope_key
from . import reading


def options_key(queue, scope):
    return scope_key(REFLECT, queue.connection_id, scope["owner_id"])


def read_options(queue, scope):
    default = {"revision": 0, "enabled": False, "timeout": 12}
    raw = queue.control.setting(options_key(queue, scope))
    if not raw:
        return default
    try:
        value = json.loads(raw)
        if (
            not isinstance(value, dict) or type(value.get("format")) is not int or value["format"] != 1
            or type(value.get("revision")) is not int or value["revision"] < 1
            or type(value.get("enabled")) is not bool or type(value.get("timeout")) is not int
            or not 3 <= value["timeout"] <= 30
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise QueueError("reflect_settings_invalid") from None
    default["revision"] = value["revision"]
    source = Scope(*json.loads(scope["identity_json"]))
    grant = queue.access.authorization(source.platform_instance_id, source.user_id)
    if (
        value.get("invalidated") or not grant or not grant["enabled"]
        or value.get("source_id") != source.id or grant["source_id"] != source.id
        or value.get("authorization_revision") != grant["revision"]
        or value.get("bank") != scope["bank"] or value.get("generation") != scope["generation"]
    ):
        return {**default, "invalidated": True}
    return {"revision": value["revision"], "enabled": value["enabled"], "timeout": value["timeout"]}


def save_options(queue, scope, *, revision, enabled, timeout):
    if type(revision) is not int or type(enabled) is not bool or type(timeout) is not int or not 3 <= timeout <= 30:
        raise QueueError("reflect_settings_invalid")
    with queue.control.transaction():
        if read_options(queue, scope)["revision"] != revision:
            raise QueueError("reflect_changed")
        owner = queue.control.owner(queue.connection_id, scope["owner_id"])
        source = Scope(*json.loads(scope["identity_json"]))
        grant = queue.access.authorization(source.platform_instance_id, source.user_id)
        if (
            queue.recovering or not owner or owner["bank"] != scope["bank"]
            or owner["revision"] != scope["revision"] or not grant or not queue.access.permitted(source)
        ):
            raise QueueError("scope_not_available")
        value = {
            "format": 1, "revision": revision + 1, "enabled": enabled, "timeout": timeout,
            "source_id": source.id, "authorization_revision": grant["revision"],
            "bank": scope["bank"], "generation": scope["generation"],
        }
        queue.control.db.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (options_key(queue, scope), canonical(value)),
        )
        queue.control.bump()
    return read_options(queue, scope)


def runtime_options(queue, scope):
    try:
        return read_options(queue, scope)
    except QueueError:
        return {"revision": 0, "enabled": False, "timeout": 12, "error": "reflect_settings_invalid"}


def project_response(value, bank, **limits):
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HindsightError("reflect_no_answer")
    sources = reading.native_sources(value)
    based = value.get("based_on") or {}
    for key in ("memories", "world", "experience", "observation", "mental_models", "mental-models", "directives"):
        for record in based.get(key, []):
            if not isinstance(record, dict) or record.get("bank_id", bank) != bank:
                raise HindsightError("invalid_reflect_response")
    usage = value.get("usage")
    if usage is not None and (not isinstance(usage, dict) or any(
        type(usage[key]) is not int or usage[key] < 0
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "thoughts_tokens") if key in usage
    )):
        raise HindsightError("invalid_reflect_response")
    usage = ({key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "thoughts_tokens") if key in usage}
             if usage is not None else None)
    result = {
        "kind": "reflect", "external_data": True, "text": text,
        "usage": usage, "sources": {**sources, "complete": False},
    }
    for key in ("memories", "mental_models", "directives"):
        result["sources"][key + "_available"] = len(sources[key])
        result["sources"][key] = sources[key][:5]
    for key in ("directives", "mental_models", "memories"):
        while not reading.fits(result, **limits) and result["sources"][key]:
            result["sources"][key].pop()
    if not reading.fits(result, **limits):
        raise HindsightError("tool_result_too_large")
    return result


class Reflection:
    def __init__(self, plugin):
        self.plugin = plugin
        self.tasks = {}
        self.outcomes = {}
        self.closed = False

    async def cancel(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close(self):
        self.closed = True
        await self.cancel()

    def public(self, scope):
        queue = self.plugin.outbox
        pending = sum(guard.queue is queue and guard.scope["bank"] == scope["bank"] for guard in self.tasks.values())
        options = runtime_options(queue, scope)
        status = self.outcomes.get((queue.connection_id, scope["owner_id"]), {})
        if status.get("bank") != scope["bank"] or status.get("revision") != options["revision"]:
            status = {}
        return {**options, "pending": pending,
                "status": {**{key: status[key] for key in ("state", "calls", "code") if key in status},
                           **({"message": self.plugin.diagnostic(status["code"])} if status.get("code") else {})}}

    def observe(self, scope, options, sequence, available):
        self.outcomes[(self.plugin.outbox.connection_id, scope["owner_id"])] = {
            "bank": scope["bank"], "revision": options["revision"], "sequence": sequence,
            "state": "not_called" if available else "unavailable", "calls": 0, "code": "",
        }

    def record(self, turn, state, code):
        key = (turn.guard.queue.connection_id, turn.guard.scope["owner_id"])
        if self.outcomes.get(key, {}).get("sequence") == turn.sequence:
            self.outcomes[key].update(state=state, calls=turn.reflect_calls, code=code)

    async def ask(self, turn, query, recheck, **limits):
        if self.closed or self.plugin.stopping:
            raise QueueError("plugin_stopping")
        queue, scope, client = turn.guard.queue, turn.guard.scope, turn.guard.client
        if limits["tokens"] < 192:
            raise QueueError("tool_budget_exhausted")
        await recheck()
        if not await client.bank_exists(scope["bank"]):
            raise HindsightError("bank_not_created" if queue.control.bank_state(queue.connection_id, scope["bank"]) == "new" else "bank_missing")
        await recheck()
        if self.closed or not turn.guard.valid(self.plugin):
            raise QueueError("reflect_changed")
        if len(self.tasks) >= 4:
            raise QueueError("reflect_busy")
        task = asyncio.create_task(self._native(
            turn, query, recheck, max_tokens=max(64, limits["tokens"] - 160),
        ), name="hindsight-reflect")
        self.tasks[task] = turn.guard
        task.add_done_callback(self.finished)
        value = await asyncio.shield(task)
        await recheck()
        return project_response(value, scope["bank"], **limits)

    def finished(self, task):
        self.tasks.pop(task)
        if not task.cancelled():
            task.exception()

    async def _native(self, turn, query, recheck, *, max_tokens):
        queue, control, client = turn.guard.queue, turn.guard.queue.control, turn.guard.client

        def settled():
            self.plugin.worker.wake.set()

        try:
            async with self.plugin.using_resources(), asyncio.timeout(120):
                await recheck()
                if not turn.guard.valid(self.plugin) or read_options(queue, turn.guard.scope) != turn.reflect_options:
                    raise QueueError("reflect_changed")
                value = await client.reflect(
                    turn.guard.scope["bank"], query, max_tokens=max_tokens,
                    settled=settled,
                )
                return value
        finally:
            control.retired_request_returned(queue.connection_id, turn.guard.scope["bank"])
            self.plugin.worker.wake.set()
