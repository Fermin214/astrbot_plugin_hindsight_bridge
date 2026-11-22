"""Feishu ingress through AstrBot's native Lark event and API client."""

import json
import re
import time
from dataclasses import dataclass

from .source import bound_account, identifier, private_event, valid_time as valid_message_time


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
    if (not bound_account(event, adapter, binding, login, login.bot_id if login else "", getattr(event, "bot", None),
                          login.api if login else None) or event.get_self_id() != login.open_id):
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
        or not private_event(event)
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
        and valid_message_time(occurred / 1000, received)
    )
    if inspect is not None:
        inspect(login.bot_id, occurred / 1000 if valid_time else None, received, lambda: message_shape(raw))
    if (
        not valid_time or raw.message_type not in {"text", "post", "image", "audio", "file", "media"}
        or any(value and not identifier(value) for value in (raw.parent_id, raw.root_id))
        or getattr(raw, "thread_id", None) and not identifier(raw.thread_id)
        or getattr(raw, "lark_agent_context", None) is not None
        or not isinstance(raw.content, str) or len(raw.content) > 64_000
    ):
        return None
    try:
        content = json.loads(raw.content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(content, dict):
        return None
    from .source import finish_message

    mentions = {}
    for mention in raw.mentions or ():
        key = getattr(mention, "key", None)
        identity = getattr(getattr(mention, "id", None), "open_id", None)
        name = getattr(mention, "name", None)
        if not identifier(key) or not identifier(identity) or not isinstance(name, str):
            return None
        mentions[key] = (identity, name)
    normalized, texts, expected_mentions = [], [], []
    if raw.message_type == "text":
        if set(content) != {"text"} or not isinstance(content["text"], str):
            return None
        for text in re.split(r"(@_user_\d+)", content["text"]):
            if text in mentions:
                identity, name = mentions[text]
                expected_mentions.append((identity, name))
                normalized.append(("text", "@" + (name or identity)))
            elif text.strip():
                texts.append(text.strip())
                normalized.append(("text", text))
    elif raw.message_type == "post":
        title, paragraphs = content.get("title", ""), content.get("content", [])
        if not isinstance(title, str) or not isinstance(paragraphs, list):
            return None
        elements = [{"tag": "text", "text": title}] if title else []
        for index, paragraph in enumerate(paragraphs):
            if index or title:
                elements.append({"tag": "text", "text": "\n"})
            if isinstance(paragraph, list):
                elements.extend(paragraph)
            elif isinstance(paragraph, dict):
                elements.append(paragraph)
            else:
                return None
        for element in elements:
            if not isinstance(element, dict):
                return None
            kind = element.get("tag")
            if kind in {"text", "a"}:
                text = element.get("text", "")
                if not isinstance(text, str):
                    return None
                if kind == "a" and element.get("href"):
                    if not isinstance(element["href"], str):
                        return None
                    text += "(" + element["href"].strip() + ")"
                if text:
                    texts.append(text)
                    normalized.append(("text", text))
            elif kind in {"img", "media"}:
                normalized.append(("image" if kind == "img" else "video", None))
            elif kind == "at" and element.get("user_id") in mentions:
                identity, name = mentions[element["user_id"]]
                expected_mentions.append((identity, name))
                normalized.append(("text", "@" + (name or identity)))
            else:
                return None
    else:
        normalized.append(({"image": "image", "audio": "audio", "file": "file", "media": "video"}[raw.message_type], None))
    parts = event.get_messages()
    if not isinstance(parts, list) or [(part.qq, part.name) for part in parts if type(part).__name__ == "At"] != expected_mentions:
        return None
    visible, leading = [], True
    for part in parts:
        kind = type(part).__name__
        if kind == "Reply":
            continue
        if leading and kind == "At" and part.qq == login.open_id:
            leading = False
            continue
        if kind == "At":
            visible.append("@" + (part.name or str(part.qq)))
        elif type(part) is plain_type:
            text = part.text if raw.message_type == "post" else part.text.strip()
            if text:
                visible.append(text)
        elif kind in {"Image", "Record", "Video", "File"}:
            visible.append({"Image": "[image]", "Record": "[audio]", "Video": "[video]",
                            "File": getattr(part, "name", "") or "[file]"}[kind])
        if kind != "Plain" or part.text.strip():
            leading = False
    return finish_message(event, plain_type, wake_prefixes, "lark", login.bot_id, user,
                          occurred / 1000, normalized, texts,
                          visible_text=("" if raw.message_type == "post" else " ").join(visible),
                          reply_to=raw.parent_id or raw.root_id or "")
