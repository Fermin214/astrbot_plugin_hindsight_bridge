"""Native QQ C2C contracts; authentication and transport remain in AstrBot."""

import math
import base64
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .source import bound_account, private_event, valid_time as valid_message_time


RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
def native_types(host_type):
    if host_type not in {"qq_official", "qq_official_webhook"}:
        return None
    from astrbot.api.message_components import At
    from botpy.message import C2CMessage

    if host_type == "qq_official":
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter, botClient,
        )

        return C2CMessage, At, QQOfficialPlatformAdapter, botClient, None, QQOfficialMessageEvent
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_adapter import (
        QQOfficialWebhookPlatformAdapter, botClient,
    )
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import (
        QQOfficialWebhookMessageEvent,
    )
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
        QQOfficialWebhook,
    )

    return (
        C2CMessage, At, QQOfficialWebhookPlatformAdapter, botClient,
        QQOfficialWebhook, QQOfficialWebhookMessageEvent,
    )


@dataclass(frozen=True, eq=False)
class QQLogin:
    appid: str
    client: object
    login_object: object
    api: object
    http: object
    login_id: str
    helper: object = None

    def matches(self, other):
        return bool(
            isinstance(other, QQLogin) and self.appid == other.appid
            and self.client is other.client and self.login_object is other.login_object
            and self.api is other.api and self.http is other.http
            and self.login_id == other.login_id and self.helper is other.helper
        )


def native_login(adapter):
    """Read the native producer's authenticated login without token internals."""
    try:
        host_type = adapter.meta().name
        types = native_types(host_type)
        if types is None:
            return None
        _, _, adapter_type, client_type, helper_type, _ = types
        if type(adapter) is not adapter_type:
            return None
        appid, client = adapter.appid, adapter.get_client()
        if (
            not isinstance(appid, str) or not appid.isascii() or not appid.isdecimal()
            or not 0 < len(appid) <= 20 or int(appid) <= 0
            or type(client) is not client_type or client.platform is not adapter
            or adapter.config.get("appid") != appid or client.is_closed()
        ):
            return None
        helper = None
        if helper_type is None:
            login_object = client.robot
            login_id = getattr(login_object, "id", None)
        else:
            helper = adapter.webhook_helper
            if (
                type(helper) is not helper_type or helper.client is not client
                or helper.appid != appid or helper.api is not client.api
                or helper.http is not client.http
            ):
                return None
            login_object = helper.user
            if not isinstance(login_object, dict):
                return None
            login_id = login_object.get("id")
        if (
            type(login_id) not in (str, int) or not str(login_id).strip()
            or login_id in ("0", 0) or type(login_id) is int and login_id < 0
        ):
            return None
        return QQLogin(appid, client, login_object, client.api, client.http,
                       str(login_id), helper)
    except (AttributeError, ImportError, TypeError, ValueError):
        return None


def bound_login(event, adapter, binding):
    if not binding or binding.get("invalid_login") or binding.get("adapter") is not adapter:
        return None
    login = native_login(adapter)
    if not bound_account(event, adapter, binding, login, login.appid if login else "", getattr(event, "bot", None)):
        return None
    return login


def timestamp(value):
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        result = parsed.timestamp()
        if not math.isfinite(result) or not 0 < result <= time.time() + 300:
            return None
        return int(result) if result.is_integer() else result
    except (ValueError, OverflowError, OSError):
        return None


def message_shape(data):
    elements = data.get("msg_elements")
    if data.get("message_reference") is not None or (
        data.get("message_type") in (103, "103") and isinstance(elements, list)
        and elements and isinstance(elements[0], Mapping)
    ):
        return "quoted_or_reply"
    if data.get("attachments") or data.get("msg_elements") or data.get("message_type") != 0:
        return "media_or_non_text"
    return "unsupported_message"


