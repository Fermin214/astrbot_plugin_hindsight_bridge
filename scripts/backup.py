"""Create a business SQLite snapshot and an optional separate latest control copy."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from tempfile import TemporaryDirectory, mkdtemp
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.locking import exclusive_lock
from core.scope_settings import PREFIXES


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings"))


def identity(values):
    return values["deployment_id"], values["control_id"]


def preserves_rows(db, table, fixed, *, minimum="", no_additions=False, required="1", forward="1"):
    previous = {row[1] for row in db.execute(f"PRAGMA main.table_info({table})")}
    incoming = {row[1] for row in db.execute(f"PRAGMA incoming.table_info({table})")}
    if previous - incoming:
        return False
    if previous:
        matches = [
            f"new.{field} {operator} old.{field}"
            for fields, operator in ((fixed, "IS"), (minimum, ">="))
            for field in fields.split() if field in previous
        ]
        if db.execute(
            f"SELECT 1 FROM main.{table} AS old WHERE ({required}) AND NOT EXISTS ("
            f"SELECT 1 FROM incoming.{table} AS new WHERE {' AND '.join(matches)} "
            f"AND ({forward})) LIMIT 1"
        ).fetchone():
            return False
    if not no_additions or not incoming:
        return True
    old_count = db.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0] if previous else 0
    new_count = db.execute(f"SELECT count(*) FROM incoming.{table}").fetchone()[0]
    return old_count == new_count


def preserves_settings(previous, incoming, same_revision):
    migration_prefix = "authorization_migration:"
    for key, value in previous.items():
        if key not in incoming:
            return False
        if value == incoming[key]:
            continue
        if same_revision or key.startswith(migration_prefix):
            return False
        if key.startswith(PREFIXES):
            try:
                old_revision = json.loads(value)["revision"]
                new_revision = json.loads(incoming[key])["revision"]
            except (ValueError, TypeError, KeyError):
                return False
            if type(old_revision) is not int or type(new_revision) is not int or new_revision <= old_revision:
                return False
    return not same_revision or all(
        key.startswith(migration_prefix) and key != migration_prefix and incoming[key] == "1"
        for key in incoming.keys() - previous.keys()
    )


def control_conflict(previous, incoming, incoming_settings, same_revision):
    with closing(connect(previous)) as db:
        db.execute("ATTACH DATABASE ? AS incoming", (incoming.resolve().as_uri() + "?mode=ro",))
        old_format = db.execute("PRAGMA main.user_version").fetchone()[0]
        new_format = db.execute("PRAGMA incoming.user_version").fetchone()[0]
        if new_format < old_format or new_format > 7:
            return "control_format"
        if not preserves_settings(settings(db), incoming_settings, same_revision):
            return "settings"
        rules = (
            ("requests", "operation_id connection_id owner_id bank digest", "dispatched accepted cancel_intent", False),
            ("erasures", "forget_id connection_id owner_id principal_id old_bank old_generation "
                         "new_bank new_generation accept_from created_at", "cleanup_epoch", same_revision),
            ("erasure_refs", "forget_id kind ref", "", False),
            ("command_receipts", "connection_id source_id event_key digest occurred_at "
                                 "authorization_revision bank generation result", "", same_revision),
            ("accounts", "connection_id source_id identity_json platform_instance_id user_id owner_id", "", same_revision),
        )
        for table, fixed, minimum, no_additions in rules:
            if not preserves_rows(db, table, fixed, minimum=minimum, no_additions=no_additions):
                return table
        bank_progress = "1"
        if "state" in {row[1] for row in db.execute("PRAGMA main.table_info(banks)")}:
            bank_progress = (
                "new.state IN (old.state,'established')" if same_revision
                else "new.state<>'new' OR old.state='new'"
            )
        if not preserves_rows(
            db, "banks", "connection_id bank owner_id generation", no_additions=same_revision,
            forward=bank_progress,
        ):
            return "banks"
        owner_identity = "connection_id owner_id principal_id agent_id memory_space_id"
        owner_progress = "generation revision accept_from command_from command_received_from receive_from"
        if not preserves_rows(
            db, "owners", owner_identity + (" bank mode " + owner_progress if same_revision else ""),
            minimum="" if same_revision else owner_progress, no_additions=same_revision,
            forward="(new.generation>old.generation OR new.bank IS old.bank) AND "
                    "(new.revision>old.revision OR (new.generation IS old.generation "
                    "AND new.mode IS old.mode AND new.accept_from IS old.accept_from))",
        ):
            return "owners"
        grant_identity = "connection_id platform_instance_id user_id"
        if not preserves_rows(
            db, "authorizations", grant_identity + (" source_id enabled revision accept_from" if same_revision else ""),
            minimum="" if same_revision else "revision accept_from", no_additions=same_revision,
            forward="new.revision>old.revision OR (new.source_id IS old.source_id "
                    "AND new.enabled IS old.enabled AND new.accept_from IS old.accept_from)",
        ):
            return "authorizations"
        reflection_progress = "new.state IS old.state"
        if new_format >= 7:
            reflection_progress = (
                "(old.state='unknown' AND new.state='unknown' AND new.overridden_at IS NULL AND new.overridden_by='')"
            )
            if not same_revision:
                reflection_progress += (
                    " OR (old.state='unknown' AND new.state='overridden' "
                    "AND typeof(new.overridden_at) IN ('real','integer') "
                    "AND typeof(new.overridden_by)='text' AND trim(new.overridden_by)<>'')"
                )
            if old_format >= 7:
                reflection_progress += (
                    " OR (old.state='overridden' AND new.state='overridden' "
                    "AND new.overridden_at IS old.overridden_at AND new.overridden_by IS old.overridden_by)"
                )
        if not preserves_rows(
            db, "reflection_requests", "request_id connection_id owner_id bank generation created_at error_code",
            required="old.state IN ('unknown','overridden')", forward=reflection_progress,
        ):
            return "reflection_requests"
        if not preserves_rows(
            db, "retry_intents", "operation_id", no_additions=same_revision,
            forward="new.pending<=old.pending" if same_revision else "1",
        ):
            return "retry_intents"
        if same_revision and not preserves_rows(
            db, "maintenance", "connection_id bank started_at discard_pending", no_additions=True,
        ):
            return "maintenance"
    return None


def control_fingerprint(path):
    with closing(connect(path)) as db:
        digest = hashlib.sha256()
        digest.update(str(db.execute("PRAGMA user_version").fetchone()[0]).encode())
        for line in sorted(db.iterdump()):
            encoded = line.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()


def snapshot(source, path):
    path.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(path)) as target:
        source.backup(target)


def archive_control(source, latest):
    directory = Path(mkdtemp(prefix=latest.name + ".archive-", dir=latest.parent))
    archived = directory / "control.sqlite3"
    try:
        source.replace(archived)
    except OSError:
        directory.rmdir()
        raise
    return archived


def archive_conflict(source, latest, fingerprint):
    directory = latest.with_name(f"{latest.name}.archive-conflict-{fingerprint}")
    archived = directory / "control.sqlite3"
    if archived.exists():
        return archived if control_fingerprint(archived) == fingerprint else archive_control(source, latest)
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        source.replace(archived)
    except OSError:
        directory.rmdir()
        raise
    return archived


def publish_control(journal, latest, manifest):
    latest.parent.mkdir(parents=True, exist_ok=True)
    lock_path = latest.with_name(latest.name + ".lock")
    with lock_path.open("a+b") as lock, TemporaryDirectory(dir=latest.parent) as directory:
        try:
            exclusive_lock(lock)
        except OSError as exc:
            raise ValueError(f"latest_control_lock_failed: {lock_path}: {exc}") from exc
        incoming = Path(directory) / "incoming.sqlite3"
        snapshot(journal, incoming)
        with closing(connect(incoming)) as candidate:
            incoming_settings = settings(candidate)
        if identity(incoming_settings) != identity(manifest):
            raise ValueError("backup_identity_mismatch")
        incoming_revision = int(incoming_settings["revision"])
        if incoming_revision < manifest["last_seen_control_revision"]:
            raise ValueError("backup_control_older_than_runtime")
        if latest.exists():
            previous = Path(directory) / "previous.sqlite3"
            with closing(connect(latest)) as saved:
                snapshot(saved, previous)
            with closing(connect(previous)) as saved:
                saved_settings = settings(saved)
            if identity(saved_settings) != identity(incoming_settings):
                raise ValueError(f"latest_control_identity_mismatch: {latest}")
            saved_revision = int(saved_settings["revision"])
            if incoming_revision < saved_revision:
                raise ValueError(f"latest_control_revision_regression: {latest}")
            same_revision = incoming_revision == saved_revision
            fingerprint = None
            if same_revision:
                fingerprint = control_fingerprint(incoming)
                if control_fingerprint(previous) == fingerprint:
                    return
            if reason := control_conflict(previous, incoming, incoming_settings, same_revision):
                archived = archive_conflict(incoming, latest, fingerprint or control_fingerprint(incoming))
                code = "latest_control_revision_conflict" if same_revision else "latest_control_evidence_conflict"
                raise ValueError(f"{code} ({reason})\nlatest: {latest}\nconflict copy: {archived}")
            archive_control(previous, latest)
        incoming.replace(latest)


def backup(data_dir, control_dir, output, latest_control=None, backend_version=None):
    data_dir, control_dir, output = (
        Path(p).resolve() for p in (data_dir, control_dir, output)
    )
    if output.is_relative_to(data_dir) or output.is_relative_to(control_dir):
        raise ValueError("backup_output_must_be_separate")
    runtime, control = data_dir / "runtime.sqlite3", control_dir / "control.sqlite3"
    if not runtime.is_file() or not control.is_file():
        raise ValueError("matching_runtime_and_control_required")
    if latest_control:
        latest_control = Path(latest_control).resolve()
        if (
            latest_control.is_relative_to(output)
            or latest_control.is_relative_to(data_dir)
            or latest_control.is_relative_to(control_dir)
        ):
            raise ValueError("latest_control_copy_must_be_separate")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as directory:
        staged = Path(directory) / "business"
        staged.mkdir(mode=0o700)
        with closing(connect(runtime)) as business:
            snapshot(business, staged / "runtime.sqlite3")
        with closing(connect(staged / "runtime.sqlite3")) as captured:
            recorded = settings(captured)
            version = captured.execute("PRAGMA user_version").fetchone()[0]
        manifest = {
            "format": 2,
            "runtime_schema_version": version,
            "backend_version": backend_version,
            "deployment_id": recorded["deployment_id"],
            "control_id": recorded["control_id"],
            "last_seen_control_revision": int(recorded["control_revision"]),
            "created_at": datetime.now(UTC).isoformat(),
        }
        manifest_path = staged / "manifest.json"
        manifest_path.touch(mode=0o600)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with closing(connect(control)) as journal:
            if latest_control:
                publish_control(journal, latest_control, manifest)
            elif identity(settings(journal)) != identity(manifest):
                raise ValueError("backup_identity_mismatch")
        if output.exists():
            raise FileExistsError(output)
        staged.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--control-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--latest-control", type=Path)
    parser.add_argument("--backend-version", help="actual Hindsight version; omitted records null (unknown)")
    args = parser.parse_args()
    manifest = backup(
        args.data_dir,
        args.control_dir,
        args.output,
        args.latest_control,
        args.backend_version,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
