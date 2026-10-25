"""Read final assistant text from bounded, persisted conversation history."""

import json

from .identity import sha256


def history_messages(conversation):
    raw = getattr(conversation, "history", None)
    if not isinstance(raw, str) or len(raw) > 2 * 1024 * 1024:
        return None
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return rows if isinstance(rows, list) else None


def history_digest(rows):
    return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True))


def history_boundary(rows):
    if rows is None:
        return ""
    return json.dumps({"length": len(rows), "digest": history_digest(rows)})


def appended_history(rows, boundary):
    if rows is None or not boundary:
        return None
    try:
        before = json.loads(boundary)
        length = before["length"]
        if not isinstance(length, int) or length < 0 or len(rows) <= length:
            return None
        if history_digest(rows[:length]) != before["digest"]:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return rows[length:]


def persistent_messages(messages):
    """Apply AstrBot's temporary-content filtering before private flags disappear."""
    rows = []
    for message in messages:
        if getattr(message, "_no_save", False):
            continue
        row = message.model_dump() if hasattr(message, "model_dump") else message
        if not isinstance(row, dict) or row.get("_no_save"):
            continue
        content = getattr(message, "content", row.get("content"))
        if isinstance(content, list):
            row = dict(row)
            row["content"] = [
                part.model_dump() if hasattr(part, "model_dump") else part
                for part in content
                if not getattr(part, "_no_save", False)
                and not (isinstance(part, dict) and part.get("_no_save"))
            ]
        rows.append(row)
    return rows


def plain_text(row):
    if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
        return None
    if row.get("tool_calls") or row.get("function_call"):
        return None
    if row.get("_no_save") or row.get("type") == "checkpoint":
        return None
    content = row.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        if any(
            not isinstance(part, dict)
            or part.get("type") not in {"text", "think"}
            or part.get("_no_save")
            or (part.get("type") == "text" and not isinstance(part.get("text"), str))
            or (row["role"] == "user" and part.get("type") != "text")
            for part in content
        ):
            return None
        text = "".join(part["text"] for part in content if part["type"] == "text")
    else:
        return None
    if not text.strip() or len(text) > 8000 or "<think>" in text:
        return None
    return text


def final_reply(rows, user_text):
    """Only the last persisted user turn can establish an unambiguous anchor."""
    rows = [
        row
        for row in rows
        if not (isinstance(row, dict) and row.get("role") == "_checkpoint")
    ]
    users = [
        index
        for index, row in enumerate(rows)
        if isinstance(row, dict) and row.get("role") == "user"
    ]
    if not users or plain_text(rows[users[-1]]) != user_text:
        return None
    tail = rows[users[-1] + 1 :]
    if not tail or not isinstance(tail[-1], dict):
        return None
    return plain_text(tail[-1]) if tail[-1].get("role") == "assistant" else None
