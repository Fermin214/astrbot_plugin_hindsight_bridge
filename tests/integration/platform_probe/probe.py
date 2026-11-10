"""Allowlisted field evidence for disposable, explicitly selected test accounts."""

import hashlib
import hmac
import math
from collections.abc import Mapping
from datetime import datetime


LABELS = frozenset({
    "text", "command", "image", "voice", "file", "video", "reply", "forward",
    "formatted", "text-link", "mention", "relogin",
})
WX_FIELDS = (
    "from_user_id", "to_user_id", "message_id", "msg_id", "create_time_ms",
    "create_time", "message_type", "message_state", "item_list", "group_id",
)
TG_FIELDS = (
    "message_id", "date", "from", "chat", "text", "entities", "reply_to_message",
    "forward_origin", "caption", "photo", "voice", "audio", "video", "document",
    "edit_date", "business_connection_id", "sender_business_bot", "sender_chat",
    "is_automatic_forward", "is_topic_message", "message_thread_id",
)
TG_UPDATES = (
    "message", "edited_message", "channel_post", "edited_channel_post",
    "callback_query", "business_message", "edited_business_message",
)
ENTITY_TYPES = frozenset({
    "mention", "hashtag", "cashtag", "bot_command", "url", "email",
    "phone_number", "bold", "italic", "underline", "strikethrough", "spoiler",
    "blockquote", "expandable_blockquote", "code", "pre", "text_link",
    "text_mention", "custom_emoji",
})


def fingerprint(value, key):
    if type(value) not in (str, int) or value == "":
        return None
    return hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def field(value):
    """Describe a value without serializing arbitrary input."""
    return {
        "present": value is not None,
        "type": type(value).__name__ if type(value) in (
            str, int, float, bool, list, tuple, dict, type(None),
        ) else "object",
    }


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def identity(value, key):
    return {
        **field(value), "fingerprint": fingerprint(value, key),
        "above_js_safe_integer": type(value) is int and abs(value) > 2**53 - 1,
    }


def producer(event, adapter, key):
    host = event.get_platform_name()
    if adapter is None:
        return {"available": False}
    if host == "weixin_oc":
        return {
            "available": bool(getattr(adapter, "account_id", None)),
            "account": identity(getattr(adapter, "account_id", None), key),
            "event_adapter_matches": getattr(event, "platform", None) is adapter,
        }
    if host == "telegram":
        client = adapter.get_client()
        try:
            bot_id = client.id
        except (AttributeError, RuntimeError):
            bot_id = None
        return {
            "available": bot_id is not None,
            "account": identity(bot_id, key),
            "event_client_matches": getattr(event, "client", None) is client,
        }
    return {"available": False}


