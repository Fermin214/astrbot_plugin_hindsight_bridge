"""Atomic capture, immutable requests, and one consumer per durable SQLite queue."""

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .control import Control, QueueError
from .identity import Incoming, Scope, canonical, sha256
from .locking import exclusive_lock
from .materials import Message, request_digest, retain_payload

SCHEMA_VERSION = 5
ACTIVE_STATES = ("ready", "submitting", "unknown", "tracking", "cancel_requested")
STATES = (
    *ACTIVE_STATES,
    "done",
    "failed",
    "cancelled",
    "discarded",
    "reconcile_required",
)


@dataclass(frozen=True)
class Capture:
    task_id: int
    scope_id: str
    bank: str
    generation: int
    history: tuple[Message, ...]
    created: bool


class Outbox:
    def __init__(
        self,
        directory: Path,
        service_id: str,
        url: str,
        *,
        credential_fingerprint: str,
        address_revision: int = 1,
        zone: str | None = None,
        max_tasks: int = 10000,
        max_bytes: int = 64 * 1024 * 1024,
        control_directory: Path | None = None,
        agent_id: str = "default",
    ):
        self.db = self.lock = self.control = None
        self.directory = Path(directory)
        self.connection_id, self.zone = service_id.strip(), zone
        self.agent_id = agent_id.strip()
        self.permission_pairs = None
        self.recovering = False
        self.in_flight = set()
        self.max_tasks, self.max_bytes = max_tasks, max_bytes
        if zone is not None:
            ZoneInfo(zone)
        if (
            not self.connection_id
            or not self.agent_id
            or address_revision < 1
            or max_tasks < 1
            or max_bytes < 1
        ):
            raise QueueError("invalid_queue_configuration")
        self.directory.mkdir(parents=True, exist_ok=True)
        control_directory = Path(
            control_directory
            or self.directory.with_name(self.directory.name + "-control")
        ).resolve()
        runtime_directory = self.directory.resolve()
        if control_directory.is_relative_to(
            runtime_directory
        ) or runtime_directory.is_relative_to(control_directory):
            raise QueueError("control_directory_must_be_separate")
        try:
            self.lock = (self.directory / "worker.lock").open("a+b")
            exclusive_lock(self.lock)
            legacy = self.directory / "outbox.sqlite3"
            if legacy.exists():
                with sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True) as old:
                    if old.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]:
                        raise QueueError("legacy_queue_requires_migration")
            self.db = sqlite3.connect(self.directory / "runtime.sqlite3", timeout=2)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):
                raise QueueError("unsupported_database_version")
            self._schema()
            deployment = self.setting("deployment_id")
            self.control = Control(control_directory, deployment, required=version >= 2)
            control_id = self.control.setting("control_id")
            if self.setting("control_id") not in ("", control_id):
                raise QueueError("recovery_required:control_mismatch")
            previous_revision = int(self.setting("control_revision") or 0)
            if previous_revision > int(self.control.setting("revision")):
                raise QueueError("recovery_required:control_rolled_back")
            self.recovering = version >= 2 and previous_revision != int(
                self.control.setting("revision")
            )
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES ('deployment_id',?)",
                    (self.control.setting("deployment_id"),),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES ('accept_from',?)",
                    (str(int(time.time())),),
                )
                self._connection(url, credential_fingerprint, address_revision)
                # An interrupted submit may have reached the server. Never assign a new ID.
                self.db.execute(
                    "UPDATE outbox SET state='unknown' WHERE connection_id=? "
                    "AND state='submitting'",
                    (self.connection_id,),
                )
                self.db.execute(
                    "UPDATE outbox SET released=1 WHERE connection_id=?",
                    (self.connection_id,),
                )
            self.deployment_id = self.setting("deployment_id")
            self._migrate_ownership(version)
            self.apply_control()
            with self.db:
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('control_id',?)",
                    (control_id,),
                )
            if os.name != "nt":
                os.chmod(self.directory, 0o700)
                os.chmod(self.directory / "runtime.sqlite3", 0o600)
        except BaseException:
            self.close()
            raise

    def _schema(self):
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corrections'"
        ).fetchone():
            if self.db.execute("SELECT 1 FROM corrections LIMIT 1").fetchone():
                raise QueueError("legacy_corrections_require_handover")
            self.db.execute("DROP TABLE corrections")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, address_revision INTEGER NOT NULL,
                credential_fingerprint TEXT NOT NULL, credential_revision INTEGER NOT NULL,
                paused_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS scopes (
                connection_id TEXT NOT NULL REFERENCES connections(id),
                scope_id TEXT NOT NULL, identity_json TEXT NOT NULL,
                platform_instance_id TEXT NOT NULL, user_id TEXT NOT NULL,
                bank TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                allowed INTEGER NOT NULL DEFAULT 1, mode TEXT NOT NULL DEFAULT 'active',
                accept_from INTEGER NOT NULL,
                PRIMARY KEY(connection_id,scope_id)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                bank TEXT NOT NULL, generation INTEGER NOT NULL,
                document_id TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                accepted INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0,
                retry_requested INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, completed_at REAL,
                FOREIGN KEY(connection_id,scope_id) REFERENCES scopes(connection_id,scope_id),
                UNIQUE(connection_id,scope_id,generation,document_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                connection_id TEXT NOT NULL, scope_id TEXT NOT NULL, event_key TEXT NOT NULL,
                text_sha256 TEXT NOT NULL, task_id INTEGER NOT NULL REFERENCES outbox(id),
                occurred_at INTEGER NOT NULL, received_at REAL NOT NULL,
                conflict_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(connection_id,scope_id,event_key)
            );
            CREATE TABLE IF NOT EXISTS window (
                task_id INTEGER PRIMARY KEY REFERENCES outbox(id),
                connection_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                generation INTEGER NOT NULL, conversation_id TEXT NOT NULL,
                event_key TEXT NOT NULL, text TEXT NOT NULL,
                occurred_at INTEGER NOT NULL, received_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                task_id INTEGER NOT NULL REFERENCES outbox(id),
                operation_id TEXT NOT NULL, parent_id TEXT, status TEXT NOT NULL,
                PRIMARY KEY(task_id,operation_id)
            );
            CREATE TABLE IF NOT EXISTS replies (
                user_task_id INTEGER PRIMARY KEY REFERENCES window(task_id) ON DELETE CASCADE,
                task_id INTEGER NOT NULL REFERENCES outbox(id),
                text TEXT NOT NULL, observed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_baselines (
                task_id INTEGER PRIMARY KEY REFERENCES window(task_id) ON DELETE CASCADE,
                digest TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(connection_id,next_at);
            CREATE INDEX IF NOT EXISTS outbox_scope ON outbox(connection_id,scope_id,id);
            CREATE INDEX IF NOT EXISTS window_scope ON
                window(connection_id,scope_id,generation,conversation_id,occurred_at);
        """)
        for table, column, declaration in (
            ("scopes", "owner_id", "TEXT NOT NULL DEFAULT ''"),
            ("scopes", "principal_id", "TEXT NOT NULL DEFAULT ''"),
            ("scopes", "revision", "INTEGER NOT NULL DEFAULT 0"),
            ("outbox", "cancel_intent", "INTEGER NOT NULL DEFAULT 0"),
        ):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )
        self.db.commit()

    def _migrate_ownership(self, version):
        for row in self.db.execute("SELECT * FROM scopes").fetchall():
            source = Scope(*json.loads(row["identity_json"]))
            if (
                source.deployment_id != self.deployment_id
                or source.id != row["scope_id"]
            ):
                raise QueueError("recovery_required:source_identity_mismatch")
            if version >= 2 and not self.control.account(
                row["connection_id"], source.id
            ):
                raise QueueError("recovery_required:account_mapping_missing")
            self.control.ensure_account(
                row["connection_id"],
                source,
                row["accept_from"],
                agent_id=self.agent_id,
                legacy=row,
            )
            for task in self.db.execute(
                "SELECT * FROM outbox WHERE connection_id=? AND scope_id=? AND accepted=1",
                (row["connection_id"], row["scope_id"]),
            ).fetchall():
                account = self.control.account(row["connection_id"], source.id)
                self.control.record_request(task, account["owner_id"], accepted=True)

    def apply_control(self):
        """Replay the authoritative owner generation before opening message I/O."""
        with self.db:
            for row in self.db.execute("SELECT * FROM scopes").fetchall():
                account = self.control.account(row["connection_id"], row["scope_id"])
                if not account:
                    raise QueueError("recovery_required:account_mapping_missing")
                changed = (
                    row["revision"] != account["revision"]
                    or row["owner_id"] != account["owner_id"]
                    or row["bank"] != account["bank"]
                )
                self.db.execute(
                    "UPDATE scopes SET owner_id=?,principal_id=?,bank=?,generation=?,"
                    "mode=?,accept_from=?,revision=? WHERE connection_id=? AND scope_id=?",
                    (
                        account["owner_id"],
                        account["principal_id"],
                        account["bank"],
                        account["generation"],
                        account["mode"],
                        account["accept_from"],
                        account["revision"],
                        row["connection_id"],
                        row["scope_id"],
                    ),
                )
                stale = self.db.execute(
                    "SELECT * FROM outbox WHERE connection_id=? AND scope_id=? "
                    "AND (bank<>? OR generation<>?)",
                    (
                        row["connection_id"],
                        row["scope_id"],
                        account["bank"],
                        account["generation"],
                    ),
                ).fetchall()
                for task in stale:
                    erasure = self.control.db.execute(
                        "SELECT forget_id FROM erasures WHERE connection_id=? AND old_bank=?",
                        (task["connection_id"], task["bank"]),
                    ).fetchone()
                    if not erasure:
                        raise QueueError("recovery_required:unrecorded_retired_bank")
                    refs = [
                        ("operation", task["operation_id"]),
                        ("document", task["document_id"]),
                    ]
                    refs.extend(
                        ("operation", oid) for oid in self.operations(task["id"])
                    )
                    with self.control.db:
                        self.control.add_refs(erasure["forget_id"], refs)
                    self.db.execute(
                        "UPDATE outbox SET state='discarded',payload='',payload_bytes=0,"
                        "retry_requested=0,last_error='' WHERE id=?",
                        (task["id"],),
                    )
                self.db.execute(
                    "DELETE FROM window WHERE connection_id=? AND scope_id=? "
                    "AND (generation<>? OR ?)",
                    (
                        row["connection_id"],
                        row["scope_id"],
                        account["generation"],
                        changed,
                    ),
                )
            for task in self.db.execute(
                "SELECT o.*,s.owner_id FROM outbox o JOIN scopes s USING(connection_id,scope_id) "
                "WHERE o.state<>'discarded'"
            ).fetchall():
                record = self.control.request(task["operation_id"])
                if record:
                    if record["digest"] != task["payload_sha256"]:
                        raise QueueError("recovery_required:request_digest_mismatch")
                    self.db.execute(
                        "UPDATE outbox SET accepted=MAX(accepted,?),cancel_intent=?,state="
                        "CASE WHEN ? THEN 'cancel_requested' "
                        "WHEN ? AND state IN ('ready','submitting') THEN 'unknown' ELSE state END "
                        "WHERE id=?",
                        (
                            record["accepted"],
                            record["cancel_intent"],
                            record["cancel_intent"],
                            record["dispatched"] or record["accepted"],
                            task["id"],
                        ),
                    )
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('control_revision',?)",
                (self.control.setting("revision"),),
            )

    def _connection(self, url, fingerprint, revision):
        row = self.db.execute(
            "SELECT * FROM connections WHERE id=?",
            (self.connection_id,),
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO connections VALUES (?,?,?,?,1,'')",
                (self.connection_id, url, revision, fingerprint),
            )
        else:
            if row["url"] != url and revision <= row["address_revision"]:
                raise QueueError("service_address_revision_required")
            if revision < row["address_revision"]:
                raise QueueError("service_address_revision_rolled_back")
            changed = row["credential_fingerprint"] != fingerprint
            self.db.execute(
                "UPDATE connections SET url=?,address_revision=?,credential_fingerprint=?,"
                "credential_revision=credential_revision+?,"
                "paused_error=CASE WHEN ? THEN '' ELSE paused_error END WHERE id=?",
                (
                    url,
                    revision,
                    fingerprint,
                    int(changed),
                    int(changed),
                    self.connection_id,
                ),
            )

    def setting(self, key: str) -> str:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    def connection_error(self) -> str:
        return self.db.execute(
            "SELECT paused_error FROM connections WHERE id=?",
            (self.connection_id,),
        ).fetchone()[0]

    def pause_connection(self, error: str):
        with self.db:
            self.db.execute(
                "UPDATE connections SET paused_error=? WHERE id=?",
                (error, self.connection_id),
            )

    def known_accounts(self):
        return [
            (r["platform_instance_id"], r["user_id"])
            for r in self.db.execute(
                "SELECT platform_instance_id,user_id FROM scopes WHERE connection_id=?",
                (self.connection_id,),
            )
        ]

    def list_scopes(self):
        return self.db.execute(
            "SELECT * FROM scopes WHERE connection_id=? ORDER BY platform_instance_id,user_id",
            (self.connection_id,),
        ).fetchall()

    def sync_permissions(self, accounts):
        self.permission_pairs = frozenset(accounts)
        with self.db:
            for row in self.db.execute(
                "SELECT scope_id,user_id,platform_instance_id,allowed FROM scopes "
                "WHERE connection_id=?",
                (self.connection_id,),
            ).fetchall():
                allowed = (
                    row["platform_instance_id"],
                    row["user_id"],
                ) in self.permission_pairs
                self.db.execute(
                    "UPDATE scopes SET allowed=? WHERE connection_id=? AND scope_id=?",
                    (int(allowed), self.connection_id, row["scope_id"]),
                )
                if row["allowed"] and not allowed:
                    self.db.execute(
                        "DELETE FROM window WHERE connection_id=? AND scope_id=?",
                        (self.connection_id, row["scope_id"]),
                    )

    def scope(self, incoming: Incoming) -> sqlite3.Row:
        identity = incoming.scope(self.deployment_id)
        account = self.control.ensure_account(
            self.connection_id,
            identity,
            int(self.setting("accept_from")),
            agent_id=self.agent_id,
        )
        if account["agent_id"] != self.agent_id:
            raise QueueError("agent_mapping_change_requires_migration")
        permitted = (
            self.permission_pairs is None
            or (identity.platform_instance_id, identity.user_id)
            in self.permission_pairs
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO scopes(connection_id,scope_id,identity_json,"
                "platform_instance_id,user_id,bank,generation,mode,accept_from,allowed,"
                "owner_id,principal_id,revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.connection_id,
                    identity.id,
                    identity.identity,
                    identity.platform_instance_id,
                    identity.user_id,
                    account["bank"],
                    account["generation"],
                    account["mode"],
                    account["accept_from"],
                    int(permitted),
                    account["owner_id"],
                    account["principal_id"],
                    account["revision"],
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('control_revision',?)",
                (self.control.setting("revision"),),
            )
        return self.db.execute(
            "SELECT * FROM scopes WHERE connection_id=? AND scope_id=?",
            (self.connection_id, identity.id),
        ).fetchone()

    def current(self, task, *, writing=True) -> bool:
        row = self.db.execute(
            "SELECT * FROM scopes WHERE connection_id=? AND scope_id=?",
            (self.connection_id, task["scope_id"]),
        ).fetchone()
        owner = self.control.owner(self.connection_id, row["owner_id"]) if row else None
        return bool(
            task["connection_id"] == self.connection_id
            and row
            and owner
            and owner["bank"] == task["bank"]
            and owner["generation"] == task["generation"]
            and (
                not writing
                or (
                    not self.recovering
                    and row["allowed"]
                    and row["mode"] == "active"
                    and not self.control.maintenance(self.connection_id, row["bank"])
                    and self.control.bank_state(self.connection_id, row["bank"]) != "missing"
                    and not self.get(task["id"])["cancel_intent"]
                )
            )
            and row["bank"] == task["bank"]
            and row["generation"] == task["generation"]
        )

    def capture(
        self,
        incoming: Incoming,
        conversation_id: str,
        *,
        history_before="",
    ) -> Capture:
        if (
            not incoming.text.strip()
            or len(incoming.text) > 8000
            or not conversation_id
        ):
            raise QueueError("invalid_capture")
        scope = self.scope(incoming)
        if not self.can_recall(scope) or scope["mode"] != "active":
            raise QueueError("scope_not_active")
        source_digest = sha256(incoming.text)
        identity = incoming.scope(self.deployment_id)
        key = identity.event_key(incoming.message_id)
        args = (self.connection_id, identity.id, key)
        previous = self.db.execute(
            "SELECT * FROM events WHERE connection_id=? AND scope_id=? AND event_key=?",
            args,
        ).fetchone()
        if previous:
            if previous["text_sha256"] != source_digest:
                with self.db:
                    self.db.execute(
                        "UPDATE events SET conflict_count=conflict_count+1 "
                        "WHERE connection_id=? AND scope_id=? AND event_key=?",
                        args,
                    )
                raise QueueError("message_identity_content_conflict")
            return Capture(
                previous["task_id"],
                identity.id,
                scope["bank"],
                scope["generation"],
                (),
                False,
            )
        if incoming.timestamp < scope["accept_from"]:
            raise QueueError("message_before_accept_from")
        now = time.time()
        with self.db:
            self.prune_window(now)
            rows = self.db.execute(
                "SELECT w.event_key,w.text,w.occurred_at,r.text reply,r.observed_at "
                "FROM window w LEFT JOIN replies r ON r.user_task_id=w.task_id "
                "WHERE w.connection_id=? AND w.scope_id=? AND w.generation=? "
                "AND w.conversation_id=? AND w.occurred_at BETWEEN ? AND ? "
                "ORDER BY w.occurred_at DESC,w.task_id DESC LIMIT 4",
                (
                    self.connection_id,
                    identity.id,
                    scope["generation"],
                    conversation_id,
                    incoming.timestamp - 1800,
                    incoming.timestamp,
                ),
            ).fetchall()
            turns = []
            for row in reversed(rows):
                turn = [Message(row["event_key"], row["text"], row["occurred_at"])]
                if row["reply"]:
                    turn.append(
                        Message(
                            row["event_key"] + ":assistant",
                            row["reply"],
                            row["observed_at"],
                            "assistant",
                        )
                    )
                turns.append(turn)
            while sum(len(m.text) for turn in turns for m in turn) > 4000:
                turns.pop(0)
            history = [message for turn in turns for message in turn]
            current = Message(key, incoming.text, incoming.timestamp)
            payload = retain_payload(
                identity,
                scope["generation"],
                current,
                tuple(history),
                conversation_id,
                str(uuid.uuid4()),
                self.zone,
            )
            payload["items"][0]["metadata"].update(
                principal_id=scope["principal_id"],
                owner_id=scope["owner_id"],
                agent_id=self.agent_id,
            )
            task_id = self._store_task(scope, payload, now)
            self.db.execute(
                "INSERT INTO events(connection_id,scope_id,event_key,text_sha256,task_id,"
                "occurred_at,received_at) VALUES (?,?,?,?,?,?,?)",
                (
                    *args,
                    source_digest,
                    task_id,
                    incoming.timestamp,
                    incoming.received_at,
                ),
            )
            self.db.execute(
                "INSERT INTO window VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    self.connection_id,
                    identity.id,
                    scope["generation"],
                    conversation_id,
                    key,
                    incoming.text,
                    incoming.timestamp,
                    incoming.received_at,
                ),
            )
            if history_before:
                self.db.execute(
                    "INSERT INTO history_baselines VALUES (?,?)",
                    (task_id, history_before),
                )
            self.db.execute(
                "DELETE FROM window WHERE connection_id=? AND scope_id=? AND "
                "generation=? AND conversation_id=? AND task_id NOT IN ("
                "SELECT task_id FROM window WHERE connection_id=? AND scope_id=? "
                "AND generation=? AND conversation_id=? ORDER BY occurred_at DESC,task_id DESC LIMIT 4)",
                (self.connection_id, identity.id, scope["generation"], conversation_id)
                * 2,
            )
        return Capture(
            task_id,
            identity.id,
            scope["bank"],
            scope["generation"],
            tuple(history),
            True,
        )

    def _store_task(self, scope, payload, now):
        encoded = canonical(payload)
        size = len(encoded.encode("utf-8"))
        usage = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN state NOT IN ('done','discarded','cancelled') "
            "THEN 1 ELSE 0 END),0) n,COALESCE(SUM(payload_bytes),0) size FROM outbox",
        ).fetchone()
        if usage["n"] >= self.max_tasks or usage["size"] + size > self.max_bytes:
            raise QueueError("queue_capacity_reached")
        cursor = self.db.execute(
            "INSERT INTO outbox(connection_id,scope_id,bank,generation,document_id,"
            "operation_id,payload,payload_sha256,payload_bytes,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.connection_id,
                scope["scope_id"],
                scope["bank"],
                scope["generation"],
                payload["items"][0]["document_id"],
                payload["operation_id"],
                encoded,
                request_digest(
                    self.connection_id, scope["bank"], scope["generation"], payload
                ),
                size,
                now,
                now,
            ),
        )
        return cursor.lastrowid

    def capture_assistant(self, user_task_id, text, *, observed_at=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise QueueError("invalid_capture")
        source = self.get(user_task_id)
        anchor = self.db.execute(
            "SELECT * FROM window WHERE task_id=? AND connection_id=?",
            (user_task_id, self.connection_id),
        ).fetchone()
        if (
            not source
            or not anchor
            or not self.current(source)
            or source["cancel_intent"]
            or source["state"] in {"cancelled", "discarded"}
        ):
            raise QueueError("scope_not_active")
        previous = self.db.execute(
            "SELECT * FROM replies WHERE user_task_id=?", (user_task_id,)
        ).fetchone()
        if previous:
            if previous["text"] != text:
                raise QueueError("assistant_history_changed")
            return Capture(
                previous["task_id"],
                source["scope_id"],
                source["bank"],
                source["generation"],
                (),
                False,
            )
        scope = self.scope_owner(source["scope_id"])
        now = time.time()
        observed_at = int(now) if observed_at is None else observed_at
        if type(observed_at) is not int or observed_at <= 0 or observed_at > now + 300:
            raise QueueError("invalid_history_time")
        user = Message(anchor["event_key"], anchor["text"], anchor["occurred_at"])
        assistant = Message(user.event_key, text, observed_at, "assistant")
        payload = retain_payload(
            Scope(*json.loads(scope["identity_json"])),
            scope["generation"],
            assistant,
            (user,),
            anchor["conversation_id"],
            str(uuid.uuid4()),
            self.zone,
            assistant=True,
        )
        payload["items"][0]["metadata"].update(
            principal_id=scope["principal_id"],
            owner_id=scope["owner_id"],
            agent_id=self.agent_id,
            source_user_event_key=anchor["event_key"],
            source="astrbot.conversation_history",
        )
        with self.db:
            task_id = self._store_task(scope, payload, now)
            self.db.execute(
                "INSERT INTO replies VALUES (?,?,?,?)",
                (user_task_id, task_id, text, observed_at),
            )
        return Capture(
            task_id,
            source["scope_id"],
            source["bank"],
            source["generation"],
            (user,),
            True,
        )

    def history_anchor(self, scope_id, conversation_id):
        return self.db.execute(
            "SELECT w.*,b.digest history_before,r.task_id reply_task_id "
            "FROM window w LEFT JOIN history_baselines b ON b.task_id=w.task_id "
            "LEFT JOIN replies r ON r.user_task_id=w.task_id "
            "JOIN scopes s ON s.connection_id=w.connection_id AND s.scope_id=w.scope_id "
            "WHERE w.connection_id=? AND w.scope_id=? AND w.conversation_id=? "
            "AND w.generation=s.generation AND w.occurred_at>=s.accept_from "
            "ORDER BY w.task_id DESC LIMIT 1",
            (self.connection_id, scope_id, conversation_id),
        ).fetchone()

    def prune_window(self, now=None):
        self.db.execute(
            "DELETE FROM window WHERE received_at<?", ((now or time.time()) - 1800,)
        )

    def release(self, task_id: int):
        with self.db:
            self.db.execute("UPDATE outbox SET released=1 WHERE id=?", (task_id,))

    def get(self, task_id: int):
        return self.db.execute("SELECT * FROM outbox WHERE id=?", (task_id,)).fetchone()

    def due(self, exclude_scopes=()):
        if self.connection_error():
            return None
        return self._due(exclude_scopes)

    def _due(self, exclude_scopes):
        exclusion = ""
        args = [self.connection_id, time.time()]
        blocked = {
            row["bank"] for row in self.control.db.execute(
                "SELECT bank FROM maintenance WHERE connection_id=? UNION "
                "SELECT bank FROM banks WHERE connection_id=? AND state='missing'",
                (self.connection_id, self.connection_id),
            )
        }
        if blocked:
            exclusion += (
                " AND (o.state NOT IN ('ready','failed') OR o.bank NOT IN ("
                + ",".join("?" for _ in blocked) + "))"
            )
            args.extend(sorted(blocked))
        active = self.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE connection_id=? AND state IN "
            "('submitting','unknown','tracking','cancel_requested')",
            (self.connection_id,),
        ).fetchone()[0]
        if active >= 2:
            exclusion += " AND o.state NOT IN ('ready','failed')"
        if exclude_scopes:
            exclusion += (
                " AND s.owner_id NOT IN (" + ",".join("?" for _ in exclude_scopes) + ")"
            )
            args.extend(exclude_scopes)
        if self.recovering:
            return None
        return self.db.execute(
            "SELECT o.*,s.owner_id FROM outbox o JOIN scopes s USING(connection_id,scope_id) "
            "WHERE o.connection_id=? AND ((s.allowed=1 AND s.mode='active') "
            "OR o.state IN ('unknown','tracking','cancel_requested') OR o.cancel_intent=1) "
            "AND o.bank=s.bank AND o.generation=s.generation AND o.released=1 "
            "AND (o.state IN ('ready','unknown','tracking','cancel_requested') "
            "OR (o.state='cancelled' AND o.cancel_intent=1) "
            "OR (o.state='failed' AND o.retry_requested=1)) AND o.next_at<=? "
            "AND (o.state NOT IN ('ready','failed') OR NOT EXISTS (SELECT 1 FROM outbox a "
            "JOIN scopes a_scope ON a.connection_id=a_scope.connection_id AND a.scope_id=a_scope.scope_id "
            "WHERE a.connection_id=o.connection_id AND a_scope.owner_id=s.owner_id AND a.id<>o.id "
            "AND a.state IN ('submitting','unknown','tracking','cancel_requested'))) "
            "AND (o.state NOT IN ('ready','failed') OR NOT EXISTS "
            "(SELECT 1 FROM outbox p JOIN scopes p_scope ON p.connection_id=p_scope.connection_id "
            "AND p.scope_id=p_scope.scope_id WHERE p.connection_id=o.connection_id "
            "AND p_scope.owner_id=s.owner_id AND p.id<o.id AND (p.state IN "
            "('ready','submitting','unknown','tracking','cancel_requested','reconcile_required') "
            "OR (p.state='failed' AND p.retry_requested=1))))"
            + exclusion
            + " ORDER BY o.next_at,o.id LIMIT 1",
            args,
        ).fetchone()

    def claim(self, row):
        if row["state"] == "ready":
            self.update(row["id"], "submitting")
        elif row["state"] == "failed":
            self.update(row["id"], "tracking")
        return self.get(row["id"])

    def verify(self, row) -> dict:
        try:
            payload = json.loads(row["payload"])
            valid = (
                payload["operation_id"] == row["operation_id"]
                and len(payload["items"]) == 1
                and payload["items"][0]["document_id"] == row["document_id"]
                and request_digest(
                    row["connection_id"],
                    row["bank"],
                    row["generation"],
                    payload,
                )
                == row["payload_sha256"]
            )
        except (ValueError, KeyError, TypeError, IndexError):
            valid = False
        if not valid:
            raise QueueError("payload_digest_conflict")
        return payload

    def update(
        self,
        task_id: int,
        state: str,
        *,
        delay=0,
        error="",
        attempts=None,
        accepted=None,
        retry_requested=None,
    ):
        if state not in STATES:
            raise QueueError("invalid_outbox_state")
        row = self.get(task_id)
        if row is None or (row["state"] == "discarded" and state != "discarded"):
            return
        if accepted:
            owner = self.scope_owner(row["scope_id"])
            self.control.record_request(row, owner["owner_id"], accepted=True)
        if row["cancel_intent"] and state not in {
            "cancelled",
            "discarded",
            "reconcile_required",
        }:
            state = "cancel_requested"
            retry_requested = 0
        clear = state in {"done", "discarded", "cancelled"}
        with self.db:
            self.db.execute(
                "UPDATE outbox SET state=?,next_at=?,last_error=?,updated_at=?,"
                "attempts=COALESCE(?,attempts),accepted=COALESCE(?,accepted),"
                "retry_requested=COALESCE(?,retry_requested),"
                "payload=CASE WHEN ? THEN '' ELSE payload END,"
                "payload_bytes=CASE WHEN ? THEN 0 ELSE payload_bytes END,"
                "completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE id=?",
                (
                    state,
                    time.time() + delay,
                    error,
                    time.time(),
                    attempts,
                    accepted,
                    retry_requested,
                    clear,
                    clear,
                    clear,
                    time.time(),
                    task_id,
                ),
            )
            if state in {"discarded", "cancelled"}:
                self.db.execute("DELETE FROM window WHERE task_id=?", (task_id,))

    def save_operations(self, task_id: int, nodes: dict):
        with self.db:
            for operation_id, node in nodes.items():
                self.db.execute(
                    "INSERT INTO operations VALUES (?,?,?,?) ON CONFLICT(task_id,operation_id)"
                    " DO UPDATE SET parent_id=excluded.parent_id,status=excluded.status",
                    (task_id, operation_id, node["parent_id"], node["status"]),
                )

    def operations(self, task_id: int) -> dict:
        return {
            r["operation_id"]: {"parent_id": r["parent_id"], "status": r["status"]}
            for r in self.db.execute(
                "SELECT * FROM operations WHERE task_id=?", (task_id,)
            )
        }

    def request_retry(self, scope_id: str) -> int:
        scope = self.scope_owner(scope_id)
        if not scope or not self.can_recall(scope, manual=True) or scope["mode"] != "active":
            raise QueueError("scope_not_active")
        with self.db:
            cursor = self.db.execute(
                "UPDATE outbox SET retry_requested=1,next_at=0,attempts=0 "
                "WHERE connection_id=? AND scope_id IN "
                "(SELECT scope_id FROM scopes WHERE connection_id=? AND owner_id=?) "
                "AND state='failed' AND cancel_intent=0 "
                "AND last_error NOT IN ('payload_digest_conflict','control_request_digest_conflict')",
                (self.connection_id, self.connection_id, scope["owner_id"]),
            )
        return cursor.rowcount

    def summary(self, scope_id: str) -> dict:
        scope = self.scope_owner(scope_id)
        rows = self.db.execute(
            "SELECT state,COUNT(*) n FROM outbox WHERE connection_id=? "
            "AND scope_id IN (SELECT scope_id FROM scopes WHERE connection_id=? AND owner_id=?) "
            "GROUP BY state",
            (self.connection_id, self.connection_id, scope["owner_id"]),
        )
        counts = {r["state"]: r["n"] for r in rows}
        row = self.db.execute(
            "SELECT last_error FROM outbox WHERE connection_id=? AND scope_id=? "
            "AND last_error<>'' ORDER BY updated_at DESC LIMIT 1",
            (self.connection_id, scope_id),
        ).fetchone()
        oldest = self.db.execute(
            "SELECT MIN(created_at) FROM outbox WHERE connection_id=? AND scope_id IN "
            "(SELECT scope_id FROM scopes WHERE connection_id=? AND owner_id=?) "
            "AND state IN ('ready','submitting','unknown','tracking','failed','reconcile_required')",
            (self.connection_id, self.connection_id, scope["owner_id"]),
        ).fetchone()[0]
        tasks = self.db.execute(
            "SELECT id,state FROM outbox WHERE connection_id=? AND scope_id IN "
            "(SELECT scope_id FROM scopes WHERE connection_id=? AND owner_id=?) "
            "AND state NOT IN ('done','discarded') ORDER BY id DESC LIMIT 10",
            (self.connection_id, self.connection_id, scope["owner_id"]),
        ).fetchall()
        return {
            "counts": counts,
            "last_error": row["last_error"] if row else "",
            "oldest_seconds": max(0, int(time.time() - oldest)) if oldest else 0,
            "tasks": [{"id": r["id"], "state": r["state"]} for r in tasks],
            "maintenance": self.control.maintenance(self.connection_id, scope["bank"]),
            "bank_missing": self.control.bank_state(self.connection_id, scope["bank"]) == "missing",
        }

    def scope_owner(self, scope_id):
        return self.db.execute(
            "SELECT * FROM scopes WHERE connection_id=? AND scope_id=?",
            (self.connection_id, scope_id),
        ).fetchone()

    def can_recall(self, scope, *, manual=False):
        latest = self.scope_owner(scope["scope_id"])
        owner = self.control.owner(self.connection_id, scope["owner_id"])
        return bool(
            not self.recovering
            and latest
            and latest["allowed"]
            and owner
            and owner["bank"] == scope["bank"]
            and owner["revision"] == scope["revision"]
            and latest["revision"] == scope["revision"]
            and latest["bank"] == scope["bank"]
            and not self.control.maintenance(self.connection_id, scope["bank"])
            and self.control.bank_state(self.connection_id, scope["bank"]) != "missing"
            and (manual or latest["mode"] in {"active", "readonly"})
        )

    def set_mode(self, scope_id, mode, *, now=None):
        row = self.scope_owner(scope_id)
        if not row or not row["allowed"] or self.recovering:
            raise QueueError("scope_not_available")
        if mode != "paused":
            if self.control.maintenance(self.connection_id, row["bank"]):
                raise QueueError("maintenance_active")
            if self.control.bank_state(self.connection_id, row["bank"]) == "missing":
                raise QueueError("bank_missing")
        self.control.set_mode(self.connection_id, row["owner_id"], mode, now=now)
        self.apply_control()
        return self.scope_owner(scope_id)

    def before_submit(self, row):
        if not self.current(row):
            raise QueueError("scope_changed")
        scope = self.scope_owner(row["scope_id"])
        self.control.record_request(row, scope["owner_id"], dispatched=True)

    def request_cancel(self, scope_id, task_id):
        scope, task = self.scope_owner(scope_id), self.get(task_id)
        target_scope = self.scope_owner(task["scope_id"]) if task else None
        if (
            not scope
            or not scope["allowed"]
            or self.recovering
            or not task
            or task["connection_id"] != self.connection_id
            or not target_scope
            or target_scope["owner_id"] != scope["owner_id"]
            or task["bank"] != scope["bank"]
            or task["state"] == "discarded"
        ):
            raise QueueError("task_outside_memory_scope")
        self.control.record_request(task, scope["owner_id"], cancel=True)
        with self.db:
            self.db.execute(
                "UPDATE outbox SET cancel_intent=1,retry_requested=0,released=1,next_at=0 "
                "WHERE id=?",
                (task_id,),
            )
        record = self.control.request(task["operation_id"])
        if (
            not task["accepted"]
            and task["state"] == "ready"
            and not record["dispatched"]
        ):
            self.update(task_id, "cancelled", delay=60, error="cancelled_before_submit")
        else:
            self.update(task_id, "cancel_requested")
        return self.get(task_id)["state"]

    def forget(self, scope_id, *, now=None):
        row = self.scope_owner(scope_id)
        if not row or not row["allowed"] or self.recovering:
            raise QueueError("scope_not_available")
        tasks = self.db.execute(
            "SELECT * FROM outbox WHERE connection_id=? AND bank=?",
            (self.connection_id, row["bank"]),
        ).fetchall()
        refs = []
        for task in tasks:
            refs.extend(
                [("operation", task["operation_id"]), ("document", task["document_id"])]
            )
            refs.extend(("operation", oid) for oid in self.operations(task["id"]))
        erasure = self.control.forget(
            self.connection_id, row["owner_id"], refs, now=now
        )
        # A failed second commit leaves the control journal authoritative at restart.
        try:
            self.apply_control()
        except BaseException:
            self.recovering = True
            raise
        return erasure

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.control is not None:
            self.control.close()
            self.control = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None
