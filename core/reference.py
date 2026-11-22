import json
import os
import sqlite3
import time
import uuid

from .client import RECALL_BUDGETS
from .control import QueueError
from .identity import canonical, sha256
from .scope_settings import REFERENCE, scope_key
from .materials import DEFAULT_MEMORY_PROMPT, token_count


PROMPT_TOKEN_LIMIT = 256
DETAIL_LIMIT_MAX = 100
HISTORY_LIMIT_MAX = 300
REFERENCE_TYPES = ("world", "experience", "observation")
UNSET = object()


def _options_key(connection, owner):
    return scope_key(REFERENCE, connection, owner)


def _binding(control_id, connection, owner, source, bank, generation, authorization_revision):
    return sha256(canonical([control_id, connection, owner, source, bank, generation, authorization_revision]))


def _scope_binding(queue, scope):
    grant = queue.access.authorization(scope["platform_instance_id"], scope["user_id"])
    owner = queue.control.owner(queue.connection_id, scope["owner_id"])
    if (
        not grant or not grant["enabled"] or grant["source_id"] != scope["scope_id"]
        or not owner or owner["bank"] != scope["bank"] or owner["generation"] != scope["generation"]
    ):
        return None
    return _binding(
        queue.control.setting("control_id"), queue.connection_id, scope["owner_id"],
        scope["scope_id"], scope["bank"], scope["generation"], grant["revision"],
    )


def _stored(value):
    try:
        options = json.loads(value)
        if not isinstance(options, dict):
            raise ValueError
        return options
    except (ValueError, TypeError, RecursionError):
        raise QueueError("reference_settings_invalid") from None


def _stored_revision(options):
    revision = options.get("revision")
    if type(revision) is not int or revision < 1:
        raise QueueError("reference_settings_invalid")
    return revision


def _valid_history_limit(limit):
    return type(limit) is int and 0 <= limit <= HISTORY_LIMIT_MAX


def _validate(prompt, types, exclude_assistant, detail_limit, history_limit, recall_budget, capture_assistant,
              recall_on_quoted_or_link):
    if (
        not isinstance(prompt, str) or not isinstance(types, list) or len(types) > len(REFERENCE_TYPES)
        or any(kind not in REFERENCE_TYPES for kind in types) or len(set(types)) != len(types)
        or type(exclude_assistant) is not bool or type(detail_limit) is not int
        or not 0 <= detail_limit <= DETAIL_LIMIT_MAX or not _valid_history_limit(history_limit)
        or recall_budget not in RECALL_BUDGETS or type(capture_assistant) is not bool
        or type(recall_on_quoted_or_link) is not bool
    ):
        raise QueueError("reference_settings_invalid")


def _options(stored):
    result = {
        "prompt": stored.get("prompt", DEFAULT_MEMORY_PROMPT), "types": stored.get("types", list(REFERENCE_TYPES)),
        "exclude_assistant": stored.get("exclude_assistant", False), "detail_limit": stored.get("detail_limit", 12),
        "history_limit": stored.get("history_limit", 30),
        "recall_budget": stored.get("recall_budget", "mid"),
        "capture_assistant": stored.get("capture_assistant", True),
        "recall_on_quoted_or_link": stored.get("recall_on_quoted_or_link", False),
    }
    _validate(**result)
    return {"revision": stored["revision"], **result}


def reference_options(queue, scope):
    value = queue.control.setting(_options_key(queue.connection_id, scope["owner_id"]))
    if not value:
        return _options({"revision": 0})
    stored = _stored(value)
    revision = _stored_revision(stored)
    if not stored.get("binding") or stored["binding"] != _scope_binding(queue, scope):
        return _options({"revision": revision})
    return _options(stored)