def project(event, adapter, key):
    """Project only known scalar properties; never dump raw objects or to_dict()."""
    host = event.get_platform_name()
    message = event.message_obj
    raw = message.raw_message
    components = event.get_messages()
    report = {
        "platform": host, "instance": fingerprint(event.get_platform_id(), key),
        "sender": identity(event.get_sender_id(), key),
        "host_message_id": identity(message.message_id, key),
        "host_timestamp": numeric(message.timestamp),
        "host_received_at": numeric(getattr(event, "created_at", None)),
        "private": event.is_private_chat(), "has_group": bool(event.get_group_id()),
        "component_types": [
            type(part).__name__ if type(part).__module__.startswith("astrbot.") else "other"
            for part in components[:32]
        ],
        "component_count": len(components),
        "producer_at_observation": producer(event, adapter, key),
    }
    host_text = event.get_message_str()
    plain = [getattr(part, "text", None) for part in components if type(part).__name__ == "Plain"]
    report["host_text"] = {
        "length": len(host_text) if isinstance(host_text, str) else None,
        "fingerprint": fingerprint(host_text, key),
    }
    report["all_components_plain"] = len(plain) == len(components)
    if host == "weixin_oc":
        if not isinstance(raw, Mapping):
            return {**report, "raw_mapping": False}
        items = raw.get("item_list")
        report["fields"] = {name: field(raw.get(name)) for name in WX_FIELDS}
        report["identifiers"] = {
            name: identity(raw.get(name), key)
            for name in ("from_user_id", "to_user_id", "message_id", "msg_id")
        }
        report["raw_id_matches_host"] = (
            type(raw.get("message_id")) in (str, int)
            and str(raw["message_id"]) == message.message_id
        )
        report["sender_matches_host"] = raw.get("from_user_id") == event.get_sender_id()
        report["recipient_matches_observed_account"] = (
            adapter is not None and bool(getattr(adapter, "account_id", None))
            and raw.get("to_user_id") == adapter.account_id
        )
        milliseconds = numeric(raw.get("create_time_ms"))
        report["raw_timestamp_seconds"] = milliseconds / 1000 if milliseconds is not None else None
        for name in ("message_type", "message_state"):
            value = raw.get(name)
            report[name] = value if type(value) is int and 0 <= value <= 100 else None
        texts, item_reports = [], []
        if isinstance(items, list):
            for item in items[:32]:
                if not isinstance(item, Mapping):
                    item_reports.append({"mapping": False})
                    continue
                kind = item.get("type")
                text_item = item.get("text_item")
                text = text_item.get("text") if isinstance(text_item, Mapping) else None
                item_reports.append({
                    "type": kind if type(kind) is int and 0 <= kind <= 100 else None,
                    "has_reference": bool(item.get("ref_msg")),
                    "has_voice": bool(item.get("voice_item")),
                    "text": {**field(text), "length": len(text) if isinstance(text, str) else None},
                })
                if kind == 1 and isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        report["items"] = item_reports
        report["item_count"] = len(items) if isinstance(items, list) else None
        report["text_transform_matches_host"] = "\n".join(texts).strip() == host_text
        report["text_components_match"] = texts == plain
    elif host == "telegram":
        report["update_fields"] = {
            name: getattr(raw, name, None) is not None for name in TG_UPDATES
        }
        msg = getattr(raw, "message", None)
        if msg is None:
            return report
        sender, chat = getattr(msg, "from_user", None), getattr(msg, "chat", None)
        report["fields"] = {
            name: field(getattr(msg, "from_user" if name == "from" else name, None))
            for name in TG_FIELDS
        }
        report["chat"] = identity(getattr(chat, "id", None), key)
        chat_type = getattr(chat, "type", None)
        report["chat_type"] = chat_type if chat_type in ("private", "group", "supergroup", "channel") else "other"
        report["chat_matches_sender"] = bool(sender and chat and chat.id == sender.id)
        report["sender_matches_host"] = bool(sender and str(sender.id) == event.get_sender_id())
        report["sender_is_bot"] = getattr(sender, "is_bot", None) is True
        report["raw_id_matches_host"] = str(getattr(msg, "message_id", None)) == message.message_id
        date = getattr(msg, "date", None)
        report["raw_timestamp_seconds"] = date.timestamp() if (
            isinstance(date, datetime) and date.utcoffset() is not None
        ) else None
        text = getattr(msg, "text", None)
        report["text_matches_host"] = isinstance(text, str) and text == host_text
        report["text_components_match"] = plain == [text]
        report["entities"] = [
            entity.type if entity.type in ENTITY_TYPES else "other"
            for entity in (getattr(msg, "entities", None) or ())[:32]
        ]
        try:
            report["raw_client_matches_event"] = raw.get_bot() is getattr(event, "client", None)
        except RuntimeError:
            report["raw_client_matches_event"] = False
    return report


class Samples:
    """One next event per explicit arm; reloading loses all armed selections."""

    def __init__(self):
        self.armed = {}

    def arm(self, pair, label, now, login, *, received_at):
        if label not in LABELS:
            raise ValueError("unknown sample label")
        self.armed[pair] = (label, now + 120, login, received_at)

    def take(self, pair, now, received_at):
        armed = self.armed.pop(pair, None)
        if (
            armed is None or now > armed[1] or type(received_at) not in (int, float)
            or not math.isfinite(received_at) or not armed[3] < received_at <= now + 1
        ):
            return None
        return armed[0], armed[2]
