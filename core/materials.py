"""Frozen user windows and bounded recall queries; no additional model calls."""

import asyncio
import json
import pickle
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import tiktoken

from .identity import Scope, canonical, sha256
from .source import TIME_SOURCES

FORMAT = "user_window_v1"
DEFAULT_MEMORY_PROMPT = "Hindsight 长期记忆：插件已根据本轮对话自动检索，相关内容如下："
_loaded_encodings = {}
_ENCODING_LOADER = """
import pickle, sys, tiktoken
value = tiktoken.get_encoding(sys.argv[1])
pickle.dump(dict(name=value.name, pat_str=value._pat_str,
                 mergeable_ranks=value._mergeable_ranks,
                 special_tokens=value._special_tokens), sys.stdout.buffer)
"""


class TokenizerError(ValueError):
    pass


async def load_encoding(name="o200k_base", *, timeout=20):
    if name in _loaded_encodings:
        return _loaded_encodings[name]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _ENCODING_LOADER, name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate()
        if process.returncode:
            raise TokenizerError("tokenizer_load_failed")
        # Named Encoding pickles call get_encoding again; transfer only the
        # constructor data from our child so the host never starts a download.
        value = tiktoken.Encoding(**pickle.loads(output))
        _loaded_encodings[name] = value
        return value
    except TimeoutError:
        raise TokenizerError("tokenizer_timeout") from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()


@lru_cache(maxsize=3)
def encoding(name="o200k_base"):
    return _loaded_encodings.get(name) or tiktoken.get_encoding(name)


def token_count(text: str, name="o200k_base") -> int:
    return len(encoding(name).encode(text, disallowed_special=()))


def shorten(text: str, budget: int, name="o200k_base", *, keep_tail=False) -> str:
    if token_count(text, name) <= budget:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        n = (lo + hi) // 2
        head, tail = (n + 1) // 2, n // 2
        candidate = (
            text[:head] + " … " + (text[-tail:] if tail else "")
            if keep_tail
            else text[:n] + " … "
        )
        if token_count(candidate, name) <= budget:
            best, lo = candidate, n + 1
        else:
            hi = n - 1
    return best


def recall_query(current: str, history=(), *, budget=380, name="o200k_base") -> str:
    if not 32 <= budget <= 400:
        raise ValueError("invalid_query_budget")
    title = "当前问题：\n"
    question = title + shorten(
        current, budget - token_count(title, name), name, keep_tail=True
    )
    selected = []
    prefix = "\n最近对话（仅检索线索）：\n"
    for role, text in reversed(list(history)[-4:]):
        item = f"{role}: {text}"
        candidate = question + prefix + "\n".join([item, *selected])
        if token_count(candidate, name) <= budget:
            selected.insert(0, item)
        elif not selected:
            overhead = token_count(question + prefix + f"{role}: ", name)
            clipped = shorten(text, max(0, budget - overhead - 3), name)
            if clipped:
                candidate = question + prefix + f"{role}: {clipped}"
                if token_count(candidate, name) <= budget:
                    selected.insert(0, f"{role}: {clipped}")
    result = question + (prefix + "\n".join(selected) if selected else "")
    if token_count(result, name) > budget:
        raise ValueError("query_budget_exceeded")
    return result


@dataclass(frozen=True)
class Message:
    event_key: str
    text: str
    timestamp: int | float
    role: str = "user"

    def iso_time(self, zone: str | None = None) -> str:
        occurred = datetime.fromtimestamp(self.timestamp, UTC)
        return occurred.astimezone(
            ZoneInfo(zone) if zone is not None else None
        ).isoformat()


