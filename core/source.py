from dataclasses import dataclass
from collections.abc import Mapping
import json
import math
import time
from types import ModuleType

from .identity import Incoming


@dataclass(frozen=True)
class Platform:
    logical_type: str
    user_label: str
    user_hint: str
    source: ModuleType
    numeric: bool = False
    login_id: str = ""
    app_id_setting: str = ""
    pin_client_login: bool = False

    def bot_id(self, login, record=None):
        if not login or (record is not None and self.app_id_setting
                         and login.appid != record.get(self.app_id_setting)):
            return ""
        return getattr(login, self.login_id)


TIME_SOURCES = {
    "aiocqhttp": "onebot.time",
    "qq_official": "qq_official.timestamp",
    "weixin_oc": "weixin_oc.create_time_ms",
    "lark": "lark.create_time",
    "telegram": "telegram.message.date",
}
MEDIA_MARKERS = {
    "mface": "[表情包]", "sticker": "[表情包]", "image": "[图片]",
    "audio": "[语音]", "voice": "[语音]", "record": "[语音]",
    "file": "[文件]", "video": "[视频]",
}


def valid_time(occurred, received):
    return all(
        type(value) in (int, float) and math.isfinite(value) and 0 < value <= time.time() + 300
        for value in (occurred, received)
    )


def private_event(event):
    return event.is_private_chat() and not event.get_group_id()


