from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .control import Control, QueueError
from .database import Database
from .identity import Scope, canonical, sha256
from .locking import exclusive_lock


VERSION = 8
FORMAT = "bridge-v1"
BACKUP = "legacy-v0.5-backup"
CONTROL_TABLES = {
    "settings", "owners", "accounts", "banks", "erasures", "erasure_refs", "requests",
    "reflection_requests", "maintenance", "authorizations", "command_receipts", "retry_intents",
}
RUNTIME_TABLES = {
    "settings", "connections", "scopes", "outbox", "events", "window", "operations",
    "replies", "history_baselines",
}
MAIN_TABLES = (CONTROL_TABLES | RUNTIME_TABLES | {"history_prompts"}) - {"scopes"}
IGNORED = {BACKUP, BACKUP + ".building", "worker.lock", "control.lock"} | {
    name + suffix for name in ("bridge.sqlite3", "bridge.sqlite3.migrating")
    for suffix in ("", "-wal", "-shm")
}


def safe_path(path):
    if any(path.with_name(path.name + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
        raise QueueError("database_path_invalid")


def connect(path):
    safe_path(path)
    database = sqlite3.connect(path, timeout=2, factory=Database)
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA secure_delete=ON")
        if os.name != "nt":
            os.chmod(path, 0o600)
        return database
    except BaseException:
        database.close()
        raise


def settings(database):
    return dict(database.execute("SELECT key,value FROM settings"))


def tables(database):
    return {row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
    )}


def valid(database):
    return (database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            and not database.execute("PRAGMA foreign_key_check").fetchall())


def validate_main(database):
    if (database.execute("PRAGMA user_version").fetchone()[0] != VERSION
            or not MAIN_TABLES <= tables(database) or not valid(database)
            or not database.execute("SELECT 1 FROM sqlite_master WHERE name='scopes' AND type='view'").fetchone()):
        raise QueueError("main_database_invalid")
    values = settings(database)
    if values.get("storage_format") != FORMAT or not all(values.get(key) for key in ("deployment_id", "control_id")):
        raise QueueError("main_database_invalid")
    for row in database.execute("SELECT * FROM accounts"):
        source = Scope(*json.loads(row["identity_json"]))
        if (source.deployment_id != values["deployment_id"] or source.id != row["source_id"]
                or source.platform_instance_id != row["platform_instance_id"] or source.user_id != row["user_id"]):
            raise QueueError("main_identity_mismatch")
    if database.execute(
        "SELECT 1 FROM owners LEFT JOIN banks USING(connection_id,bank) "
        "WHERE banks.owner_id IS NULL OR banks.owner_id<>owners.owner_id "
        "OR banks.generation<>owners.generation LIMIT 1",
    ).fetchone():
        raise QueueError("main_identity_mismatch")


def fingerprint(path):
    if path.is_symlink():
        return sha256(os.readlink(path))
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inventory(directory, *, exclude=()):
    return {str(path.relative_to(directory)): fingerprint(path) for path in directory.rglob("*")
            if not set(path.relative_to(directory).parts) & set(exclude)
            and (path.is_file() or path.is_symlink())}


def sync_path(path):
    if path.is_symlink() or (os.name == "nt" and path.is_dir()):
        return
    descriptor = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup(directory, control_directory, identities):
    destination = directory / BACKUP
    expected = {name + "/" + relative: digest
                for source, name in ((directory, "runtime"), (control_directory, "control"))
                for relative, digest in inventory(source, exclude=IGNORED).items()}
    if destination.exists():
        try:
            if destination.is_symlink():
                raise QueueError("migration_backup_incomplete")
            manifest = json.loads((destination / "manifest.json").read_text())
            actual = inventory(destination)
            actual.pop("manifest.json", None)
            transient = {folder + "/" + name + suffix
                         for folder, name in (("runtime", "runtime.sqlite3"), ("control", "control.sqlite3"))
                         for suffix in ("-wal", "-shm")}
            stored = manifest["files"]
            if (manifest["identity"] != identities or actual != stored
                    or {key: value for key, value in stored.items() if key not in transient}
                    != {key: value for key, value in expected.items() if key not in transient}):
                raise QueueError("migration_backup_incomplete")
            return destination
        except (OSError, ValueError, KeyError, TypeError):
            raise QueueError("migration_backup_incomplete") from None
    required = sum((directory if name.startswith("runtime/") else control_directory)
                   .joinpath(name.split("/", 1)[1]).stat().st_size for name in expected)
    if shutil.disk_usage(directory).free < required * 2 + 16 * 1024 * 1024:
        raise QueueError("migration_insufficient_space")
    building = directory / (BACKUP + ".building")
    if building.exists():
        if building.is_symlink():
            raise QueueError("migration_backup_incomplete")
        shutil.rmtree(building)
    building.mkdir(mode=0o700)
    for source, name in ((directory, "runtime"), (control_directory, "control")):
        shutil.copytree(source, building / name, symlinks=True,
                        ignore=lambda parent, names: set(names) & IGNORED)
    if inventory(building) != expected:
        raise QueueError("migration_backup_incomplete")
    (building / "manifest.json").write_text(canonical({"version": "0.5.0", "identity": identities, "files": expected}))
    for path in [*building.rglob("*"), building]:
        if os.name != "nt" and not path.is_symlink():
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        if not path.is_dir():
            sync_path(path)
    for path in sorted((path for path in building.rglob("*") if path.is_dir()), reverse=True):
        sync_path(path)
    sync_path(building)
    os.replace(building, destination)
    sync_path(directory)
    return destination


def copy_tables(target, source, excluded):
    available = tables(target)
    counts = {}
    for table in sorted(tables(source) - excluded):
        if table not in available:
            raise QueueError("migration_legacy_schema")
        columns = [row["name"] for row in source.execute(f'PRAGMA table_info("{table}")')]
        target_columns = {row["name"] for row in target.execute(f'PRAGMA table_info("{table}")')}
        if not set(columns) <= target_columns:
            raise QueueError("migration_legacy_schema")
        names = ",".join('"' + name + '"' for name in columns)
        placeholders = ",".join("?" for name in columns)
        target.executemany(f'INSERT INTO "{table}"({names}) VALUES ({placeholders})',
                           source.execute(f'SELECT {names} FROM "{table}"'))
        counts[table] = source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != counts[table]:
            raise QueueError("migration_count_mismatch")
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        for name, sequence in source.execute("SELECT name,seq FROM sqlite_sequence"):
            if name not in counts:
                continue
            target.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
            target.execute("INSERT INTO sqlite_sequence VALUES (?,?)", (name, sequence))
    return counts


def reconcile(database, runtime, deployment):
    control = Control(database)
    for previous in runtime.execute("SELECT * FROM scopes"):
        source = Scope(*json.loads(previous["identity_json"]))
        account = control.account(previous["connection_id"], previous["scope_id"])
        if not account or source.deployment_id != deployment or source.id != previous["scope_id"]:
            raise QueueError("migration_identity_mismatch")
        changed = any(previous[key] != account[key] for key in ("revision", "owner_id", "bank"))
        for task in database.execute("SELECT * FROM outbox WHERE connection_id=? AND scope_id=?",
                                     (previous["connection_id"], source.id)).fetchall():
            if task["bank"] != account["bank"] or task["generation"] != account["generation"]:
                erasure = database.execute("SELECT forget_id FROM erasures WHERE connection_id=? AND old_bank=?",
                                            (task["connection_id"], task["bank"])).fetchone()
                if not erasure:
                    raise QueueError("migration_retired_bank_unrecorded")
                refs = [("operation", task["operation_id"]), ("document", task["document_id"])]
                refs.extend(("operation", row[0]) for row in database.execute(
                    "SELECT operation_id FROM operations WHERE task_id=?", (task["id"],),
                ))
                control.add_refs(erasure["forget_id"], refs)
                database.execute("UPDATE outbox SET state='discarded',payload='',payload_bytes=0,"
                                 "retry_requested=0,last_error='' WHERE id=?", (task["id"],))
            elif task["accepted"]:
                control.record_request(task, account["owner_id"], accepted=True)
        database.execute("DELETE FROM window WHERE connection_id=? AND scope_id=? AND (generation<>? OR ?)",
                         (previous["connection_id"], source.id, account["generation"], changed))
    for task in database.execute("SELECT * FROM outbox WHERE state<>'discarded'").fetchall():
        record = control.request(task["operation_id"])
        if record:
            owner = control.account(task["connection_id"], task["scope_id"])
            control.record_request(task, owner["owner_id"])
            database.execute("UPDATE outbox SET accepted=MAX(accepted,?),cancel_intent=?,state=CASE WHEN ? THEN 'cancel_requested' "
                             "WHEN ? AND state='deferred' THEN 'unknown' WHEN ? AND state IN ('ready','submitting') THEN 'unknown' ELSE state END WHERE id=?",
                             (record["accepted"], record["cancel_intent"], record["cancel_intent"], record["accepted"],
                              record["dispatched"] or record["accepted"], task["id"]))
            intent = database.execute("SELECT pending FROM retry_intents WHERE operation_id=?", (task["operation_id"],)).fetchone()
            if intent:
                pending = intent["pending"] and task["state"] not in {"done", "discarded", "cancelled"} and not record["cancel_intent"]
                database.execute("UPDATE outbox SET retry_requested=?,next_at=CASE WHEN ? THEN 0 ELSE next_at END WHERE id=?",
                                 (int(bool(pending)), intent["pending"], task["id"]))
    database.execute("UPDATE outbox SET state='unknown' WHERE state='submitting'")
    database.execute("INSERT INTO history_prompts(task_id,text) SELECT task_id,text FROM window "
                     "WHERE task_id NOT IN (SELECT task_id FROM history_prompts)")
    for row in database.execute("SELECT key,value FROM settings WHERE key LIKE 'background:%'").fetchall():
        value = json.loads(row["value"])
        if value.get("format") == 1:
            value["format"] = 2
            value["pages"] = [{"page_id": value["page_id"], "mental_model_id": value["mental_model_id"]}] if value.get("page_id") or value.get("mental_model_id") else []
            database.execute("UPDATE settings SET value=? WHERE key=?", (canonical(value), row["key"]))


def cleanup_legacy(database, directory, control_directory):
    values = settings(database)
    if "migration_v050" not in values or values.get("legacy_cleanup_complete") == "1":
        return
    paths = ((directory / "runtime.sqlite3", "runtime/runtime.sqlite3"),
             (control_directory / "control.sqlite3", "control/control.sqlite3"))
    remaining = [(path, name) for path, name in paths if path.exists() or path.is_symlink()]
    if remaining:
        manifest = json.loads((directory / BACKUP / "manifest.json").read_text())
        if any(manifest["identity"].get(key) != values[key] for key in ("deployment_id", "control_id")):
            raise QueueError("migration_backup_incomplete")
        with ExitStack() as locks:
            lock_path = control_directory / "control.lock"
            if lock_path.is_symlink():
                raise QueueError("database_path_invalid")
            if lock_path.exists():
                lock = locks.enter_context(lock_path.open("a+b"))
                exclusive_lock(lock)
            with ExitStack() as originals:
                for path, name in remaining:
                    safe_path(path)
                    original = originals.enter_context(closing(sqlite3.connect(path, timeout=2)))
                    if original.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        raise QueueError("migration_legacy_locked")
                    original.execute("BEGIN EXCLUSIVE")
                    if fingerprint(path) != manifest["files"].get(name):
                        raise QueueError("migration_legacy_changed")
            for path, name in remaining:
                for suffix in ("", "-wal", "-shm"):
                    path.with_name(path.name + suffix).unlink(missing_ok=True)
                sync_path(path.parent)
    with database:
        database.execute("INSERT OR REPLACE INTO settings VALUES ('legacy_cleanup_complete','1')")


def open_main(directory, control_directory, create_schema):
    directory, control_directory = Path(directory), Path(control_directory)
    target = directory / "bridge.sqlite3"
    safe_path(target)
    if target.exists():
        database = connect(target)
        try:
            validate_main(database)
            cleanup_legacy(database, directory, control_directory)
            return database
        except BaseException:
            database.close()
            raise
    runtime_path, control_path = directory / "runtime.sqlite3", control_directory / "control.sqlite3"
    for path in (runtime_path, control_path):
        safe_path(path)
    legacy = runtime_path.exists() or control_path.exists()
    if legacy and not (runtime_path.is_file() and control_path.is_file()):
        raise QueueError("migration_legacy_database_missing")
    if not legacy and (directory / BACKUP).exists():
        raise QueueError("migration_main_database_missing")
    temporary = directory / "bridge.sqlite3.migrating"
    safe_path(temporary)
    with ExitStack() as resources:
        original_runtime = original_control = None
        if legacy:
            if (control_directory / "control.lock").is_symlink():
                raise QueueError("database_path_invalid")
            lock = resources.enter_context((control_directory / "control.lock").open("a+b"))
            exclusive_lock(lock)
            original_runtime = resources.enter_context(closing(sqlite3.connect(runtime_path, timeout=2)))
            original_control = resources.enter_context(closing(sqlite3.connect(control_path, timeout=2)))
            for database in (original_runtime, original_control):
                database.row_factory = sqlite3.Row
                if database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise QueueError("migration_legacy_locked")
                database.execute("BEGIN EXCLUSIVE")
                if not valid(database):
                    raise QueueError("migration_legacy_corrupt")
            if (original_runtime.execute("PRAGMA user_version").fetchone()[0] != 6
                    or original_control.execute("PRAGMA user_version").fetchone()[0] != 7
                    or not RUNTIME_TABLES <= tables(original_runtime) or not CONTROL_TABLES <= tables(original_control)):
                raise QueueError("migration_legacy_version")
            previous, authoritative = settings(original_runtime), settings(original_control)
            identities = {key: previous.get(key) for key in ("deployment_id", "control_id")}
            if not all(identities.values()) or any(authoritative.get(key) != value for key, value in identities.items()):
                raise QueueError("migration_identity_mismatch")
            if int(previous.get("control_revision", 0)) > int(authoritative["revision"]):
                raise QueueError("migration_control_rolled_back")
            backup(directory, control_directory, identities)
        for suffix in ("", "-wal", "-shm"):
            temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
        database = connect(temporary)
        try:
            create_schema(database)
            with database:
                database.execute("PRAGMA defer_foreign_keys=ON")
                if legacy:
                    counts = {**copy_tables(database, original_control, {"settings"}),
                              **copy_tables(database, original_runtime, {"settings", "scopes"})}
                    values = {**previous, **authoritative}
                    values.pop("control_revision", None)
                    database.executemany("INSERT INTO settings VALUES (?,?)", values.items())
                    reconcile(database, original_runtime, identities["deployment_id"])
                    database.execute("INSERT INTO settings VALUES ('migration_v050',?)", (canonical({"identity": identities, "counts": counts}),))
                else:
                    database.executemany("INSERT INTO settings VALUES (?,?)", (
                        ("deployment_id", str(uuid.uuid4())), ("control_id", str(uuid.uuid4())),
                        ("revision", "0"), ("accept_from", str(int(time.time()))),
                    ))
                database.execute("INSERT INTO settings VALUES ('storage_format',?)", (FORMAT,))
                database.execute(f"PRAGMA user_version={VERSION}")
                validate_main(database)
            if database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise QueueError("migration_main_locked")
        finally:
            database.close()
        sync_path(temporary)
        os.replace(temporary, target)
        sync_path(directory)
    database = connect(target)
    try:
        cleanup_legacy(database, directory, control_directory)
        return database
    except BaseException:
        database.close()
        raise
