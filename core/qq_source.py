"""Native QQ C2C contracts; authentication and transport remain in AstrBot."""

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .identity import Incoming


RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
TEXT_FIELDS = frozenset({
    "author", "id", "timestamp", "content", "message_type", "message_scene",
    "attachments", "msg_elements", "message_reference",
})


def native_types(host_type):
    if host_type not in {"qq_official", "qq_official_webhook"}:
        return None
    from astrbot.api.message_components import At
    from botpy.message import C2CMessage

    if host_type == "qq_official":
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter, botClient,
        )

        return C2CMessage, At, QQOfficialPlatformAdapter, botClient, None
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_adapter import (
        QQOfficialWebhookPlatformAdapter, botClient,
    )
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
        QQOfficialWebhook,
    )

    return C2CMessage, At, QQOfficialWebhookPlatformAdapter, botClient, QQOfficialWebhook


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
        _, _, adapter_type, client_type, helper_type = types
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
    previous = binding.get("login")
    if (
        login is None or not isinstance(previous, QQLogin) or not previous.matches(login)
        or binding.get("bot_id") != login.appid
        or binding.get("client") is not login.client
        or getattr(event, "bot", None) is not login.client
        or event.get_platform_id() != adapter.meta().id
        or event.get_platform_name() != adapter.meta().name
    ):
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


def header(event, plain_type, wake_prefixes, adapter, binding):
    login = bound_login(event, adapter, binding)
    if login is None or not event.is_private_chat() or event.get_group_id():
        return None
    c2c_type, at_type, _, _, _ = native_types(event.get_platform_name())
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, c2c_type) or getattr(raw, "_api", None) is not login.api:
        return None
    data = getattr(raw, "raw_data", None)
    if not isinstance(data, Mapping) or set(data) - TEXT_FIELDS:
        return None
    if type(data.get("message_type")) is not int or data["message_type"] != 0:
        return None
    for name in ("attachments", "msg_elements"):
        if name in data and data[name] is not None:
            if not isinstance(data[name], list) or data[name]:
                return None
    if data.get("message_reference") is not None:
        return None
    author = data.get("author")
    if not isinstance(author, Mapping) or author.get("bot") is not False:
        return None
    user, mid, original = author.get("user_openid"), data.get("id"), data.get("content")
    if (
        not all(isinstance(value, str) and value for value in (user, mid, original))
        or ":" in user or any(ord(char) < 32 or ord(char) == 127 for char in user)
        or mid in {"0", "None"} or user != event.get_sender_id()
        or user != getattr(getattr(raw, "author", None), "user_openid", None)
        or mid != getattr(raw, "id", None) or mid != event.message_obj.message_id
        or original != getattr(raw, "content", None)
        or not original.strip() or len(original) > 8000
    ):
        return None
    components = event.get_messages()
    if (
        not isinstance(components, list) or len(components) != 2
        or type(components[0]) is not at_type or components[0].qq != "qq_official"
        or type(components[1]) is not plain_type
        or original.strip() != components[1].text
    ):
        return None
    host_text = event.get_message_str()
    if not isinstance(host_text, str):
        return None
    matching = original.strip() == host_text.strip() or (
        getattr(event, "is_at_or_wake_command", False) is True
        and any(
            original.strip().startswith(prefix)
            and original.strip()[len(prefix):].strip() == host_text.strip()
            for prefix in wake_prefixes if isinstance(prefix, str) and prefix
        )
    )
    occurred = timestamp(data.get("timestamp"))
    if not matching or occurred is None or data["timestamp"] != getattr(raw, "timestamp", None):
        return None
    received = getattr(event, "created_at", None)
    if (
        type(received) not in (int, float) or not math.isfinite(received)
        or not 0 < received <= time.time() + 1
    ):
        return None
    return Incoming("qq_official", event.get_platform_id(), login.appid,
                    user, mid, occurred, original, received_at=received)
