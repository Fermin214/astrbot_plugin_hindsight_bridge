"""Bounded native read projections and version-bound, whole-line page excerpts."""

import math
import re
from urllib.parse import quote, urlencode

from .client import FACT_TYPES, HindsightError, native_page_id
from .identity import canonical, sha256
from .materials import token_count


def fits(value, *, tokens, chars, bytes_limit, tokenizer):
    text = canonical(value)
    return (
        len(text) <= chars and len(text.encode("utf-8")) <= bytes_limit
        and token_count(text, tokenizer) <= tokens
    )


def query_text(query, *, tokenizer, budget=380):
    if (
        not isinstance(query, str) or not query.strip() or len(query) > 4000
        or token_count(query, tokenizer) > min(400, budget)
    ):
        raise HindsightError("invalid_query_budget", permanent=True)
    return query.strip()


async def search_pages(client, bank, query):
    value = await client._background_read(bank, "/knowledge-base/search?" + urlencode({
        "q": query, "limit": 5,
    }))
    rows = value.get("results")
    if (
        not isinstance(rows, list) or len(rows) > 5 or type(value.get("total")) is not int
        or value["total"] < len(rows)
    ):
        raise HindsightError("invalid_background_response")
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise HindsightError("invalid_background_response")
        identity = native_page_id(row.get("id"))
        score = row.get("score")
        if (
            identity in seen or not isinstance(row.get("name"), str) or len(row["name"]) > 1024
            or not isinstance(row.get("snippet"), str)
            or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
            or (row.get("updated_at") is not None and not isinstance(row["updated_at"], str))
        ):
            raise HindsightError("invalid_background_response")
        if row.get("mental_model_id") is not None:
            native_page_id(row["mental_model_id"])
        seen.add(identity)
        clean.append({key: row[key] for key in ("id", "name", "snippet", "score")} | {
            "updated_at": row.get("updated_at"),
        })
    return clean


async def mapped_page(client, bank, page_id, recheck):
    page_id = native_page_id(page_id)
    tree = await client.knowledge_pages(bank)
    await recheck()
    selected = next((item for item in tree if item["page_id"] == page_id), None)
    if selected is None:
        raise HindsightError("background_missing")
    page = await client.knowledge_page(bank, page_id)
    await recheck()
    model = await client.background_page(bank, selected["mental_model_id"])
    await recheck()
    if page["body"] != model["content"]:
        raise HindsightError("background_changed")
    return {
        **page, "mental_model_id": model["id"], "path": selected["path"],
        "last_refreshed_at": model["last_refreshed_at"],
        "last_refresh_failed_at": model["last_refresh_failed_at"],
        "version": sha256(canonical([page_id, model["id"], page["body"], model["last_refreshed_at"]])),
    }


def page_excerpt(page, *, start_line=1, version="", **limits):
    if type(start_line) is not int or start_line < 1 or not isinstance(version, str):
        raise HindsightError("invalid_read_position", permanent=True)
    if (start_line > 1 and not version) or (version and not re.fullmatch(r"[0-9a-f]{64}", version)):
        raise HindsightError("invalid_read_version", permanent=True)
    if version and version != page["version"]:
        raise HindsightError("read_version_changed")
    if not page["body"].strip():
        raise HindsightError("background_empty")
    lines = page["body"].splitlines(keepends=True)
    if start_line > len(lines):
        raise HindsightError("invalid_read_position", permanent=True)
    result = {
        "page_id": page["id"], "name": page["name"], "version": page["version"],
        "last_refreshed_at": page["last_refreshed_at"], "freshness": "unknown",
        "start_line": start_line, "end_line": start_line - 1, "total_lines": len(lines),
        "next_line": start_line, "content": "",
    }
    for number in range(start_line, len(lines) + 1):
        candidate = {**result, "content": result["content"] + lines[number - 1],
                     "end_line": number, "next_line": number + 1 if number < len(lines) else None}
        if not fits({"kind": "page", "external_data": True, **candidate}, **limits):
            break
        result = candidate
    if result["end_line"] < start_line:
        raise HindsightError("read_line_too_large")
    return result