def identifier(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and ":" not in value and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def bound_account(event, adapter, binding, login, bot_id, producer, expected=None):
    previous = binding.get("login") if binding else None
    return bool(
        login is not None and binding and not binding.get("invalid_login")
        and binding.get("adapter") is adapter and type(previous) is type(login) and previous.matches(login)
        and binding.get("bot_id") == bot_id and binding.get("client") is adapter.get_client()
        and producer is (login.client if expected is None else expected)
        and event.get_platform_id() == adapter.meta().id and event.get_platform_name() == adapter.meta().name
    )


def visible_matches(event, expected, wake_prefixes):
    actual = event.get_message_str()
    if not isinstance(actual, str):
        return False
    expected, actual = expected.strip(), actual.strip()
    return expected == actual or (
        getattr(event, "is_at_or_wake_command", False) is True
        and any(expected.startswith(prefix) and expected[len(prefix):].strip() == actual
                for prefix in wake_prefixes if isinstance(prefix, str) and prefix)
    )


def excluded_prefix(text, prefixes):
    return any(text.lstrip().startswith(prefix) for prefix in ("/", *prefixes) if prefix)


def extract_segments(segments):
    pieces, media, meaningful = [], [], False
    for kind, value in segments:
        if kind == "text":
            if not isinstance(value, str):
                return None
            pieces.append(value)
            meaningful = meaningful or bool(value.strip())
        elif kind == "emoji":
            name = value.strip()[:64] if isinstance(value, str) else ""
            pieces.append("[表情:" + name + "]" if name else "[表情]")
            meaningful = True
        elif kind in MEDIA_MARKERS:
            pieces.append(MEDIA_MARKERS[kind])
            media.append(kind)
        else:
            return None
    text = "".join(pieces)
    return (text, tuple(dict.fromkeys(media))) if meaningful and len(text) <= 8000 else None


def message_media(event):
    raw = getattr(event.message_obj, "raw_message", None)
    if event.get_platform_name() == "aiocqhttp" and isinstance(raw, dict):
        segments = raw.get("message")
        if not isinstance(segments, list):
            return ()
        return tuple(dict.fromkeys(part.get("type") for part in segments
                                   if isinstance(part, dict) and isinstance(part.get("type"), str) and part["type"] in MEDIA_MARKERS))
    if event.get_platform_name() == "weixin_oc" and isinstance(raw, dict):
        kinds = {2: "image", 3: "voice", 4: "file", 5: "video"}
        items = raw.get("item_list")
        if not isinstance(items, list):
            return ()
        return tuple(dict.fromkeys(kinds[part["type"]] for part in items
                                   if isinstance(part, dict) and type(part.get("type")) is int and part["type"] in kinds))
    if event.get_platform_name() == "telegram":
        message = getattr(raw, "message", None)
        return tuple(kind for field, kind in (("photo", "image"), ("sticker", "sticker"),
                                             ("animation", "sticker"), ("voice", "voice"),
                                             ("audio", "audio"), ("document", "file"), ("video", "video"))
                     if getattr(message, field, None))
    if event.get_platform_name().startswith("qq_official"):
        data = getattr(raw, "raw_data", {})
        if not isinstance(data, Mapping) or not isinstance(data.get("attachments", []), (list, type(None))):
            return ()
        return tuple(dict.fromkeys(
            part["content_type"].split("/", 1)[0] if part["content_type"].split("/", 1)[0] in {"image", "audio", "video"} else "file"
            for part in data.get("attachments", []) or () if isinstance(part, dict) and isinstance(part.get("content_type"), str)
        ))
    if event.get_platform_name() == "lark":
        if getattr(raw, "message_type", "") == "post":
            try:
                content = json.loads(raw.content)
                paragraphs = content.get("content", []) if isinstance(content, dict) else []
                if not isinstance(paragraphs, list):
                    return ()
                kinds = {"img": "image", "media": "video"}
                return tuple(dict.fromkeys(kinds[element["tag"]] for paragraph in paragraphs
                                          for element in (paragraph if isinstance(paragraph, list) else [paragraph])
                                          if isinstance(element, dict) and element.get("tag") in kinds))
            except (ValueError, TypeError, RecursionError):
                return ()
        kind = {"image": "image", "audio": "audio", "file": "file", "media": "video"}.get(getattr(raw, "message_type", ""))
        return (kind,) if kind else ()
    return ()


def finish_message(event, plain_type, wake_prefixes, platform_type, bot_id, user_id,
                   occurred, segments, host_parts, *, visible_text=None, host_separator="", reply_to=""):
    extracted = extract_segments(segments)
    components = event.get_messages()
    if extracted is None or not isinstance(components, list) or not components:
        return None
    plain = [part.text for part in components if type(part) is plain_type]
    if plain != host_parts or any(not isinstance(text, str) for text in plain):
        return None
    if any(type(part) is not plain_type and type(part).__name__ not in {
        "Face", "Image", "Record", "Video", "File", "At", "Reply",
    } for part in components):
        return None
    host_text = host_separator.join(plain) if visible_text is None else visible_text
    if not visible_matches(event, host_text, wake_prefixes):
        return None
    host_text = event.get_message_str()
    text, media = extracted
    return Incoming(platform_type, event.get_platform_id(), bot_id, user_id,
                    event.message_obj.message_id, occurred, text, received_at=event.created_at,
                    host_text=host_text, reply_to=reply_to, media_types=media)


def platform_details(host_type):
    contract = PLATFORMS.get(host_type)
    return {
        "type": host_type,
        "logical_type": contract.logical_type if contract else host_type,
        "supported": contract is not None,
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
    if host_type == "lark":
        return value.startswith("ou_") and len(value) > 3 and value == value.strip()
    return not contract.numeric or (value.isascii() and value.isdecimal() and len(value) <= 20)


def native_header(event, plain_type, wake_prefixes=(), *, adapter=None, binding=None,
                  allow_readonly=False) -> Incoming | None:
    return inspect_header(event, plain_type, wake_prefixes, adapter=adapter, binding=binding,
                          allow_readonly=allow_readonly)


def inspect_header(event, plain_type, wake_prefixes, *, adapter, binding, inspect=None, allow_readonly=False):
    contract = PLATFORMS.get(event.get_platform_name())
    if not contract:
        return None
    return contract.source.header(event, plain_type, wake_prefixes, adapter, binding,
                                  inspect=inspect, allow_readonly=allow_readonly)


def native_login(adapter):
    """Read each native adapter's login without keeping its credentials."""
    try:
        contract = PLATFORMS.get(adapter.meta().name)
        return contract.source.native_login(adapter) if contract and contract.login_id else None
    except AttributeError:
        return None


def pin_client_login(host_type):
    contract = PLATFORMS.get(host_type)
    return bool(contract and contract.pin_client_login)


def platform_record(context, platform):
    return next((row for row in context.get_config().get("platform", [])
                 if str(row.get("id")) == platform), {})


async def prepare_bot_identity(context, platform):
    """A bounded read for management actions; never called by synchronous checks."""
    record = platform_record(context, platform)
    contract = PLATFORMS.get(record.get("type"))
    if not contract or not record.get("enable", True):
        return ""
    getter = getattr(context, "get_platform_inst", None)
    adapter = getter(platform) if getter else None
    if adapter is None:
        return ""
    if contract.login_id:
        return contract.bot_id(contract.source.native_login(adapter), record)
    return await contract.source.bot_identity(adapter, platform, getter)


async def management_bot_identity(plugin, platform):
    contract = PLATFORMS.get(platform_record(plugin.context, platform).get("type"))
    if contract and contract.login_id:
        await plugin.prepare_sources()
        return plugin.source_accounts.get(platform, {}).get("bot_id", "")
    return await prepare_bot_identity(plugin.context, platform)


def local_runner(config) -> bool:
    return config.get("agent_runner", {}).get("runner_type", "local") == "local"


from . import lark_source, onebot_source, qq_source, telegram_source, weixin_source

QQ_OFFICIAL = Platform(
    "qq_official", "用户 OpenID", "填写 /sid 中的用户 UID（OpenID，不是 QQ 号）",
    qq_source, login_id="appid", app_id_setting="appid", pin_client_login=True,
)
PLATFORMS = {
    "aiocqhttp": Platform("aiocqhttp", "用户 QQ 号", "与这个机器人私聊的 QQ 号", onebot_source, numeric=True),
    "qq_official": QQ_OFFICIAL,
    "qq_official_webhook": QQ_OFFICIAL,
    "weixin_oc": Platform("weixin_oc", "ClawBot 用户 UID", "填写微信私聊 /sid 中的用户 UID", weixin_source, login_id="account_id"),
    "lark": Platform("lark", "用户 OpenID", "填写飞书私聊 /sid 中的用户 UID（ou_ 开头）", lark_source,
                     login_id="bot_id", app_id_setting="app_id"),
    "telegram": Platform("telegram", "Telegram 用户 ID", "填写 Telegram 私聊 /sid 中的用户 UID", telegram_source,
                         numeric=True, login_id="bot_id"),
}
