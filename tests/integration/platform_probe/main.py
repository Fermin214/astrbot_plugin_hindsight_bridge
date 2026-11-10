"""Disposable native sampler installed only in a dedicated acceptance host."""

import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Star, register
from astrbot.core.config.default import VERSION
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .probe import LABELS, Samples, producer, project


@register("astrbot_plugin_bridge_platform_probe", "Fermin214", "Platform field sampler", "1.0.0")
class PlatformProbe(Star):
    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        self.samples = Samples()
        self.directory = Path(get_astrbot_data_path()) / "plugin_data" / "bridge_platform_probe"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = self.directory / "fingerprint.key"
        if not key_path.exists():
            self.write(key_path, secrets.token_bytes(32))
        self.key = key_path.read_bytes()

    @staticmethod
    def write(path, content):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(content)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def sample(self, event):
        pair = (event.get_platform_id(), event.get_sender_id())
        if (
            not self.config.get("enabled", False)
            or event.get_platform_name() not in {"weixin_oc", "telegram"}
            or not event.is_private_chat() or event.get_group_id()
            or pair != (self.config.get("platform_instance_id"), self.config.get("user_id"))
        ):
            return
        text = getattr(event.message_obj, "message_str", "")
        adapter = self.context.get_platform_inst(pair[0])
        if text.startswith("/bridge_probe"):
            parts = text.split()
            if len(parts) == 3 and parts[:2] == ["/bridge_probe", "arm"] and parts[2] in LABELS:
                self.samples.arm(
                    pair, parts[2], time.time(), producer(event, adapter, self.key),
                    received_at=event.created_at,
                )
                yield event.plain_result("已准备采样，请在 120 秒内发送一条测试消息。")
            elif parts == ["/bridge_probe", "cancel"]:
                self.samples.armed.pop(pair, None)
                yield event.plain_result("采样已取消。")
            else:
                yield event.plain_result("用法：/bridge_probe arm " + "|".join(sorted(LABELS)))
            event.stop_event()
            return
        selection = self.samples.take(pair, time.time(), getattr(event, "created_at", None))
        if selection is None:
            return
        try:
            report = project(event, adapter, self.key)
            report.update({
                "format": 1, "evidence_layer": "native_platform_event",
                "astrbot": VERSION, "sample": selection[0],
                "producer_at_arm": selection[1],
                "observed_at": time.time(),
            })
            if event.get_platform_name() == "telegram":
                report["python_telegram_bot"] = importlib.metadata.version("python-telegram-bot")
            path = self.directory / (str(time.time_ns()) + "-" + secrets.token_hex(4) + ".json")
            self.write(path, json.dumps(report, ensure_ascii=True, indent=2).encode("utf-8"))
        except Exception as error:
            logger.error("Bridge platform probe: %s", type(error).__name__)
            yield event.plain_result("采样失败，请检查测试插件日志。")
            return
        yield event.plain_result("字段样本已保存。")