def recall_records(rows, bank):
    """Project native attribution without flattening sources into new evidence."""
    clean = []
    for row in rows:
        if (
            not isinstance(row, dict) or row.get("bank_id", bank) != bank
            or not isinstance(row.get("id"), str) or not row["id"]
            or not isinstance(row.get("text"), str)
            or row.get("type", row.get("fact_type")) not in FACT_TYPES
        ):
            raise HindsightError("invalid_recall_response")
        record = {"id": row["id"], "text": row["text"], "type": row.get("type", row.get("fact_type"))}
        for key in ("document_id", "chunk_id", "context", "occurred_start", "occurred_end", "mentioned_at"):
            if row.get(key) is not None:
                if not isinstance(row[key], str):
                    raise HindsightError("invalid_recall_response")
                record[key] = row[key]
        for key in ("source_fact_ids", "source_memory_ids"):
            sources = row.get(key) or []
            if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
                raise HindsightError("invalid_recall_response")
            if sources:
                record[key] = sources
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise HindsightError("invalid_recall_response")
        record["metadata"] = {}
        for key in ("source", "role", "delivery", "occurred_at", "time_source"):
            if key in metadata:
                if not isinstance(metadata[key], str):
                    raise HindsightError("invalid_recall_response")
                record["metadata"][key] = metadata[key]
        clean.append(record)
    return clean


def native_sources(response):
    if response is None:
        return {"available": False, "memories": [], "mental_models": [], "directives": []}
    if not isinstance(response, dict):
        raise HindsightError("invalid_background_response")
    if response.get("based_on") is None:
        return {"available": False, "memories": [], "mental_models": [], "directives": []}
    if not isinstance(response["based_on"], dict):
        raise HindsightError("invalid_background_response")
    based = response["based_on"]
    memories = based.get("memories")
    if memories is None:
        memories = []
        for key in ("world", "experience", "observation"):
            values = based.get(key, [])
            if not isinstance(values, list):
                raise HindsightError("invalid_background_response")
            memories.extend(values)
    if not isinstance(memories, list):
        raise HindsightError("invalid_background_response")
    clean = []
    for fact in memories:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str) or not fact["id"]:
            raise HindsightError("invalid_background_response")
        item = {"id": fact["id"]}
        for key in ("document_id", "chunk_id", "type", "fact_type", "occurred_start", "occurred_end", "mentioned_at"):
            if fact.get(key) is not None:
                if not isinstance(fact[key], str):
                    raise HindsightError("invalid_background_response")
                item[key] = fact[key]
        clean.append(item)
    result = {"available": True, "memories": clean}
    for key in ("mental_models", "directives"):
        values = based.get(key, based.get("mental-models", []) if key == "mental_models" else [])
        if not isinstance(values, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
            for item in values
        ):
            raise HindsightError("invalid_background_response")
        result[key] = [{"id": item["id"]} for item in values]
    return result


async def page_sources(client, bank, page):
    value = await client._background_read(
        bank, "/mental-models/" + quote(page["mental_model_id"], safe="") + "?detail=full",
    )
    if (
        value.get("id") != page["mental_model_id"] or value.get("bank_id") != bank
        or value.get("is_knowledge_page") is not True
        or (value.get("content") or "") != page["body"]
        or value.get("last_refreshed_at") != page["last_refreshed_at"]
    ):
        raise HindsightError("background_changed")
    return native_sources(value.get("reflect_response"))


async def page_history(client, bank, page):
    try:
        rows = await client._background_read(
            bank, "/mental-models/" + quote(page["mental_model_id"], safe="") + "/history", expected=list,
        )
    except HindsightError as exc:
        if exc.code == "background_response_too_large":
            raise HindsightError("history_response_too_large") from None
        raise
    clean = []
    for row in rows:
        if (
            not isinstance(row, dict) or not isinstance(row.get("changed_at"), str)
            or row.get("kind") not in (None, "refresh_failed")
            or (row.get("previous_content") is not None and not isinstance(row["previous_content"], str))
        ):
            raise HindsightError("invalid_background_response")
    for index, row in enumerate(rows[:5], 1):
        failed = row.get("kind") == "refresh_failed"
        item = {"position": index, "kind": "refresh_failed" if failed else "version",
                "changed_at": row["changed_at"]}
        if not failed:
            item["content"] = row.get("previous_content")
        else:
            item["has_new_version"] = False
        clean.append(item)
    return {"items": clean, "shown": len(clean), "retained_count": len(rows), "complete_history": False}
