"""Feishu ingress through AstrBot's native Lark event and API client."""

import json
import math
import time
from dataclasses import dataclass

from .identity import Incoming


def identifier(value):
    return (
        isinstance(value, str) and bool(value) and value == value.strip()
        and ":" not in value and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def milliseconds(value):
    # Feishu JSON carries decimal strings; the SDK also accepts integer fields.
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        return int(value)
    return value if type(value) is int else None


@dataclass(frozen=True, eq=False)
class LarkLogin:
    appid: str
    open_id: str
    domain: str
    client: object
    api: object

    @property
    def bot_id(self):
        return f"{self.domain}/{self.appid}/{self.open_id}"

    def matches(self, other):
        return (
            isinstance(other, LarkLogin) and self.bot_id == other.bot_id
            and self.client is other.client and self.api is other.api
        )


def native_login(adapter):
    """Use the bot identity loaded by AstrBot, without requesting platform tokens."""
    try:
        import lark_oapi as lark
        from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter

        if (
            type(adapter) is not LarkPlatformAdapter
            or not identifier(adapter.appid) or not identifier(adapter.bot_open_id)
            or not adapter.bot_open_id.startswith("ou_")
            or not adapter.appsecret or adapter.config.get("app_id") != adapter.appid
            or type(adapter.get_client()) is not lark.ws.Client
            or type(adapter.lark_api) is not lark.Client
        ):
            return None
        domain = adapter.domain.rstrip("/")
        if domain not in {lark.FEISHU_DOMAIN, lark.LARK_DOMAIN}:
            return None
        if adapter.config.get("domain", lark.FEISHU_DOMAIN).rstrip("/") != domain:
            return None
        return LarkLogin(
            adapter.appid, adapter.bot_open_id, domain,
            adapter.get_client(), adapter.lark_api,
        )
    except (AttributeError, ImportError, TypeError):
        return None


def bound_login(event, adapter, binding):
    if (
        not binding or binding.get("invalid_login")
        or binding.get("adapter") is not adapter
    ):
        return None
    login = native_login(adapter)
    previous = binding.get("login")
    if (
        login is None or not isinstance(previous, LarkLogin) or not previous.matches(login)
        or binding.get("bot_id") != login.bot_id
        or binding.get("client") is not login.client
        or getattr(event, "bot", None) is not login.api
        or event.get_platform_id() != adapter.meta().id
        or event.get_platform_name() != "lark"
        or event.get_self_id() != login.open_id
    ):
        return None
    return login


def message_shape(raw):
    if raw.parent_id or raw.root_id or getattr(raw, "thread_id", None):
        return "quoted_or_reply"
    if raw.message_type != "text" or raw.mentions:
        return "media_or_non_text"
    return "unsupported_message"


def header(event, plain_type, wake_prefixes, adapter, binding, *, inspect=None, allow_readonly=False):
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from lark_oapi.api.im.v1 import EventMessage

    login = bound_login(event, adapter, binding)
    if (
        login is None or type(event) is not LarkMessageEvent
        or not event.is_private_chat() or event.get_group_id()
    ):
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    if type(raw) is not EventMessage or raw.chat_type != "p2p" or not identifier(raw.chat_id):
        return None
    occurred = milliseconds(raw.create_time)
    updated = milliseconds(raw.update_time)
    user, mid = event.get_sender_id(), raw.message_id
    if (
        not identifier(user) or not user.startswith("ou_") or user == login.open_id
        or not identifier(mid) or mid in {"0", "None"}
        or mid != event.message_obj.message_id
    ):
        return None
    received = getattr(event, "created_at", None)
    valid_time = (
        type(occurred) is int and 1_000_000_000_000 <= occurred <= (time.time() + 300) * 1000
        and (raw.update_time is None or updated in (0, occurred))
        and type(received) in (int, float) and math.isfinite(received) and 0 < received <= time.time() + 300
    )
    if inspect is not None:
        inspect(login.bot_id, occurred / 1000 if valid_time else None, received, lambda: message_shape(raw))
    if (
        not valid_time or raw.message_type != "text"
        or any(value and (not allow_readonly or not identifier(value)) for value in (raw.parent_id, raw.root_id))
        or getattr(raw, "thread_id", None)
        or raw.mentions or getattr(raw, "lark_agent_context", None) is not None
        or not isinstance(raw.content, str) or len(raw.content) > 64_000
    ):
        return None
    try:
        content = json.loads(raw.content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(content, dict) or set(content) != {"text"}:
        return None
    original = content["text"]
    if not isinstance(original, str) or not original.strip() or len(original) > 8000:
        return None
    parts, host_text = event.get_messages(), event.get_message_str()
    if allow_readonly and raw.parent_id and isinstance(parts, list) and parts:
        from astrbot.api.message_components import Reply

        if type(parts[0]) is Reply and parts[0].id == raw.parent_id:
            parts = parts[1:]
    if (
        not isinstance(parts, list) or len(parts) != 1
        or type(parts[0]) is not plain_type or parts[0].text != original.strip()
        or not isinstance(host_text, str)
    ):
        return None
    matching = original.strip() == host_text.strip() or (
        getattr(event, "is_at_or_wake_command", False) is True
        and any(
            original.strip().startswith(prefix)
            and original.strip()[len(prefix):].strip() == host_text.strip()
            for prefix in wake_prefixes if isinstance(prefix, str) and prefix
        )
    )
    if not matching:
        return None
    return Incoming(
        "lark", event.get_platform_id(), login.bot_id, user, mid,
        occurred / 1000, original, received_at=received, capture_allowed=not bool(raw.parent_id or raw.root_id),
    )
