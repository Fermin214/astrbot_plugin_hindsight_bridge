"""Pure, bounded Chinese presentations of bank-scoped memory records."""

import json
import unicodedata

from .client import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    HindsightError,
    _document_record,
    _identifier,
    _memory_record,
)

_TYPES = {"world": "事实", "experience": "经历", "observation": "整合观察"}
_PAGE_LIMIT = 3500
_DETAIL_LIMIT = 6000


def _text(value, limit):
    text = "".join(
        " " if c.isspace() else c
        for c in str(value)
        if not unicodedata.category(c).startswith("C") or c.isspace()
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _time(record):
    return (
        record.get("occurred_start")
        or record.get("mentioned_at")
        or record.get("date")
        or record.get("created_at")
        or "未知"
    )


def render_page(page, offset=0) -> str:
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("items"), list)
        or type(page.get("total")) is not int
        or page["total"] < 0
        or type(offset) is not int
        or offset < 0
    ):
        raise HindsightError("invalid_catalog_page")
    rows = page["items"]
    lines = [f"本人记忆：共 {page['total']} 条，起始位置 {offset + 1}"]
    if not rows:
        lines.append("此页没有记忆。")
    shown = 0
    for row in rows:
        if not isinstance(row, dict):
            raise HindsightError("invalid_catalog_page")
        prefix = (
            f"\n{offset + shown + 1}. 记忆ID：{_text(row.get('id', ''), 80)}"
            f"\n类型：{_TYPES.get(row.get('type') or row.get('fact_type'), '未知')}"
            f"；时间：{_text(_time(row), 32)}"
            f"\n来源：{_text(row.get('document_id') or '整合来源见详情', 72)}"
        )
        preview_limit = max(
            12, min(240, (_PAGE_LIMIT - 240) // max(1, len(rows)) - len(prefix) - 1)
        )
        block = prefix + "\n" + _text(row.get("text", ""), preview_limit)
        if len("\n".join(lines)) + len(block) > _PAGE_LIMIT - 120:
            break
        lines.append(block)
        shown += 1
    if shown < len(rows):
        lines.append(
            f"\n文本长度受限，此页显示 {shown}/{len(rows)} 条；下一起始位置：{offset + shown}。"
        )
    elif offset + shown < page["total"]:
        lines.append(f"\n下一起始位置：{offset + shown}。")
    return "\n".join(lines)


def render_memory(record) -> str:
    if not isinstance(record, dict):
        raise HindsightError("invalid_catalog_memory")
    sources = record.get("source_memory_ids", [])
    if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
        raise HindsightError("invalid_catalog_memory")
    lines = [
        f"记忆ID：{_text(record.get('id', ''), 200)}",
        f"类型：{_TYPES.get(record.get('type') or record.get('fact_type'), '未知')}",
        f"时间：{_text(_time(record), 100)}",
        f"结束时间：{_text(record.get('occurred_end') or '未知', 100)}",
        f"提及时间：{_text(record.get('mentioned_at') or '未知', 100)}",
        f"来源文档：{_text(record.get('document_id') or '无直接来源文档', 240)}",
        f"来源记忆：{_text('、'.join(sources) or '无', 700)}",
        f"内容：{_text(record.get('text', ''), 3000)}",
        f"语境：{_text(record.get('context') or '无', 900)}",
    ]
    return "\n".join(lines)[:_DETAIL_LIMIT]


def export_bytes(payload) -> bytes:
    """Serialize a complete export, retaining only documented data fields."""
    code = "invalid_export_payload"
    if (
        not isinstance(payload, dict)
        or payload.get("format") != EXPORT_FORMAT
        or type(payload.get("version")) is not int
        or payload["version"] != EXPORT_VERSION
        or not isinstance(payload.get("exported_at"), str)
        or type(payload.get("total")) is not int
        or payload["total"] < 0
        or not isinstance(payload.get("memories"), list)
        or not isinstance(payload.get("documents"), list)
        or len(payload["memories"]) != payload["total"]
    ):
        raise HindsightError(code)
    bank = _identifier(payload.get("bank_id"))
    memories = [_memory_record(row, bank, detail=True) for row in payload["memories"]]
    documents = []
    for row in payload["documents"]:
        if not isinstance(row, dict):
            raise HindsightError(code)
        documents.append(
            _document_record(row, bank, _identifier(row.get("id"), document=True))
        )
    ids = {row["id"] for row in memories}
    document_ids = {row["id"] for row in documents}
    if (
        len(ids) != len(memories)
        or len(document_ids) != len(documents)
        or document_ids
        != {row["document_id"] for row in memories if row["document_id"]}
        or any(
            source not in ids for row in memories for source in row["source_memory_ids"]
        )
    ):
        raise HindsightError(code)
    clean = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "bank_id": bank,
        "exported_at": payload["exported_at"],
        "total": payload["total"],
        "memories": memories,
        "documents": documents,
    }
    try:
        return json.dumps(clean, ensure_ascii=False, allow_nan=False, indent=2).encode(
            "utf-8"
        )
    except (ValueError, TypeError, UnicodeError):
        raise HindsightError(code) from None
