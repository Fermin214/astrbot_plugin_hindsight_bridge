"""Weixin ClawBot ingress through AstrBot's native adapter and raw recipient."""

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .identity import Incoming


MESSAGE_FIELDS = frozenset({
    "seq", "message_id", "from_user_id", "to_user_id", "client_id",
    "create_time_ms", "update_time_ms", "delete_time_ms", "session_id", "group_id",
    "message_type", "message_state", "item_list", "context_token", "run_id",
    "root_id", "parent_id",
})
ITEM_FIELDS = frozenset({
    "type", "create_time_ms", "update_time_ms", "is_completed", "msg_id", "text_item",
})
NON_TEXT_FIELDS = frozenset({
    "ref_msg", "image_item", "voice_item", "file_item", "video_item",
    "tool_call_start_item", "tool_call_result_item",
})
EMPTY_LIST_FIELDS = frozenset({"button_item_list", "at_bot_username_list"})


def valid_update(value, created, finished):
    if value is None or type(value) is int and value == 0:
        return True
    # FINISH can arrive after creation; message_id still deduplicates later updates.
    return (
        type(value) is int and type(created) is int
        and created <= value <= (time.time() + 300) * 1000
        and (finished or value == created)
    )


def identifier(value):
    return (
        isinstance(value, str) and bool(value.strip()) and value == value.strip()
        and ":" not in value and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


@dataclass(frozen=True, eq=False)
class WeixinLogin:
    account_id: str
    client: object

    def matches(self, other):
        return (
            isinstance(other, WeixinLogin) and self.account_id == other.account_id
            and self.client is other.client
        )


def native_login(adapter):
    try:
        from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import WeixinOCAdapter
        from astrbot.core.platform.sources.weixin_oc.weixin_oc_client import WeixinOCClient

        if (
            type(adapter) is not WeixinOCAdapter or not identifier(adapter.account_id)
            or type(adapter.client) is not WeixinOCClient
            or not adapter.token or not adapter.client.token
            or adapter.config.get("weixin_oc_account_id") != adapter.account_id
        ):
            return None
        return WeixinLogin(adapter.account_id, adapter.client)
    except (AttributeError, ImportError, TypeError):
        return None


def bound_login(event, adapter, binding):
    if (
        not binding or binding.get("invalid_login")
        or binding.get("adapter") is not adapter
        or getattr(event, "platform", None) is not adapter
    ):
        return None
    login = native_login(adapter)
    previous = binding.get("login")
    if (
        login is None or not isinstance(previous, WeixinLogin)
        or not previous.matches(login) or binding.get("bot_id") != login.account_id
        or event.get_platform_id() != adapter.meta().id
        or event.get_platform_name() != "weixin_oc"
    ):
        return None
    # The event holds a mutable adapter. Its original recipient proves which
    # account received the message, even before this plugin first observes it.
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, Mapping) or raw.get("to_user_id") != login.account_id:
        return None
    return login


def header(event, plain_type, wake_prefixes, adapter, binding):
    login = bound_login(event, adapter, binding)
    if login is None or not event.is_private_chat() or event.get_group_id():
        return None
    raw = event.message_obj.raw_message
    if (
        set(raw) - MESSAGE_FIELDS or raw.get("group_id")
        or any(value is not None and (type(value) is not int or value != 0)
               for value in (raw.get("root_id"), raw.get("parent_id")))
    ):
        return None
    if type(raw.get("message_type")) is not int or raw["message_type"] != 1:
        return None
    if type(raw.get("message_state")) is not int or raw["message_state"] not in {0, 2}:
        return None
    finished = raw["message_state"] == 2
    occurred = raw.get("create_time_ms")
    if (
        type(occurred) is not int or not 1_000_000_000_000 <= occurred <= (time.time() + 300) * 1000
        or not valid_update(raw.get("update_time_ms"), occurred, finished)
        or raw.get("delete_time_ms") not in (None, 0)
    ):
        return None
    user, mid = raw.get("from_user_id"), raw.get("message_id")
    if (
        not identifier(user) or user == login.account_id
        or user != event.get_sender_id()
        or type(mid) not in (str, int) or not str(mid).isascii()
        or not str(mid).isdecimal() or len(str(mid)) > 20
        or not 0 < int(mid) < 2**64 or str(mid) != event.message_obj.message_id
    ):
        return None
    items, parts = raw.get("item_list"), event.get_messages()
    if not isinstance(items, list) or not items or len(items) > 8000 or not isinstance(parts, list):
        return None
    texts, length = [], 0
    for item in items:
        if (
            not isinstance(item, Mapping) or set(item) - (ITEM_FIELDS | NON_TEXT_FIELDS | EMPTY_LIST_FIELDS)
            or any(item.get(name) is not None for name in NON_TEXT_FIELDS)
            or any(type(value) is not list or value
                   for name in EMPTY_LIST_FIELDS if (value := item.get(name)) is not None)
            or type(item.get("type")) is not int or item["type"] != 1
            or item.get("is_completed") is not None and item.get("is_completed") is not True
            or not valid_update(item.get("update_time_ms"), item.get("create_time_ms"), finished)
        ):
            return None
        text_item = item.get("text_item")
        if not isinstance(text_item, Mapping) or set(text_item) != {"text"}:
            return None
        text = text_item["text"]
        if not isinstance(text, str):
            return None
        length += len(text)
        if length > 8000:
            return None
        if text.strip():
            texts.append(text.strip())
    original = "\n".join(texts)
    if (
        not original or len(original) > 8000 or len(parts) != len(texts)
        or not all(type(part) is plain_type and part.text == text for part, text in zip(parts, texts))
    ):
        return None
    host_text = event.get_message_str()
    matching = isinstance(host_text, str) and (
        original == host_text.strip() or (
            getattr(event, "is_at_or_wake_command", False) is True
            and any(
                original.startswith(prefix) and original[len(prefix):].strip() == host_text.strip()
                for prefix in wake_prefixes if isinstance(prefix, str) and prefix
            )
        )
    )
    received = getattr(event, "created_at", None)
    if (
        not matching or type(received) not in (int, float) or not math.isfinite(received)
        or not 0 < received <= time.time() + 1
    ):
        return None
    return Incoming(
        "weixin_oc", event.get_platform_id(), login.account_id, user, str(mid),
        occurred / 1000, original, received_at=received,
    )
