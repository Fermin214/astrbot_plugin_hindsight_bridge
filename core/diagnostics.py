"""Configuration checks and counted, per-error warnings without message content."""

import math
import time


class ConfigurationError(ValueError):
    pass


def connection_fields(config):
    return {
        name: value.strip() if isinstance(value := config.get(name, ""), str) else ""
        for name in ("api_url", "api_key", "service_id")
    }


def missing_connection(config):
    return [name for name, value in connection_fields(config).items() if not value]


def configuration_message(config):
    missing = missing_connection(config)
    if not missing:
        return ""
    if len(missing) == 3:
        return "请在设置中连接记忆服务"
    labels = {"api_url": "API 地址", "api_key": "连接密钥", "service_id": "稳定服务标识"}
    return "缺少" + "、".join(labels[name] for name in missing) + "，请在设置中完成连接"


def recall_timeout(config):
    value = config.get("recall_timeout", 10)
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ConfigurationError("invalid_recall_timeout") from None
    if isinstance(value, bool) or not math.isfinite(seconds) or not 0.2 <= seconds <= 30:
        raise ConfigurationError("invalid_recall_timeout")
    return seconds


def recall_description(value):
    if not value:
        return "尚未检索"
    label = {
        "success": "检索成功", "empty": "无相关结果", "error": "检索失败",
        "skipped": "本轮已跳过",
    }[value["outcome"]]
    if value["outcome"] == "error" and "timeout" in value.get("code", ""):
        label = "检索超时"
    detail = f"{label}；耗时 {value['elapsed_ms'] / 1000:.2f} 秒"
    if value.get("consecutive_failures"):
        detail += f"；连续失败 {value['consecutive_failures']} 次"
    return detail


def recall_notice(code):
    if code in {"recall_timeout", "auth_timeout", "read_timeout", "write_timeout", "pool_timeout", "timeout"}:
        return "Hindsight 本轮检索超时，记忆暂未载入。"
    if code in {"connection_error", "connect_timeout", "http_401", "http_403", "server_auth_not_enforced"}:
        return "Hindsight 连接失败，记忆暂未载入。"
    return "Hindsight 本轮记忆准备失败，记忆暂未载入。"


class WarningLog:
    def __init__(self, logger, *, interval=60):
        self.logger = logger
        self.interval = interval
        self.errors = {}

    def warning(self, code):
        now = time.monotonic()
        state = self.errors.setdefault(code, {"last": None, "count": 0, "suppressed": 0})
        state["count"] += 1
        state["suppressed"] += 1
        if state["last"] is None or now - state["last"] >= self.interval:
            self.logger.warning(
                "Hindsight: %s (occurrences=%s, since_last_log=%s)",
                code, state["count"], state["suppressed"],
            )
            state["last"], state["suppressed"] = now, 0

    def recovered(self, code):
        state = self.errors.pop(code, None)
        if state:
            self.logger.info("Hindsight recovered: %s (occurrences=%s)", code, state["count"])
