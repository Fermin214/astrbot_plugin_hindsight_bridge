import json

from .identity import canonical, sha256


BACKGROUND = "background:"
READING = "reading:"
REFLECT = "reflect:"
REFERENCE = "reference:"
PREFIXES = (BACKGROUND, READING, REFLECT, REFERENCE)


def scope_key(prefix, connection, owner):
    return prefix + sha256(canonical([connection, owner]))


def forget_settings(control, connection, owner):
    from .control import QueueError

    for prefix in PREFIXES:
        key = scope_key(prefix, connection, owner)
        raw = control.setting(key)
        if not raw:
            continue
        try:
            previous = json.loads(raw)
            revision = previous["revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError
        except (ValueError, KeyError, TypeError, RecursionError):
            if prefix != REFERENCE:
                raise QueueError(f"{prefix[:-1]}_settings_invalid") from None
            revision = int(control.setting("revision"))
        value = {"revision": revision + 1}
        if prefix == BACKGROUND:
            value.update(format=2, enabled=False, page_id="", mental_model_id="", pages=[],
                         invalidated=bool(previous.get("page_id") or previous.get("pages") or previous.get("invalidated")))
        elif prefix == READING:
            value.update(format=1, mode="automatic", invalidated=True)
        elif prefix == REFLECT:
            value.update(format=1, enabled=False, timeout=12, invalidated=True)
        control.db.execute("UPDATE settings SET value=? WHERE key=?", (canonical(value), key))