def retain_payload(
    scope: Scope,
    generation: int,
    current: Message,
    history: tuple[Message, ...],
    conversation_id: str,
    operation_id: str,
    zone: str | None,
    *,
    assistant=False,
    quote="",
) -> dict:
    def line(message):
        return (
            f"[{message.role}; {message.iso_time(zone)}; event={message.event_key}]\n"
            + message.text
        )

    dialogue = assistant or any(m.role == "assistant" for m in history)
    material_format = "dialogue_window_v2" if dialogue else FORMAT
    zone_label = zone if zone is not None else "system"
    content = (
        f"材料格式：{material_format}；时区：{zone_label}\n"
        "历史语境只用于理解指代，只从新增消息抽取新增信息。"
        "user 是用户原文，assistant 是助手陈述；区分用户事实与助手陈述。"
        "助手建议不等于用户接受；助手承诺或声称已完成，不等于操作实际执行或结果发生。\n"
        "[图片]/[表情包]/[语音]/[文件]/[视频] 仅表示原消息附带媒体，切勿凭空臆测媒体内容。\n"
        "【历史语境】\n"
        + ("\n".join(line(m) for m in history) or "无")
        + ("\n【引用背景（仅用于理解指代，勿重复抽取）】\n" + quote[:200] if quote else "")
        + ("\n【新增助手正文】\n" if assistant else "\n【本次新增用户消息】\n")
        + line(current)
    )
    return {
        "async": True,
        "operation_id": operation_id,
        "items": [
            {
                "document_id": (
                    f"aw1-{generation}-{current.event_key}"
                    if assistant
                    else f"uw1-{generation}-{current.event_key}"
                ),
                "content": content,
                "timestamp": current.iso_time(zone),
                "context": (
                    "AstrBot 标准会话历史保存的助手正文；发送状态未知，不证明操作执行。"
                    if assistant
                    else "AstrBot 私聊中收到的用户消息。"
                ),
                "metadata": {
                    "source": "astrbot",
                    "role": current.role,
                    "delivery": "unknown" if assistant else "received",
                    "format": material_format,
                    "scope_id": scope.id,
                    "generation": str(generation),
                    "event_key": current.event_key,
                    "context_event_keys": canonical([m.event_key for m in history]),
                    "conversation_id": conversation_id,
                    "occurred_at": current.iso_time(zone),
                    "time_source": (
                        "history_observed_at" if assistant else TIME_SOURCES[scope.platform_type]
                    ),
                    "timezone": zone_label,
                    "content_sha256": sha256(content),
                },
                "observation_scopes": "shared",
                "update_mode": "replace",
            }
        ],
    }


def request_digest(connection: str, bank: str, generation: int, payload: dict) -> str:
    return sha256(
        canonical(
            {
                "connection": connection,
                "bank": bank,
                "generation": generation,
                "format": FORMAT,
                "payload": payload,
            }
        )
    )


def document_role(document):
    match = re.fullmatch(r"([au])w1-(?:0|[1-9][0-9]*)-[0-9a-f]{64}", document) if isinstance(document, str) else None
    if match:
        return "assistant" if match[1] == "a" else "user"
    return None


def memory_item(row):
    item = {key: row[key] for key in (
        "text", "id", "type", "document_id", "chunk_id", "context", "occurred_start",
        "occurred_end", "mentioned_at", "source_fact_ids", "source_memory_ids", "source_kind",
    ) if key in row}
    metadata = row.get("metadata") or {}
    attribution = {key: metadata[key] for key in (
        "source", "role", "delivery", "occurred_at", "time_source",
    ) if key in metadata}
    if attribution:
        item["metadata"] = attribution
    if item.get("source_kind") == "assistant" or (
        "source_kind" not in item and document_role(row.get("document_id")) == "assistant"
    ):
        item["来源角色"] = "assistant"
        item["发送状态"] = attribution.get("delivery", "unknown")
    return item


def render_memories(items, prompt):
    types = {"world": "世界事实", "experience": "经历", "observation": "观察"}
    sources = {"user": "用户", "assistant": "助手回复", "mixed": "混合来源",
               "external": "早期/外部记录", "unknown": "来源未知"}
    lines = []
    for item in items:
        metadata = item.get("metadata") or {}
        occurred = item.get("occurred_start") or item.get("mentioned_at") or metadata.get("occurred_at") or ""
        date = occurred[:10] if isinstance(occurred, str) and re.match(r"\d{4}-\d{2}-\d{2}", occurred) else ""
        source = item.get("source_kind") or metadata.get("role") or document_role(item.get("document_id")) or "unknown"
        labels = [label for label in (date, types.get(item.get("type")), sources.get(source, "来源未知")) if label]
        lines.append("- [" + "｜".join(labels) + "] " + item["text"].replace("\n", "\n  "))
    if not lines:
        return ""
    return (prompt + "\n\n" if prompt else "") + "\n".join(lines)


def memory_block(results: list[dict], *, limit=4000) -> str:
    items, block = [], ""
    for row in results[:12]:
        if isinstance(row, dict) and isinstance(row.get("text"), str):
            item = memory_item(row)
            candidate = render_memories([*items, item], DEFAULT_MEMORY_PROMPT)
            if len(candidate) <= limit:
                items.append(item)
                block = candidate
    return block


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()