def save_reference_options(queue, scope, *, revision, prompt, types, exclude_assistant,
                           detail_limit, history_limit, recall_budget=UNSET, capture_assistant=UNSET,
                           recall_on_quoted_or_link=UNSET):
    control = queue.control
    with control.transaction():
        binding = _scope_binding(queue, scope)
        if not binding or not queue.can_recall(scope, manual=True):
            raise QueueError("scope_not_available")
        previous = reference_options(queue, scope)
        if type(revision) is not int or revision != previous["revision"]:
            raise QueueError("reference_changed")
        if recall_budget is UNSET:
            recall_budget = previous["recall_budget"]
        if capture_assistant is UNSET:
            capture_assistant = previous["capture_assistant"]
        if recall_on_quoted_or_link is UNSET:
            recall_on_quoted_or_link = previous["recall_on_quoted_or_link"]
        _validate(prompt, types, exclude_assistant, detail_limit, history_limit, recall_budget, capture_assistant,
                  recall_on_quoted_or_link)
        if token_count(prompt) > PROMPT_TOKEN_LIMIT:
            raise QueueError("reference_prompt_too_long")
        options = {
            "revision": revision + 1, "prompt": prompt, "types": types, "exclude_assistant": exclude_assistant,
            "detail_limit": detail_limit, "history_limit": history_limit,
            "recall_budget": recall_budget, "capture_assistant": capture_assistant,
            "recall_on_quoted_or_link": recall_on_quoted_or_link,
        }
        control.db.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_options_key(queue.connection_id, scope["owner_id"]), canonical({**options, "binding": binding})),
        )
        if history_limit < previous["history_limit"]:
            queue.references.trim(binding, history_limit)
        control.bump()
    return options


