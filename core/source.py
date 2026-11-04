"""Small, deterministic ingress contracts; deliberately has no AstrBot imports."""

import time
import math
from collections.abc import Mapping
from dataclasses import dataclass

from .identity import Incoming


@dataclass(frozen=True)
class Platform:
    logical_type: str
    user_label: str
    user_hint: str
    numeric: bool = False
    supported: bool = False


# Host types remain distinct even when two transports share a logical identity.
PLATFORMS = {
    "aiocqhttp": Platform(
        "aiocqhttp", "用户 QQ 号", "与这个机器人私聊的 QQ 号", True, True,
    ),
    "qq_official": Platform(
        "qq_official", "用户 OpenID", "填写 /sid 中的用户 UID（OpenID，不是 QQ 号）",
        supported=True,
    ),
    "qq_official_webhook": Platform(
        "qq_official", "用户 OpenID", "填写 /sid 中的用户 UID（OpenID，不是 QQ 号）",
        supported=True,
    ),
    "weixin_oc": Platform("weixin_oc", "用户 UID", "填写 /sid 中的用户 UID"),
    "telegram": Platform("telegram", "用户 ID", "填写 /sid 中的用户 UID", True),
}
TIME_SOURCES = {
    "aiocqhttp": "onebot.time",
    "qq_official": "qq_official.timestamp",
    "weixin_oc": "weixin_oc.create_time_ms",
    "telegram": "telegram.message.date",
}
# Host queue timestamps use this process's wall clock, unlike platform clocks.
HOST_CLOCK_TOLERANCE = 1.0


@dataclass(frozen=True)
class SourceIdentity:
    host_type: str
    instance_id: str
    logical_type: str
    bot_id: str
    producer: object
    authorization_revision: int = 0


def platform_details(host_type):
    contract = PLATFORMS.get(host_type)
    return {
        "type": host_type,
        "logical_type": contract.logical_type if contract else host_type,
        "supported": bool(contract and contract.supported),
        "user_label": contract.user_label if contract else "用户 ID",
        "user_hint": contract.user_hint if contract else "此平台尚未开放记忆功能",
        "numeric_user_id": bool(contract and contract.numeric),
    }


def valid_user_id(host_type, value):
    contract = PLATFORMS.get(host_type)
    if not contract or not isinstance(value, str) or not value:
        return False
    # UMO uses ':' as a field separator; IDs must not select a different profile.
    if ":" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return not contract.numeric or (value.isascii() and value.isdecimal() and len(value) <= 20)


def native_header(event, plain_type, wake_prefixes=(), *, adapter=None, binding=None) -> Incoming | None:
    """Validate original identity, time and text for both commands and capture."""
    host_type = event.get_platform_name()
    if host_type in {"qq_official", "qq_official_webhook"}:
        from .qq_source import header

        if not PLATFORMS[host_type].supported:
            return None
        return header(event, plain_type, wake_prefixes, adapter, binding)
    if event.get_platform_name() != "aiocqhttp":
        return None
    return _onebot(event, plain_type, wake_prefixes)


def _onebot(event, plain_type, wake_prefixes):
    if (
        not event.is_private_chat()
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
        original.strip() != "".join(v.text for v in components)
        or not matching_text
        or not original.strip()
        or len(original) > 8000
    ):
        return None
    timestamp = raw.get("time")
    if isinstance(timestamp, str) and timestamp.isdecimal():
        timestamp = int(timestamp)
    if type(timestamp) is not int or not 0 < timestamp <= time.time() + 300:
        return None
    received = getattr(event, "created_at", None)
    if (
        type(received) not in (int, float) or not math.isfinite(received)
        or not 0 < received <= time.time() + HOST_CLOCK_TOLERANCE
    ):
        return None
    # AstrBot sets this public timestamp before placing the event in its queue.
    return Incoming("aiocqhttp", *fields, mid, timestamp, original, received_at=received)


def native_snapshot(event, plain_type, command_prefixes=("/",)) -> Incoming | None:
    incoming = native_header(event, plain_type)
    if incoming and not any(
        incoming.text.lstrip().startswith(prefix) for prefix in command_prefixes if prefix
    ):
        return incoming
    return None


def source_identity(event, incoming, adapter=None, *, authorization_revision=0, binding=None):
    """OneBot's original recipient is producer evidence, including queued events."""
    if incoming.platform_type == "qq_official":
        from .qq_source import bound_login

        login = bound_login(event, adapter, binding)
        if login is None or incoming.bot_id != login.appid:
            return None
        return SourceIdentity(
            event.get_platform_name(), incoming.platform_instance_id, "qq_official",
            login.appid, login.client, authorization_revision,
        )
    if incoming.platform_type != "aiocqhttp":
        return None
    producer = getattr(event, "bot", None)
    if adapter is not None and (
        producer is None or producer is not adapter.get_client()
    ):
        return None
    return SourceIdentity(
        "aiocqhttp", incoming.platform_instance_id, "aiocqhttp",
        incoming.bot_id, producer, authorization_revision,
    )


async def prepare_bot_identity(context, platform):
    """A bounded read for management actions; never called by synchronous checks."""
    import asyncio

    configs = context.get_config().get("platform", [])
    record = next((row for row in configs if str(row.get("id")) == platform), None)
    if not record or not record.get("enable", True):
        return ""
    getter = getattr(context, "get_platform_inst", None)
    adapter = getter(platform) if getter else None
    if adapter is None:
        return ""
    if record.get("type") in {"qq_official", "qq_official_webhook"}:
        from .qq_source import native_login

        login = native_login(adapter)
        return login.appid if login and login.appid == record.get("appid") else ""
    if record.get("type") != "aiocqhttp":
        return ""
    client = adapter.get_client()
    try:
        async with asyncio.timeout(1):
            login = await client.get_login_info()
        if getter(platform) is not adapter or adapter.get_client() is not client:
            return ""
        identity = str(login.get("user_id", ""))
        return identity if identity.isascii() and identity.isdecimal() and int(identity) > 0 else ""
    except Exception:
        return ""


def local_runner(config) -> bool:
    return config.get("agent_runner", {}).get("runner_type", "local") == "local"
