"""主库内的身份、授权、清空与操作记录。"""

import math
import json
import time
import uuid
from contextlib import contextmanager

from .identity import Scope, canonical, sha256
from .scope_settings import forget_settings


class QueueError(ValueError):
    pass


class Control:
    def __init__(self, database):
        self.db = database

    def create_schema(self):
        self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS owners(
                    connection_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    memory_space_id TEXT NOT NULL, bank TEXT NOT NULL,
                    generation INTEGER NOT NULL, mode TEXT NOT NULL,
                    accept_from INTEGER NOT NULL, revision INTEGER NOT NULL,
                    command_from REAL NOT NULL DEFAULT 0,
                    command_received_from REAL NOT NULL DEFAULT 0,
                    receive_from REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(connection_id,owner_id),
                    UNIQUE(connection_id,bank)
                );
                CREATE TABLE IF NOT EXISTS accounts(
                    connection_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    identity_json TEXT NOT NULL, platform_instance_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    PRIMARY KEY(connection_id,source_id),
                    FOREIGN KEY(connection_id,owner_id)
                        REFERENCES owners(connection_id,owner_id)
                );
                CREATE TABLE IF NOT EXISTS banks(
                    connection_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, bank TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'established',
                    PRIMARY KEY(connection_id,bank),
                    UNIQUE(connection_id,owner_id,generation)
                );
                CREATE TABLE IF NOT EXISTS erasures(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    forget_id TEXT NOT NULL UNIQUE, connection_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                    old_bank TEXT NOT NULL, old_generation INTEGER NOT NULL,
                    new_bank TEXT NOT NULL, new_generation INTEGER NOT NULL,
                    accept_from INTEGER NOT NULL, created_at REAL NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0,
                    verified_at REAL,
                    cleanup_step INTEGER NOT NULL DEFAULT 0,
                    cleanup_epoch INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(connection_id,old_bank)
                );
                CREATE TABLE IF NOT EXISTS erasure_refs(
                    forget_id TEXT NOT NULL REFERENCES erasures(forget_id),
                    kind TEXT NOT NULL, ref TEXT NOT NULL,
                    checked_pass INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(forget_id,kind,ref)
                );
                CREATE TABLE IF NOT EXISTS requests(
                    operation_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, bank TEXT NOT NULL, digest TEXT NOT NULL,
                    dispatched INTEGER NOT NULL DEFAULT 0,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    cancel_intent INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS reflection_requests(
                    request_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, bank TEXT NOT NULL, generation INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','unknown','overridden')),
                    created_at REAL NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                    overridden_at REAL, overridden_by TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS maintenance(
                    connection_id TEXT NOT NULL, bank TEXT NOT NULL,
                    started_at REAL NOT NULL, discard_pending INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'draining',
                    PRIMARY KEY(connection_id,bank)
                );
                CREATE TABLE IF NOT EXISTS authorizations(
                    connection_id TEXT NOT NULL, platform_instance_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                    accept_from REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(connection_id,platform_instance_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS command_receipts(
                    connection_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    event_key TEXT NOT NULL, digest TEXT NOT NULL, occurred_at REAL NOT NULL,
                    authorization_revision INTEGER NOT NULL, bank TEXT NOT NULL,
                    generation INTEGER NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(connection_id,source_id,event_key)
                );
                CREATE TABLE IF NOT EXISTS retry_intents(
                    operation_id TEXT PRIMARY KEY REFERENCES requests(operation_id),
                    pending INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS erasure_refs_cleanup
                    ON erasure_refs(forget_id,kind,checked_pass,ref);
                CREATE UNIQUE INDEX IF NOT EXISTS reflection_requests_active
                    ON reflection_requests(connection_id,bank) WHERE state IN ('prepared','dispatched','unknown');
        """)

    def setting(self, key):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def bump(self):
        self.db.execute(
            "UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'"
        )

    def invalidate(self, connection, owner_id):
        self.db.execute(
            "DELETE FROM window WHERE connection_id=? AND scope_id IN "
            "(SELECT source_id FROM accounts WHERE connection_id=? AND owner_id=?)",
            (connection, connection, owner_id),
        )

    def authorization_changed(self, connection, source_id):
        account = self.account(connection, source_id)
        if account:
            self.db.execute(
                "UPDATE owners SET revision=revision+1 WHERE connection_id=? AND owner_id=?",
                (connection, account["owner_id"]),
            )
            self.invalidate(connection, account["owner_id"])

    def owner(self, connection, owner_id):
        return self.db.execute(
            "SELECT * FROM owners WHERE connection_id=? AND owner_id=?",
            (connection, owner_id),
        ).fetchone()

    def account(self, connection, source_id):
        return self.db.execute(
            "SELECT a.*,o.principal_id,o.agent_id,o.memory_space_id,o.bank,o.generation,"
            "o.mode,o.accept_from,o.receive_from,o.revision FROM accounts a JOIN owners o "
            "USING(connection_id,owner_id) WHERE connection_id=? AND source_id=?",
            (connection, source_id),
        ).fetchone()

    def ensure_account(
        self, connection, source: Scope, accept_from, *, agent_id="default"
    ):
        row = self.account(connection, source.id)
        if row:
            return row
        principal_id = "p-" + uuid.uuid4().hex
        owner_id = sha256(
            canonical(
                [source.deployment_id, agent_id, principal_id, source.memory_space_id]
            )
        )
        bank, generation, mode, lower = source.bank(), 0, "active", accept_from
        with self.transaction():
            self.db.execute(
                "INSERT INTO owners(connection_id,owner_id,principal_id,agent_id,"
                "memory_space_id,bank,generation,mode,accept_from,revision,command_from,"
                "command_received_from) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    connection,
                    owner_id,
                    principal_id,
                    agent_id,
                    source.memory_space_id,
                    bank,
                    generation,
                    mode,
                    lower,
                    time.time(),
                    time.time(),
                ),
            )
            self.db.execute(
                "INSERT INTO banks(connection_id,owner_id,generation,bank,state) VALUES (?,?,?,?,?)",
                (connection, owner_id, generation, bank, "new"),
            )
            self.db.execute(
                "INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                (
                    connection,
                    source.id,
                    source.identity,
                    source.platform_instance_id,
                    source.user_id,
                    owner_id,
                ),
            )
            self.bump()
        return self.account(connection, source.id)

    def set_mode(self, connection, owner_id, mode, *, now=None):
        if mode not in {"active", "readonly", "paused"}:
            raise QueueError("invalid_memory_mode")
        row = self.owner(connection, owner_id)
        if not row:
            raise QueueError("memory_owner_missing")
        lower = (
            max(
                row["accept_from"],
                math.ceil(time.time() if now is None else now),
            )
            if mode == "active"
            else row["accept_from"]
        )
        stamp = time.time() if now is None else now
        received = max(stamp, row["receive_from"])
        with self.transaction():
            self.db.execute(
                "UPDATE owners SET mode=?,accept_from=?,revision=revision+1,"
                "receive_from=CASE WHEN ? THEN MAX(receive_from,?) ELSE receive_from END "
                "WHERE connection_id=? AND owner_id=?",
                (mode, lower, mode == "active", received, connection, owner_id),
            )
            self.invalidate(connection, owner_id)
            self.bump()
        return self.owner(connection, owner_id)

    def forget(self, connection, owner_id, refs=(), *, now=None):
        row = self.owner(connection, owner_id)
        if row is None:
            raise QueueError("memory_owner_missing")
        stamp = time.time() if now is None else now
        received = max(stamp, row["receive_from"])
        fid, generation = str(uuid.uuid4()), row["generation"] + 1
        bank = f"abm-v1-{uuid.uuid4().hex}-g{generation}"
        lower = max(row["accept_from"], math.ceil(stamp))
        with self.transaction():
            self.db.execute(
                "INSERT INTO erasures(forget_id,connection_id,owner_id,principal_id,"
                "old_bank,old_generation,new_bank,new_generation,accept_from,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    connection,
                    owner_id,
                    row["principal_id"],
                    row["bank"],
                    row["generation"],
                    bank,
                    generation,
                    lower,
                    stamp,
                ),
            )
            self.add_refs(fid, refs)
            for task in self.db.execute(
                "SELECT id,operation_id,document_id FROM outbox WHERE connection_id=? AND bank=?",
                (connection, row["bank"]),
            ).fetchall():
                self.add_refs(fid, (("operation", task["operation_id"]), ("document", task["document_id"])))
                self.add_refs(fid, (("operation", item[0]) for item in self.db.execute(
                    "SELECT operation_id FROM operations WHERE task_id=?", (task["id"],),
                )))
            self.db.execute(
                "UPDATE retry_intents SET pending=0 WHERE operation_id IN "
                "(SELECT operation_id FROM outbox WHERE connection_id=? AND bank=?)",
                (connection, row["bank"]),
            )
            self.db.execute(
                "UPDATE outbox SET state='discarded',payload='',payload_bytes=0,"
                "retry_requested=0,last_error='' WHERE connection_id=? AND bank=?",
                (connection, row["bank"]),
            )
            self.invalidate(connection, owner_id)
            self.db.execute(
                "INSERT INTO banks(connection_id,owner_id,generation,bank,state) VALUES (?,?,?,?,?)",
                (connection, owner_id, generation, bank, "new"),
            )
            self.db.execute(
                "UPDATE owners SET bank=?,generation=?,mode='paused',accept_from=?,"
                "revision=revision+1,receive_from=MAX(receive_from,?) "
                "WHERE connection_id=? AND owner_id=?",
                (bank, generation, lower, received, connection, owner_id),
            )
            forget_settings(self, connection, owner_id)
            self.bump()
        return self.db.execute(
            "SELECT * FROM erasures WHERE forget_id=?", (fid,)
        ).fetchone()

    def add_refs(self, forget_id, refs):
        # The caller controls the transaction so the initial erasure and its references commit together.
        changes = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO erasure_refs(forget_id,kind,ref) VALUES (?,?,?)",
            ((forget_id, kind, ref) for kind, ref in refs),
        )
        if self.db.total_changes != changes:
            progress = self.db.execute(
                "SELECT cleanup_step FROM erasures WHERE forget_id=?", (forget_id,)
            ).fetchone()
            if progress["cleanup_step"] >= 4:
                # Restored queue records must invalidate a completed cancellation sweep.
                self.restart_cleanup(forget_id)

    def bank_state(self, connection, bank):
        row = self.db.execute(
            "SELECT state FROM banks WHERE connection_id=? AND bank=?", (connection, bank)
        ).fetchone()
        if row is None:
            raise QueueError("bank_mapping_missing")
        return row["state"]

    def set_bank_state(self, connection, bank, state):
        if state not in {"new", "established", "missing"}:
            raise QueueError("invalid_bank_state")
        with self.transaction():
            self.db.execute(
                "UPDATE banks SET state=? WHERE connection_id=? AND bank=?",
                (state, connection, bank),
            )
            if state == "missing":
                self.db.execute(
                    "UPDATE owners SET revision=revision+1 WHERE connection_id=? AND bank=?",
                    (connection, bank),
                )
                for owner in self.db.execute(
                    "SELECT owner_id FROM owners WHERE connection_id=? AND bank=?", (connection, bank),
                ).fetchall():
                    self.invalidate(connection, owner["owner_id"])
                self.bump()

    def maintenance(self, connection, bank):
        row = self.db.execute(
            "SELECT * FROM maintenance WHERE connection_id=? AND bank=?", (connection, bank)
        ).fetchone()
        return dict(row) if row else None

    def begin_maintenance(self, connection, owner_id, *, discard_pending=False):
        owner = self.owner(connection, owner_id)
        if not owner:
            raise QueueError("memory_owner_missing")
        with self.transaction():
            self.db.execute(
                "INSERT INTO maintenance(connection_id,bank,started_at,discard_pending) "
                "VALUES (?,?,?,?) ON CONFLICT(connection_id,bank) DO UPDATE SET "
                "discard_pending=MAX(discard_pending,excluded.discard_pending),phase='draining'",
                (connection, owner["bank"], time.time(), int(discard_pending)),
            )
            self.db.execute(
                "UPDATE owners SET revision=revision+1 "
                "WHERE connection_id=? AND owner_id=?",
                (connection, owner_id),
            )
            self.invalidate(connection, owner_id)
            self.bump()
        return self.maintenance(connection, owner["bank"])

    def finish_maintenance(self, connection, owner_id):
        owner = self.owner(connection, owner_id)
        if not owner:
            raise QueueError("memory_owner_missing")
        stamp = time.time()
        received = max(stamp, owner["receive_from"])
        with self.transaction():
            self.db.execute(
                "DELETE FROM maintenance WHERE connection_id=? AND bank=?",
                (connection, owner["bank"]),
            )
            self.db.execute(
                "UPDATE owners SET revision=revision+1,accept_from=MAX(accept_from,?),"
                "receive_from=MAX(receive_from,?) "
                "WHERE connection_id=? AND owner_id=?",
                (math.ceil(stamp), received, connection, owner_id),
            )
            self.invalidate(connection, owner_id)
            self.bump()

    def refs(self, forget_id, kind, *, after=None, limit=128):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT ref FROM erasure_refs WHERE forget_id=? AND kind=? "
                "AND (? IS NULL OR ref>?) ORDER BY ref LIMIT ?",
                (forget_id, kind, after, after, limit),
            )
        ]

    def cleanup_current(self, forget_id, epoch):
        return bool(self.db.execute(
            "SELECT 1 FROM erasures WHERE forget_id=? AND cleanup_epoch=?",
            (forget_id, epoch),
        ).fetchone())

    def erasures(self, connection, *, owner_id=None):
        clause = " AND owner_id=?" if owner_id else ""
        return self.db.execute(
            "SELECT * FROM erasures WHERE connection_id=?" + clause + " ORDER BY seq",
            (connection, owner_id) if owner_id else (connection,),
        ).fetchall()

    def cleanup_refs(self, forget_id, kind, checked_pass, *, limit=128):
        return [
            row[0] for row in self.db.execute(
                "SELECT ref FROM erasure_refs WHERE forget_id=? AND kind=? "
                "AND checked_pass<? ORDER BY checked_pass,ref LIMIT ?",
                (forget_id, kind, checked_pass, limit),
            )
        ]

    def cleanup_ref_done(self, forget_id, kind, ref, checked_pass, epoch):
        with self.transaction():
            return bool(self.db.execute(
                "UPDATE erasure_refs SET checked_pass=? WHERE forget_id=? AND kind=? AND ref=? "
                "AND EXISTS (SELECT 1 FROM erasures WHERE forget_id=? AND cleanup_epoch=?)",
                (checked_pass, forget_id, kind, ref, forget_id, epoch),
            ).rowcount)

    def restart_cleanup(self, forget_id, *, epoch=None):
        # Like add_refs, this participates in the caller's transaction.
        changed = self.db.execute(
            "UPDATE erasures SET phase='pending',last_error='',next_at=0,"
            "cleanup_step=0,cleanup_epoch=cleanup_epoch+1 WHERE forget_id=? "
            "AND (? IS NULL OR cleanup_epoch=?)",
            (forget_id, epoch, epoch),
        ).rowcount
        if changed:
            self.db.execute(
                "UPDATE erasure_refs SET checked_pass=0 WHERE forget_id=?", (forget_id,)
            )
        return bool(changed)

    def cleanup_state(self, forget_id, phase, error="", *, delay=60, step=None, epoch=None):
        with self.transaction():
            return bool(self.db.execute(
                "UPDATE erasures SET phase=?,last_error=?,next_at=?,verified_at="
                "CASE WHEN ?='verified' THEN ? ELSE verified_at END,"
                "cleanup_step=COALESCE(?,cleanup_step) WHERE forget_id=? "
                "AND (? IS NULL OR cleanup_epoch=?)",
                (phase, error, time.time() + delay, phase, time.time(), step,
                 forget_id, epoch, epoch),
            ).rowcount)

    def retired_request_returned(self, connection, bank):
        with self.transaction():
            row = self.db.execute(
                "SELECT forget_id FROM erasures WHERE connection_id=? AND old_bank=?",
                (connection, bank),
            ).fetchone()
            if row:
                self.restart_cleanup(row["forget_id"])

    def request(self, operation_id):
        return self.db.execute(
            "SELECT * FROM requests WHERE operation_id=?", (operation_id,)
        ).fetchone()

    def record_request(
        self, row, owner_id, *, dispatched=False, accepted=False, cancel=False
    ):
        previous = self.request(row["operation_id"])
        if previous and (
            previous["digest"] != row["payload_sha256"]
            or previous["bank"] != row["bank"]
            or previous["connection_id"] != row["connection_id"]
            or previous["owner_id"] != owner_id
        ):
            raise QueueError("control_request_digest_conflict")
        if previous and (
            previous["dispatched"] >= int(dispatched)
            and previous["accepted"] >= int(accepted)
            and previous["cancel_intent"] >= int(cancel)
        ):
            return
        with self.transaction():
            self.db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET "
                "dispatched=MAX(dispatched,excluded.dispatched),"
                "accepted=MAX(accepted,excluded.accepted),"
                "cancel_intent=MAX(cancel_intent,excluded.cancel_intent)",
                (
                    row["operation_id"],
                    row["connection_id"],
                    owner_id,
                    row["bank"],
                    row["payload_sha256"],
                    int(dispatched),
                    int(accepted),
                    int(cancel),
                ),
            )

    def close(self):
        self.db = None
