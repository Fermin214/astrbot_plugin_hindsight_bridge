"""Durable ownership and erasure records outside the restorable message queue."""

import math
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .identity import Scope, canonical, sha256


class QueueError(ValueError):
    pass


class Control:
    def __init__(self, directory: Path, deployment_id="", *, required=False):
        self.directory = Path(directory)
        self.db = self.lock = None
        if required and not (self.directory / "control.sqlite3").is_file():
            raise QueueError("recovery_required:control_missing")
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.lock = (self.directory / "control.lock").open("a+b")
            from .locking import exclusive_lock

            exclusive_lock(self.lock)
            self.db = sqlite3.connect(self.directory / "control.sqlite3", timeout=2)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if required and version == 0:
                raise QueueError("recovery_required:control_uninitialized")
            if version not in (0, 1):
                raise QueueError("unsupported_control_version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS owners(
                    connection_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    memory_space_id TEXT NOT NULL, bank TEXT NOT NULL,
                    generation INTEGER NOT NULL, mode TEXT NOT NULL,
                    accept_from INTEGER NOT NULL, revision INTEGER NOT NULL,
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
                    UNIQUE(connection_id,old_bank)
                );
                CREATE TABLE IF NOT EXISTS erasure_refs(
                    forget_id TEXT NOT NULL REFERENCES erasures(forget_id),
                    kind TEXT NOT NULL, ref TEXT NOT NULL,
                    PRIMARY KEY(forget_id,kind,ref)
                );
                CREATE TABLE IF NOT EXISTS requests(
                    operation_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, bank TEXT NOT NULL, digest TEXT NOT NULL,
                    dispatched INTEGER NOT NULL DEFAULT 0,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    cancel_intent INTEGER NOT NULL DEFAULT 0
                );
                PRAGMA user_version=1;
            """)
            with self.db:
                for key, value in (
                    ("deployment_id", deployment_id or str(uuid.uuid4())),
                    ("control_id", str(uuid.uuid4())),
                    ("revision", "0"),
                ):
                    self.db.execute(
                        "INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value)
                    )
            if deployment_id and deployment_id != self.setting("deployment_id"):
                raise QueueError("recovery_required:deployment_mismatch")
            if os.name != "nt":
                os.chmod(self.directory, 0o700)
                os.chmod(self.directory / "control.sqlite3", 0o600)
        except BaseException:
            self.close()
            raise

    def setting(self, key):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    def bump(self):
        self.db.execute(
            "UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'"
        )

    def owner(self, connection, owner_id):
        return self.db.execute(
            "SELECT * FROM owners WHERE connection_id=? AND owner_id=?",
            (connection, owner_id),
        ).fetchone()

    def account(self, connection, source_id):
        return self.db.execute(
            "SELECT a.*,o.principal_id,o.agent_id,o.memory_space_id,o.bank,o.generation,"
            "o.mode,o.accept_from,o.revision FROM accounts a JOIN owners o "
            "USING(connection_id,owner_id) WHERE connection_id=? AND source_id=?",
            (connection, source_id),
        ).fetchone()

    def ensure_account(
        self, connection, source: Scope, accept_from, *, agent_id="default", legacy=None
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
        bank = legacy["bank"] if legacy else source.bank()
        generation = legacy["generation"] if legacy else 0
        mode = legacy["mode"] if legacy else "active"
        lower = legacy["accept_from"] if legacy else accept_from
        with self.db:
            self.db.execute(
                "INSERT INTO owners VALUES (?,?,?,?,?,?,?,?,?,1)",
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
                ),
            )
            self.db.execute(
                "INSERT INTO banks VALUES (?,?,?,?)",
                (connection, owner_id, generation, bank),
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

    def attach_verified_account(self, connection, source: Scope, owner_id: str):
        """Attach a new, externally verified account; historical Bank merges are separate."""
        owner = self.owner(connection, owner_id)
        if not owner or source.deployment_id != self.setting("deployment_id"):
            raise QueueError("identity_mapping_target_missing")
        if source.memory_space_id != owner["memory_space_id"]:
            raise QueueError("identity_mapping_space_mismatch")
        previous = self.account(connection, source.id)
        if previous:
            if previous["owner_id"] != owner_id:
                raise QueueError("existing_account_requires_explicit_migration")
            return previous
        with self.db:
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
        if row["mode"] == mode:
            return row
        lower = (
            max(row["accept_from"], math.ceil(time.time() if now is None else now))
            if mode == "active"
            else row["accept_from"]
        )
        with self.db:
            self.db.execute(
                "UPDATE owners SET mode=?,accept_from=?,revision=revision+1 "
                "WHERE connection_id=? AND owner_id=?",
                (mode, lower, connection, owner_id),
            )
            self.bump()
        return self.owner(connection, owner_id)

    def forget(self, connection, owner_id, refs=(), *, now=None):
        row = self.owner(connection, owner_id)
        if row is None:
            raise QueueError("memory_owner_missing")
        stamp = time.time() if now is None else now
        fid, generation = str(uuid.uuid4()), row["generation"] + 1
        bank = f"abm-v1-{uuid.uuid4().hex}-g{generation}"
        lower = max(row["accept_from"], math.ceil(stamp))
        with self.db:
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
            self.db.execute(
                "INSERT INTO banks VALUES (?,?,?,?)",
                (connection, owner_id, generation, bank),
            )
            self.db.execute(
                "UPDATE owners SET bank=?,generation=?,mode='paused',accept_from=?,"
                "revision=revision+1 WHERE connection_id=? AND owner_id=?",
                (bank, generation, lower, connection, owner_id),
            )
            self.bump()
        return self.db.execute(
            "SELECT * FROM erasures WHERE forget_id=?", (fid,)
        ).fetchone()

    def add_refs(self, forget_id, refs):
        # The caller controls the transaction so the initial erasure and its references commit together.
        self.db.executemany(
            "INSERT OR IGNORE INTO erasure_refs VALUES (?,?,?)",
            ((forget_id, kind, ref) for kind, ref in refs),
        )

    def refs(self, forget_id, kind):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT ref FROM erasure_refs WHERE forget_id=? AND kind=?",
                (forget_id, kind),
            )
        ]

    def erasures(self, connection, *, owner_id=None):
        clause = " AND owner_id=?" if owner_id else ""
        return self.db.execute(
            "SELECT * FROM erasures WHERE connection_id=?" + clause + " ORDER BY seq",
            (connection, owner_id) if owner_id else (connection,),
        ).fetchall()

    def cleanup_state(self, forget_id, phase, error="", *, delay=60):
        with self.db:
            self.db.execute(
                "UPDATE erasures SET phase=?,last_error=?,next_at=?,verified_at="
                "CASE WHEN ?='verified' THEN ? ELSE verified_at END WHERE forget_id=?",
                (phase, error, time.time() + delay, phase, time.time(), forget_id),
            )

    def retired_request_returned(self, connection, bank):
        with self.db:
            self.db.execute(
                "UPDATE erasures SET phase='pending',next_at=0 "
                "WHERE connection_id=? AND old_bank=?",
                (connection, bank),
            )

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
        with self.db:
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
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None
