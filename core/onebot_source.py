"""Native OneBot v11 private messages and recipient identity."""

import asyncio
import time
from collections.abc import Mapping
from itertools import groupby

from .source import private_event, valid_time as valid_message_time


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
    if types - {"text", "face", "reply", "at"}:
        return "media_or_non_text"
    return "unsupported_message"


def header(event, plain_type, wake_prefixes, adapter, binding=None, *, inspect=None, allow_readonly=False):
    if not private_event(event):
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
        type(timestamp) is int and valid_message_time(timestamp, received)
    )
    if inspect is not None:
        inspect(fields[1], timestamp if valid_time else None, received, lambda: message_shape(segments))
    if not valid_time:
        return None
    components = event.get_messages()
    if not isinstance(segments, list) or not segments or not components:
        return None
    from .source import finish_message

    normalized, reply_to = [], ""
    for segment in segments:
        if not isinstance(segment, Mapping):
            return None
        data = segment.get("data")
        kind = segment.get("type")
        if not isinstance(data, Mapping):
            return None
        if kind == "reply":
            if not isinstance(data, Mapping) or set(data) != {"id"} or type(data["id"]) not in (str, int):
                return None
            if (set(segment) != {"type", "data"} or reply_to or len(str(data["id"])) > 20
                    or not str(data["id"]).isascii() or not str(data["id"]).isdecimal() or int(data["id"]) <= 0):
                return None
            reply_to = str(data["id"])
            continue
        if kind == "text":
            if not isinstance(data.get("text"), str):
                return None
            normalized.append(("text", data["text"]))
        elif kind == "face":
            if type(data.get("id")) not in (str, int):
                return None
            normalized.append(("emoji", data.get("name") or data.get("text")))
        elif kind in {"mface", "image", "record", "file", "video"}:
            if not data:
                return None
            normalized.append((kind, None))
        elif kind == "at":
            target = data.get("qq")
            if type(target) not in (str, int) or not str(target):
                return None
            normalized.append(("text", "@" + str(target)))
        else:
            return None
    host_parts = [text for kind, group in groupby(segments, key=lambda part: part["type"])
                  if kind == "text" and (text := "".join(part["data"]["text"] for part in group).strip())]
    visible = "".join(host_parts)
    at_segments = [str(part["data"]["qq"]) for part in segments if part["type"] == "at"]
    at_components = [part for part in components if type(part).__name__ == "At"]
    if at_segments:
        if [str(part.qq) for part in at_components] != at_segments:
            return None
        visible = event.get_message_str()
    return finish_message(event, plain_type, wake_prefixes, "aiocqhttp", fields[1], fields[2],
                          timestamp, normalized, host_parts, visible_text=visible, reply_to=reply_to)
