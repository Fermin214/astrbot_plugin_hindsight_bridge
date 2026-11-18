"""Small, deterministic ingress contracts; deliberately has no AstrBot imports."""

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
    "weixin_oc": Platform(
        "weixin_oc", "ClawBot 用户 UID", "填写微信私聊 /sid 中的用户 UID",
        supported=True,
    ),
    "lark": Platform(
        "lark", "用户 OpenID", "填写飞书私聊 /sid 中的用户 UID（ou_ 开头）",
        supported=True,
    ),
    "telegram": Platform(
        "telegram", "Telegram 用户 ID", "填写 Telegram 私聊 /sid 中的用户 UID",
        True, True,
    ),
}
TIME_SOURCES = {
    "aiocqhttp": "onebot.time",
    "qq_official": "qq_official.timestamp",
    "weixin_oc": "weixin_oc.create_time_ms",
    "lark": "lark.create_time",
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
    if host_type == "lark":
        return value.startswith("ou_") and len(value) > 3 and value == value.strip()
    return not contract.numeric or (value.isascii() and value.isdecimal() and len(value) <= 20)


def native_header(event, plain_type, wake_prefixes=(), *, adapter=None, binding=None) -> Incoming | None:
    """Validate original identity, time and text for both commands and capture."""
    host_type = event.get_platform_name()
    if host_type == "lark":
        from .lark_source import header

        return header(event, plain_type, wake_prefixes, adapter, binding)
    if host_type in {"qq_official", "qq_official_webhook"}:
        from .qq_source import header

        if not PLATFORMS[host_type].supported:
            return None
        return header(event, plain_type, wake_prefixes, adapter, binding)
    if host_type == "weixin_oc":
        from .weixin_source import header

        return header(event, plain_type, wake_prefixes, adapter, binding)
    if host_type == "telegram":
        from .telegram_source import header

        return header(event, plain_type, wake_prefixes, adapter, binding)
    if host_type == "aiocqhttp":
        from .onebot_source import header

        return header(event, plain_type, wake_prefixes, adapter)
    return None


def native_snapshot(event, plain_type, command_prefixes=("/",)) -> Incoming | None:
    incoming = native_header(event, plain_type)
    if incoming and not any(
        incoming.text.lstrip().startswith(prefix) for prefix in command_prefixes if prefix
    ):
        return incoming
    return None


def source_identity(event, incoming, adapter=None, *, authorization_revision=0, binding=None):
    """OneBot's original recipient is producer evidence, including queued events."""
    if incoming.platform_type == "lark":
        from .lark_source import bound_login

        login = bound_login(event, adapter, binding)
        if login is None or incoming.bot_id != login.bot_id:
            return None
        return SourceIdentity(
            "lark", incoming.platform_instance_id, "lark",
            login.bot_id, login.api, authorization_revision,
        )
    if incoming.platform_type == "qq_official":
        from .qq_source import bound_login

        login = bound_login(event, adapter, binding)
        if login is None or incoming.bot_id != login.appid:
            return None
        return SourceIdentity(
            event.get_platform_name(), incoming.platform_instance_id, "qq_official",
            login.appid, login.client, authorization_revision,
        )
    if incoming.platform_type == "weixin_oc":
        from .weixin_source import bound_login

        login = bound_login(event, adapter, binding)
        if login is None or incoming.bot_id != login.account_id:
            return None
        return SourceIdentity(
            "weixin_oc", incoming.platform_instance_id, "weixin_oc",
            login.account_id, login.client, authorization_revision,
        )
    if incoming.platform_type == "telegram":
        from .telegram_source import bound_login

        login = bound_login(event, adapter, binding)
        if login is None or incoming.bot_id != login.bot_id:
            return None
        return SourceIdentity(
            "telegram", incoming.platform_instance_id, "telegram",
            login.bot_id, login.client, authorization_revision,
        )
    if incoming.platform_type != "aiocqhttp":
        return None
    if adapter is None:
        producer = getattr(event, "bot", None)
    else:
        from .onebot_source import bound_client

        producer = bound_client(event, adapter)
        if producer is None:
            return None
    return SourceIdentity(
        "aiocqhttp", incoming.platform_instance_id, "aiocqhttp",
        incoming.bot_id, producer, authorization_revision,
    )


def native_login(adapter):
    """Read each native adapter's login without keeping its credentials."""
    try:
        if adapter.meta().name == "lark":
            from .lark_source import native_login as lark_login

            return lark_login(adapter)
        if adapter.meta().name == "weixin_oc":
            from .weixin_source import native_login as weixin_login

            return weixin_login(adapter)
        if adapter.meta().name == "telegram":
            from .telegram_source import native_login as telegram_login

            return telegram_login(adapter)
    except AttributeError:
        return None
    from .qq_source import native_login as qq_login

    return qq_login(adapter)


async def prepare_bot_identity(context, platform):
    """A bounded read for management actions; never called by synchronous checks."""
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
    if record.get("type") == "weixin_oc":
        from .weixin_source import native_login

        login = native_login(adapter)
        return login.account_id if login else ""
    if record.get("type") == "lark":
        from .lark_source import native_login

        login = native_login(adapter)
        return login.bot_id if login and login.appid == record.get("app_id") else ""
    if record.get("type") == "telegram":
        from .telegram_source import native_login

        login = native_login(adapter)
        return login.bot_id if login else ""
    if record.get("type") == "aiocqhttp":
        from .onebot_source import bot_identity

        return await bot_identity(adapter, platform, getter)
    return ""


def local_runner(config) -> bool:
    return config.get("agent_runner", {}).get("runner_type", "local") == "local"
