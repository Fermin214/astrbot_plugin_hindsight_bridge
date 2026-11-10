"""Ordinary Telegram Bot private messages through the native AstrBot adapter."""

import math
import time
from dataclasses import dataclass
from datetime import datetime

from .identity import Incoming


MESSAGE_FIELDS = frozenset({
    "message_id", "date", "chat", "from", "text", "entities", "has_protected_content",
    "channel_chat_created", "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
})
PLAIN_ENTITIES = frozenset({
    "bot_command", "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "blockquote", "expandable_blockquote", "url", "email",
    "phone_number", "hashtag", "cashtag",
})


def numeric_id(value):
    return type(value) is int and 0 < value < 2**63


@dataclass(frozen=True, eq=False)
class TelegramLogin:
    bot_id: str
    client: object

    def matches(self, other):
        return (
            isinstance(other, TelegramLogin) and self.bot_id == other.bot_id
            and self.client is other.client
        )


def native_login(adapter):
    try:
        from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
        from telegram.ext import ExtBot

        if type(adapter) is not TelegramPlatformAdapter:
            return None
        client = adapter.get_client()
        if (
            type(client) is not ExtBot or adapter.application.bot is not client
            or not adapter.application.running or not numeric_id(client.id)
            or client.bot.is_bot is not True
        ):
            return None
        return TelegramLogin(str(client.id), client)
    except (AttributeError, ImportError, RuntimeError, TypeError):
        return None


def bound_login(event, adapter, binding):
    if not binding or binding.get("invalid_login") or binding.get("adapter") is not adapter:
        return None
    login = native_login(adapter)
    previous = binding.get("login")
    if (
        login is None or not isinstance(previous, TelegramLogin) or not previous.matches(login)
        or binding.get("bot_id") != login.bot_id or binding.get("client") is not login.client
        or getattr(event, "client", None) is not login.client
        or event.get_platform_name() != "telegram" or event.get_platform_id() != adapter.meta().id
    ):
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    try:
        from telegram import Update

        if type(raw) is not Update or raw.get_bot() is not login.client:
            return None
    except (AttributeError, ImportError, RuntimeError):
        return None
    return login


def header(event, plain_type, wake_prefixes, adapter, binding):
    login = bound_login(event, adapter, binding)
    if login is None or not event.is_private_chat() or event.get_group_id():
        return None
    from telegram import Message

    raw = event.message_obj.raw_message
    msg = raw.message
    if (
        type(msg) is not Message or set(raw.to_dict()) - {"update_id", "message"}
        or set(msg.to_dict()) - MESSAGE_FIELDS or msg.api_kwargs
    ):
        return None
    try:
        if msg.get_bot() is not login.client:
            return None
    except RuntimeError:
        return None
    sender, chat = msg.from_user, msg.chat
    if (
        sender is None or sender.is_bot is not False or not numeric_id(sender.id)
        or chat.type != "private" or not numeric_id(chat.id) or chat.id != sender.id
        or str(sender.id) != event.get_sender_id()
        or str(chat.id) != event.message_obj.session_id
        or not numeric_id(msg.message_id) or str(msg.message_id) != event.message_obj.message_id
        or any(getattr(msg, name, False) for name in (
            "channel_chat_created", "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
        ))
    ):
        return None
    date, original = msg.date, msg.text
    if (
        not isinstance(date, datetime) or date.utcoffset() is None
        or not isinstance(original, str) or not original.strip() or len(original) > 8000
        or any(entity.type not in PLAIN_ENTITIES for entity in msg.entities or ())
    ):
        return None
    occurred = date.timestamp()
    received = getattr(event, "created_at", None)
    if (
        not math.isfinite(occurred) or not 0 < occurred <= time.time() + 300
        or type(received) not in (int, float) or not math.isfinite(received)
        or not 0 < received <= time.time() + 1
    ):
        return None
    # AstrBot removes the Bot username suffix before dispatching a command.
    canonical = original
    if original.startswith("/"):
        command, separator, arguments = original.partition(" ")
        if command.count("@") == 1:
            name, target = command.split("@")
            if target == login.client.username:
                canonical = name + (separator + arguments if separator else "")
    parts = event.get_messages()
    if (
        not isinstance(parts, list) or len(parts) != 1
        or type(parts[0]) is not plain_type or parts[0].text != canonical
    ):
        return None
    host_text = event.get_message_str()
    matching = isinstance(host_text, str) and (
        canonical.strip() == host_text.strip() or (
            getattr(event, "is_at_or_wake_command", False) is True
            and any(
                canonical.strip().startswith(prefix)
                and canonical.strip()[len(prefix):].strip() == host_text.strip()
                for prefix in wake_prefixes if isinstance(prefix, str) and prefix
            )
        )
    )
    if not matching:
        return None
    return Incoming(
        "telegram", event.get_platform_id(), login.bot_id, str(sender.id),
        str(msg.message_id), occurred, canonical, received_at=received,
    )
