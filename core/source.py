"""The OneBot ingress boundary; deliberately has no AstrBot imports."""

import time
from collections.abc import Mapping

from .identity import Incoming


def native_snapshot(event, plain_type, command_prefixes=("/",)) -> Incoming | None:
    if (
        event.get_platform_name() != "aiocqhttp"
        or not event.is_private_chat()
        or event.get_group_id()
    ):
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("post_type") != "message" or raw.get("message_type") != "private":
        return None
    fields = (event.get_platform_id(), event.get_self_id(), event.get_sender_id())
    if not all(isinstance(v, str) and v for v in fields) or fields[1] == fields[2]:
        return None
    if str(raw.get("self_id")) != fields[1] or str(raw.get("user_id")) != fields[2]:
        return None
    mid = str(getattr(event.message_obj, "message_id", "") or "")
    if not mid or mid in {"0", "None"} or str(raw.get("message_id")) != mid:
        return None
    segments, components = raw.get("message"), event.get_messages()
    if not isinstance(segments, list) or not segments or not components:
        return None
    texts = []
    for segment in segments:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            return None
        data = segment.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("text"), str):
            return None
        texts.append(data["text"])
    if not all(
        isinstance(v, plain_type) and isinstance(v.text, str) for v in components
    ):
        return None
    original = "".join(texts)
    if (
        original != "".join(v.text for v in components)
        or original.strip() != event.get_message_str().strip()
        or not original.strip()
        or len(original) > 8000
        or any(original.lstrip().startswith(p) for p in command_prefixes if p)
    ):
        return None
    timestamp = raw.get("time")
    if isinstance(timestamp, str) and timestamp.isdecimal():
        timestamp = int(timestamp)
    if type(timestamp) is not int or not 0 < timestamp <= time.time() + 300:
        return None
    return Incoming("aiocqhttp", *fields, mid, timestamp, original)


def local_runner(config) -> bool:
    return config.get("agent_runner", {}).get("runner_type", "local") == "local"