def face_text(content):
    def replace(match):
        encoded = re.search(r'ext="([^"]*)"', match[0])
        if encoded:
            try:
                name = json.loads(base64.b64decode(encoded[1]).decode("utf-8")).get("text")
                if isinstance(name, str) and name:
                    return "[表情:" + name + "]"
            except (ValueError, UnicodeError, AttributeError):
                pass
        return "[表情]"
    return re.sub(r"<faceType=\d+[^>]*>", replace, content)


def header(event, plain_type, wake_prefixes, adapter, binding, *, inspect=None, allow_readonly=False):
    login = bound_login(event, adapter, binding)
    if login is None or not private_event(event):
        return None
    c2c_type, at_type, _, _, _, event_type = native_types(event.get_platform_name())
    raw = getattr(event.message_obj, "raw_message", None)
    if (
        type(event) is not event_type or not isinstance(raw, c2c_type)
        or getattr(raw, "_api", None) is not login.api
    ):
        return None
    data = getattr(raw, "raw_data", None)
    if not isinstance(data, Mapping):
        return None
    if any(data.get(name) for name in ("channel_id", "guild_id", "group_openid")):
        return None
    author = data.get("author")
    if not isinstance(author, Mapping) or author.get("bot") is not False:
        return None
    user, mid, original = author.get("user_openid"), data.get("id"), data.get("content")
    if (
        not all(isinstance(value, str) and value for value in (user, mid))
        or ":" in user or any(ord(char) < 32 or ord(char) == 127 for char in user)
        or mid in {"0", "None"} or user != event.get_sender_id()
        or user != getattr(getattr(raw, "author", None), "user_openid", None)
        or mid != getattr(raw, "id", None) or mid != event.message_obj.message_id
    ):
        return None
    occurred = timestamp(data.get("timestamp"))
    received = getattr(event, "created_at", None)
    valid_time = (
        occurred is not None and data["timestamp"] == getattr(raw, "timestamp", None)
        and valid_message_time(occurred, received)
    )
    if inspect is not None:
        inspect(login.appid, occurred if valid_time else None, received, lambda: message_shape(data))
    if not valid_time:
        return None
    quoted = data.get("message_reference") is not None or data.get("message_type") == 103
    if quoted:
        reference = data.get("message_reference")
        if reference is not None and (
            not isinstance(reference, Mapping) or set(reference) - {"message_id", "ignore_get_message_error"}
            or not isinstance(reference.get("message_id"), str) or not reference["message_id"]
        ):
            return None
        elements = data.get("msg_elements")
        if elements is not None and not isinstance(elements, list):
            return None
        if elements:
            if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], Mapping):
                return None
            if set(elements[0]) - {"id", "message_id", "msg_idx", "message_type", "content", "attachments"}:
                return None
        elif data.get("message_type") == 103:
            return None
    if (
        data.get("parallel_message") is not None or type(data.get("message_type")) is not int
        or data["message_type"] not in (0, 7, 103) or not isinstance(original, str)
        or original != getattr(raw, "content", None) or not original.strip() or len(original) > 8000
    ):
        return None
    if not quoted and data.get("msg_elements") is not None and (
        not isinstance(data["msg_elements"], list) or data["msg_elements"]
    ):
        return None
    components = event.get_messages()
    parts = [part for part in components if type(part).__name__ != "Reply"] if isinstance(components, list) else []
    if (
        not parts or type(parts[0]) is not at_type or parts[0].qq != "qq_official"
        or sum(type(part) is at_type for part in parts) != 1
    ):
        return None
    from .source import finish_message

    visible = face_text(original.strip())
    normalized = [("text", face_text(original))]
    attachments = data.get("attachments")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, Mapping) or not isinstance(attachment.get("content_type"), str):
            return None
        media_type = attachment["content_type"].split("/", 1)[0]
        normalized.append((media_type if media_type in {"image", "audio", "video"} else "file", None))
    reference = data.get("message_reference") or {}
    element = (data.get("msg_elements") or [{}])[0] if quoted else {}
    reply_to = reference.get("message_id") or element.get("id") or element.get("message_id") or ""
    return finish_message(event, plain_type, wake_prefixes, "qq_official", login.appid, user,
                          occurred, normalized, [visible], reply_to=str(reply_to))