class ReferenceHistory:
    def __init__(self, queue, warning):
        self.queue = queue
        self.path = queue.directory / "references.sqlite3"
        self.db = None
        self.warning = warning
        self.revision = None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def _open(self):
        if self.db is None:
            if any(self.path.with_name(self.path.name + suffix).is_symlink()
                   for suffix in ("", "-journal", "-wal", "-shm")):
                raise OSError("reference_path_invalid")
            self.path.touch(mode=0o600, exist_ok=True)
            database = sqlite3.connect(self.path, timeout=0.2)
            try:
                database.row_factory = sqlite3.Row
                database.execute("PRAGMA foreign_keys=ON")
                database.execute("PRAGMA secure_delete=ON")
                page_size = database.execute("PRAGMA page_size").fetchone()[0]
                database.execute(f"PRAGMA max_page_count={64 * 1024 * 1024 // page_size}")
                database.executescript("""
                    CREATE TABLE IF NOT EXISTS turns(
                        id TEXT PRIMARY KEY, binding TEXT NOT NULL, created_at REAL NOT NULL,
                        conversation_id TEXT NOT NULL, message_id TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS reference_time ON turns(binding,created_at DESC);
                    CREATE TABLE IF NOT EXISTS deliveries(
                        turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE, payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS reference_turn ON deliveries(turn_id);
                """)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
            except BaseException:
                database.close()
                raise
            self.db = database
        return self.db

    def reconcile(self, *, force=False):
        control = self.queue.control
        revision = control.setting("revision")
        if not force and revision == self.revision:
            return True
        control_id = control.setting("control_id")
        bindings = {
            _binding(control_id, *row): _options_key(row["connection_id"], row["owner_id"])
            for row in control.db.execute(
                "SELECT a.connection_id,a.owner_id,a.source_id,o.bank,o.generation,g.revision "
                "FROM accounts a JOIN owners o USING(connection_id,owner_id) "
                "JOIN authorizations g ON g.connection_id=a.connection_id AND g.source_id=a.source_id "
                "AND g.platform_instance_id=a.platform_instance_id AND g.user_id=a.user_id "
                "WHERE g.enabled=1 AND NOT EXISTS(SELECT 1 FROM erasures e "
                "WHERE e.connection_id=o.connection_id AND e.old_bank=o.bank)"
            )
        }
        limits = {}
        with control.transaction():
            for key, value in control.db.execute(
                "SELECT key,value FROM settings WHERE key GLOB ?", (REFERENCE + "*",)
            ).fetchall():
                previous_revision = int(revision)
                try:
                    options = _stored(value)
                    binding = options.get("binding")
                    if isinstance(binding, str) and bindings.get(binding) == key:
                        limit = options.get("history_limit", 30)
                        limits[binding] = limit if _valid_history_limit(limit) else None
                        try:
                            _stored_revision(options)
                            _options(options)
                        except QueueError:
                            self.warning("reference_settings_invalid")
                        continue
                    previous_revision = _stored_revision(options)
                    if options.keys() == {"revision"}:
                        continue
                except QueueError:
                    self.warning("reference_settings_invalid")
                control.db.execute("UPDATE settings SET value=? WHERE key=?", (
                    canonical({"revision": previous_revision + 1}), key,
                ))
                control.bump()
        try:
            if self.path.exists():
                database = self._open()
                with database:
                    for binding, count in database.execute("SELECT binding,COUNT(*) FROM turns GROUP BY binding").fetchall():
                        limit = limits.get(binding, 30) if binding in bindings else 0
                        if limit is not None and count > limit:
                            self._trim(database, binding, limit)
        except (sqlite3.Error, OSError):
            self.close()
            self.revision = None
            self.warning("reference_history_unavailable")
            return False
        self.revision = control.setting("revision")
        return True

    def _trim(self, database, binding, limit):
        database.execute(
            "DELETE FROM turns WHERE id IN (SELECT id FROM turns WHERE binding=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)", (binding, limit),
        )

    def trim(self, binding, limit):
        if self.path.exists():
            try:
                with self._open() as database:
                    self._trim(database, binding, limit)
            except (sqlite3.Error, OSError):
                self.warning("reference_history_unavailable")
                raise QueueError("reference_history_unavailable") from None

    def start(self, scope, conversation_id, message_id, delivery):
        try:
            if not self.queue.can_recall(scope) or not (binding := _scope_binding(self.queue, scope)):
                return None
            limit = reference_options(self.queue, scope)["history_limit"]
            if not limit:
                return None
            turn_id = uuid.uuid4().hex
            with self._open() as database:
                self._trim(database, binding, limit - 1)
                database.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (
                    turn_id, binding, time.time(), conversation_id, message_id,
                ))
                database.execute("INSERT INTO deliveries VALUES (?,?)", (turn_id, canonical(delivery)))
            return turn_id
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.warning("reference_history_unavailable")
            return None

    def append(self, scope, turn_id, delivery):
        try:
            if not turn_id or not self.queue.can_recall(scope) or not (binding := _scope_binding(self.queue, scope)):
                return
            with self._open() as database:
                database.execute(
                    "INSERT INTO deliveries SELECT id,? FROM turns WHERE id=? AND binding=?",
                    (canonical(delivery), turn_id, binding),
                )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.warning("reference_history_unavailable")

    def _read_binding(self, scope):
        binding = _scope_binding(self.queue, scope)
        if not binding or not self.queue.can_recall(scope, manual=True):
            raise QueueError("scope_not_available")
        if not self.reconcile():
            raise QueueError("reference_history_unavailable")
        reference_options(self.queue, scope)
        return binding

    def list(self, scope):
        binding = self._read_binding(scope)
        if not self.path.exists():
            return []
        try:
            database = self._open()
            turns = []
            for row in database.execute(
                "SELECT id,conversation_id,message_id,created_at FROM turns WHERE binding=? "
                "ORDER BY created_at DESC,rowid DESC", (binding,),
            ):
                deliveries = [json.loads(item["payload"]) for item in database.execute(
                    "SELECT payload FROM deliveries WHERE turn_id=? ORDER BY rowid", (row["id"],),
                )]
                turns.append({**dict(row), "summary": {
                    "pages": sum(len(item.get("pages", [])) for item in deliveries),
                    "memories": sum(len(item.get("items", [])) for item in deliveries),
                    "tools": sum(item.get("kind") != "request" for item in deliveries),
                    "status": "error" if any(item.get("status") == "error" for item in deliveries)
                    else deliveries[-1].get("status", "") if deliveries else "",
                }})
            return turns
        except (sqlite3.Error, OSError):
            raise QueueError("reference_history_unavailable") from None

    def detail(self, scope, turn_id):
        binding = self._read_binding(scope)
        try:
            if self.path.exists():
                database = self._open()
                turn = database.execute(
                    "SELECT id,conversation_id,message_id,created_at FROM turns WHERE id=? AND binding=?",
                    (turn_id, binding),
                ).fetchone()
                if turn:
                    return {**dict(turn), "deliveries": [
                        json.loads(row["payload"]) for row in database.execute(
                            "SELECT payload FROM deliveries WHERE turn_id=? ORDER BY rowid", (turn_id,),
                        )
                    ]}
        except (sqlite3.Error, OSError, ValueError):
            raise QueueError("reference_history_unavailable") from None
        raise QueueError("reference_turn_missing")
