"""Create a business SQLite snapshot and an optional separate latest control copy."""

import argparse
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


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings"))


def identity(values):
    return values["deployment_id"], values["control_id"]


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
            with closing(connect(previous)) as saved, closing(connect(incoming)) as candidate:
                saved_settings = settings(saved)
                if identity(saved_settings) != identity(incoming_settings):
                    raise ValueError(f"latest_control_identity_mismatch: {latest}")
                saved_revision = int(saved_settings["revision"])
                if incoming_revision < saved_revision:
                    raise ValueError(f"latest_control_revision_regression: {latest}")
                if incoming_revision == saved_revision:
                    same_content = (
                        saved.execute("PRAGMA user_version").fetchone()
                        == candidate.execute("PRAGMA user_version").fetchone()
                        and sorted(saved.iterdump()) == sorted(candidate.iterdump())
                    )
                    if same_content:
                        return
                    archived = archive_control(incoming, latest)
                    raise ValueError(f"latest_control_revision_conflict: {latest}; {archived}")
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
