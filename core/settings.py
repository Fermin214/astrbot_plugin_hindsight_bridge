"""Panel validation for the plugin's local memory parameters."""

import copy
import math

import tiktoken

from .materials import load_encoding


# These bounds match the values consumed by the recall and outbox paths.
NUMBERS = {
    "background_max_tokens": ("知识页长度上限", int, 64, 8192, 400),
    "recall_max_tokens": ("共享 token 上限", int, 256, 8192, 1200),
    "context_max_chars": ("共享字符上限", int, 256, 32768, 4000),
    "recall_timeout": ("记忆检索等待时间", float, 0.2, 30, 10),
    "background_timeout": ("知识页读取等待时间", float, 0.05, 10, 1),
    "recall_query_tokens": ("查询 token 上限", int, 32, 400, 380),
    "queue_max_tasks": ("待保存记录数量上限", int, 1, 2**53 - 1, 10000),
    "queue_max_mib": ("待保存内容容量上限", int, 1, 2**53 - 1, 64),
}
DEFAULTS = {
    **{key: spec[4] for key, spec in NUMBERS.items()},
    "query_tokenizer": "o200k_base",
    "command_prefixes": [],
}


def values(config):
    return {key: copy.deepcopy(config.get(key, default)) for key, default in DEFAULTS.items()}


async def validate(payload):
    if not isinstance(payload, dict) or set(payload) != set(DEFAULTS):
        raise ValueError("记忆参数不完整或含未知项目，请重新加载记忆设置后保存。")
    result = copy.deepcopy(payload)
    for key, (label, kind, low, high, _) in NUMBERS.items():
        value = result[key]
        valid_type = type(value) is int if kind is int else type(value) in (int, float)
        if not valid_type or not low <= value <= high or not math.isfinite(value):
            raise ValueError(f"「{label}」须为 {low} 到 {high} 的{'整数' if kind is int else '数值'}，请修改后保存。")
    tokenizer = result["query_tokenizer"]
    if not isinstance(tokenizer, str) or tokenizer not in tiktoken.list_encoding_names():
        raise ValueError("「查询分词编码」无效，请填写 tiktoken 支持的编码名称，例如 o200k_base。")
    prefixes = result["command_prefixes"]
    if not isinstance(prefixes, list) or any(
        not isinstance(prefix, str) or not prefix or prefix != prefix.strip()
        or "\n" in prefix or "\r" in prefix
        for prefix in prefixes
    ):
        raise ValueError("「额外排除的命令前缀」须为非空文本列表，每行一个前缀，请删除空白或换行后保存。")
    await load_encoding(tokenizer)
    return result


def snapshot(plugin):
    stored = values(plugin.config)
    effective = dict(stored)
    for key, low, high in (
        ("recall_max_tokens", 256, 8192), ("context_max_chars", 256, 32768),
        ("recall_query_tokens", 32, 400),
    ):
        try:
            effective[key] = max(low, min(high, int(stored[key])))
        except (TypeError, ValueError, OverflowError):
            effective[key] = None
    if plugin.client:
        effective["query_tokenizer"] = getattr(plugin.client, "tokenizer", stored["query_tokenizer"])
    if plugin.outbox:
        effective["queue_max_tasks"] = getattr(plugin.outbox, "max_tasks", stored["queue_max_tasks"])
        effective["queue_max_mib"] = getattr(plugin.outbox, "max_bytes", stored["queue_max_mib"] * 1024 * 1024) // (1024 * 1024)
    return {
        "values": stored,
        "effective": effective,
        "running": bool(plugin.client and plugin.outbox and not plugin.stopping),
        "deployment": {
            key: plugin.config.get(key, default) for key, default in (
                ("service_id", ""), ("agent_id", "default"),
                ("service_address_revision", 1), ("control_state_dir", ""),
            )
        },
    }


def apply(plugin, parameters):
    if plugin.client:
        plugin.client.tokenizer = parameters["query_tokenizer"]
    if plugin.outbox:
        plugin.outbox.max_tasks = parameters["queue_max_tasks"]
        plugin.outbox.max_bytes = parameters["queue_max_mib"] * 1024 * 1024
