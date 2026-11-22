"""Native OneBot v11 private messages and recipient identity."""

import asyncio
import math
import time
from collections.abc import Mapping

from .identity import Incoming


HOST_CLOCK_TOLERANCE = 300.0


def native_types():
    from aiocqhttp import CQHttp, Event
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )

    return AiocqhttpAdapter, AiocqhttpMessageEvent, CQHttp, Event


def native_client(adapter, instance):
    try:
        adapter_type, _, client_type, _ = native_types()
        if type(adapter) is not adapter_type:
            return None
        meta = adapter.meta()
        client = adapter.get_client()
        if meta.name == "aiocqhttp" and meta.id == instance and type(client) is client_type:
            return client
    except (AttributeError, ImportError):
        return None
    return None


async def bot_identity(adapter, instance, getter):
    client = native_client(adapter, instance)
    if client is None:
        return ""
    try:
        async with asyncio.timeout(1):
            login = await client.get_login_info()
        if getter(instance) is not adapter or native_client(adapter, instance) is not client:
            return ""
        identity = str(login.get("user_id", ""))
        if identity.isascii() and identity.isdecimal() and int(identity) > 0:
            return identity
    except Exception:
        return ""
    return ""


def bound_client(event, adapter):
    client = native_client(adapter, event.get_platform_id())
    if client is None or getattr(event, "bot", None) is not client:
        return None
    _, event_type, _, raw_type = native_types()
    if (
        type(event) is event_type
        and isinstance(getattr(event.message_obj, "raw_message", None), raw_type)
    ):
        return client
    return None


def message_shape(segments):
    if not isinstance(segments, list):
        return "unsupported_message"
    types = {
        segment["type"] for segment in segments
        if isinstance(segment, Mapping) and isinstance(segment.get("type"), str)
    }
    if "reply" in types:
        return "quoted_or_reply"
    if types - {"text"}:
        return "media_or_non_text"
    return "unsupported_message"


def header(event, plain_type, wake_prefixes, adapter, binding=None, *, inspect=None, allow_readonly=False):
    if not event.is_private_chat() or event.get_group_id():
        return None
    if adapter is not None and bound_client(event, adapter) is None:
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("post_type") != "message" or raw.get("message_type") != "private":
        return None
    fields = (event.get_platform_id(), event.get_self_id(), event.get_sender_id())
    if not all(isinstance(value, str) and value for value in fields) or fields[1] == fields[2]:
        return None
    if str(raw.get("self_id")) != fields[1] or str(raw.get("user_id")) != fields[2]:
        return None
    mid = str(getattr(event.message_obj, "message_id", "") or "")
    if not mid or mid in {"0", "None"} or str(raw.get("message_id")) != mid:
        return None
    timestamp = raw.get("time")
    if isinstance(timestamp, str) and timestamp.isdecimal():
        try:
            timestamp = int(timestamp)
        except ValueError:
            timestamp = None
    received = getattr(event, "created_at", None)
    segments = raw.get("message")
    valid_time = (
        type(timestamp) is int and 0 < timestamp <= time.time() + 300
        and type(received) in (int, float) and math.isfinite(received)
        and 0 < received <= time.time() + HOST_CLOCK_TOLERANCE
    )
    if inspect is not None:
        inspect(fields[1], timestamp if valid_time else None, received, lambda: message_shape(segments))
    if not valid_time:
        return None
    components = event.get_messages()
    if not isinstance(segments, list) or not segments or not components:
        return None
    texts = []
    quoted = False
    for segment in segments:
        if not isinstance(segment, Mapping):
            return None
        data = segment.get("data")
        if allow_readonly and segment.get("type") == "reply":
            if not isinstance(data, Mapping) or set(data) != {"id"} or type(data["id"]) not in (str, int):
                return None
            if (set(segment) != {"type", "data"} or quoted or len(str(data["id"])) > 20
                    or not str(data["id"]).isascii() or not str(data["id"]).isdecimal() or int(data["id"]) <= 0):
                return None
            quoted = True
            continue
        if segment.get("type") != "text":
            return None
        if not isinstance(data, Mapping) or not isinstance(data.get("text"), str):
            return None
        texts.append(data["text"])
    if quoted:
        from astrbot.api.message_components import Reply

        if (any(type(value) not in (plain_type, Reply) for value in components)
                or sum(type(value) is Reply for value in components) > 1):
            return None
        components = [value for value in components if type(value) is plain_type]
    if not components or not all(isinstance(value, plain_type) and isinstance(value.text, str) for value in components):
        return None
    original = "".join(texts)
    host_text = event.get_message_str().strip()
    matching_text = original.strip() == host_text or (
        getattr(event, "is_at_or_wake_command", False) is True
        and any(
            original.strip().startswith(prefix)
            and original.strip()[len(prefix):].strip() == host_text
            for prefix in wake_prefixes if isinstance(prefix, str) and prefix
        )
    )
    if (
        original.strip() != "".join(value.text for value in components)
        or not matching_text
        or not original.strip()
        or len(original) > 8000
    ):
        return None
    return Incoming("aiocqhttp", *fields, mid, timestamp, original, received_at=received,
                    capture_allowed=not quoted)
