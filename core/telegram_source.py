"""Ordinary Telegram Bot private messages through the native AstrBot adapter."""

from dataclasses import dataclass
from datetime import datetime

from .source import bound_account, private_event, valid_time as valid_message_time


PLAIN_ENTITIES = frozenset({
    "bot_command", "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "blockquote", "expandable_blockquote", "url", "email",
    "phone_number", "hashtag", "cashtag",
})
READONLY_ENTITIES = frozenset({"text_link", "mention"})
REPLY_FIELDS = frozenset({"reply_to_message", "external_reply", "quote", "reply_to_story"})
MEDIA_FIELDS = frozenset({
    "animation", "audio", "contact", "dice", "document", "game", "location", "paid_media",
    "photo", "poll", "sticker", "story", "venue", "video", "video_note", "voice",
})
NONORDINARY_FIELDS = REPLY_FIELDS | MEDIA_FIELDS | frozenset({
    "forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date",
    "edit_date", "via_bot", "sender_chat", "business_connection_id", "sender_business_bot",
    "is_automatic_forward", "paid_star_count", "invoice", "successful_payment", "web_app_data",
    "passport_data", "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "pinned_message", "migrate_to_chat_id", "migrate_from_chat_id", "message_auto_delete_timer_changed",
    "caption", "caption_entities",
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
    if not bound_account(event, adapter, binding, login, login.bot_id if login else "", getattr(event, "client", None)):
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    try:
        from telegram import Update

        if type(raw) is not Update or raw.get_bot() is not login.client:
            return None
    except (AttributeError, ImportError, RuntimeError):
        return None
    return login


def message_shape(message):
    fields = set(message.to_dict())
    if fields & REPLY_FIELDS:
        return "quoted_or_reply"
    if fields & MEDIA_FIELDS or any(entity.type not in PLAIN_ENTITIES for entity in message.entities or ()):
        return "media_or_non_text"
    return "unsupported_message"


def readonly_text(message, bot_name, *, caption=False):
    if not isinstance(bot_name, str):
        return None
    try:
        encoded = (message.caption if caption else message.text).encode("utf-16-le")
    except UnicodeError:
        return None
    pieces, mentions, end = [], [], 0
    for entity in (message.caption_entities if caption else message.entities) or ():
        if entity.type not in READONLY_ENTITIES:
            continue
        if type(entity.offset) is not int or type(entity.length) is not int:
            return None
        start, stop = entity.offset * 2, (entity.offset + entity.length) * 2
        if entity.api_kwargs or entity.offset < 0 or entity.length <= 0 or start < end or stop > len(encoded):
            return None
        try:
            text = encoded[start:stop].decode("utf-16-le")
        except UnicodeError:
            return None
        if entity.type == "mention":
            if not text.startswith("@") or len(text) < 2:
                return None
            name = text[1:]
            self_mention = name.lower() == bot_name.lower()
            mentions.append((bot_name if self_mention else name, name))
            if self_mention and not caption:
                pieces.append(encoded[end:start])
                end = stop
        else:
            if not isinstance(entity.url, str) or not entity.url or len(entity.url) > 8000:
                return None
            pieces.extend((encoded[end:stop], f" ({entity.url})".encode("utf-16-le")))
            end = stop
    pieces.append(encoded[end:])
    try:
        return b"".join(pieces).decode("utf-16-le"), mentions
    except UnicodeError:
        return None


def text_segments(original, entities):
    try:
        encoded = original.encode("utf-16-le")
        emojis, links = {}, {}
        for entity in entities:
            if (entity.type not in PLAIN_ENTITIES | READONLY_ENTITIES | {"custom_emoji"}
                    or type(entity.offset) is not int or type(entity.length) is not int
                    or entity.offset < 0 or entity.length <= 0):
                return None
            start, stop = entity.offset * 2, (entity.offset + entity.length) * 2
            if stop > len(encoded):
                return None
            name = encoded[start:stop].decode("utf-16-le")
            if entity.type == "custom_emoji":
                if not isinstance(entity.custom_emoji_id, str) or not entity.custom_emoji_id:
                    return None
                if start in emojis:
                    return None
                emojis[start] = (stop, name)
            elif entity.type == "text_link":
                if not isinstance(entity.url, str) or not entity.url or len(entity.url) > 8000:
                    return None
                links.setdefault(stop, []).append(entity.url)
        pieces, end = [], 0
        for position in sorted(set(emojis) | set(links) | {len(encoded)}):
            if position >= end:
                pieces.append(("text", encoded[end:position].decode("utf-16-le")))
                end = position
            if position in emojis:
                if position < end:
                    return None
                stop, name = emojis[position]
                pieces.append(("emoji", name))
                end = stop
            for url in links.get(position, ()):
                pieces.append(("text", " (" + url + ")"))
        pieces.append(("text", encoded[end:].decode("utf-16-le")))
        return pieces
    except UnicodeError:
        return None


def header(event, plain_type, wake_prefixes, adapter, binding, *, inspect=None, allow_readonly=False):
    login = bound_login(event, adapter, binding)
    if login is None or not private_event(event):
        return None
    from telegram import Message

    raw = event.message_obj.raw_message
    msg = raw.message
    if type(msg) is not Message:
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
    ):
        return None
    from .source import finish_message

    caption = not msg.text and msg.caption is not None
    date, original = msg.date, msg.caption if caption else msg.text
    entities = (msg.caption_entities if caption else msg.entities) or ()
    occurred = date.timestamp() if isinstance(date, datetime) and date.utcoffset() is not None else None
    received = getattr(event, "created_at", None)
    valid_time = valid_message_time(occurred, received)
    if inspect is not None:
        inspect(login.bot_id, occurred if valid_time else None, received, lambda: message_shape(msg))
    blocked_fields = NONORDINARY_FIELDS - MEDIA_FIELDS - {"reply_to_message", "caption", "caption_entities"}
    if (
        not valid_time or any(getattr(raw, name, None) is not None for name in (
            "edited_message", "channel_post", "edited_channel_post", "business_message", "edited_business_message",
        ))
        or any(msg.to_dict().get(name) not in (None, False, (), []) for name in blocked_fields)
        or any(getattr(msg, name, False) for name in (
            "channel_chat_created", "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
        ))
        or not isinstance(original, str) or not original.strip() or len(original) > 8000
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
    projected = readonly_text(msg, event.get_self_id(), caption=caption)
    normalized = text_segments(original, entities)
    parts = event.get_messages()
    if projected is None or normalized is None or not isinstance(parts, list):
        return None
    visible, mentions = projected
    if canonical != original:
        visible = canonical
        normalized = [("text", canonical)]
    if [(part.qq, part.name) for part in parts if type(part).__name__ == "At"] != mentions:
        return None
    for field, kind in (("photo", "image"), ("animation", "sticker"), ("sticker", "sticker"),
                        ("audio", "audio"), ("voice", "voice"), ("document", "file"),
                        ("video", "video"), ("video_note", "video")):
        if getattr(msg, field, None):
            normalized.append((kind, None))
    if any(getattr(msg, field, None) for field in MEDIA_FIELDS - {
        "photo", "animation", "sticker", "audio", "voice", "document", "video", "video_note",
    }):
        return None
    reply_to = str(msg.reply_to_message.message_id) if msg.reply_to_message else ""
    return finish_message(event, plain_type, wake_prefixes, "telegram", login.bot_id, str(sender.id),
                          occurred, normalized, [visible] if visible else [], reply_to=reply_to)
