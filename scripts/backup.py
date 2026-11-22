"""Online main-database snapshots and stopped-plugin full-state restoration."""

import argparse
from contextlib import closing
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.locking import exclusive_lock
from core.storage import VERSION, fingerprint, safe_path, settings, sync_path, valid, validate_main


def connect(path):
    safe_path(path)
    database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    database.row_factory = sqlite3.Row
    return database


def snapshot(source, path):
    path.touch(mode=0o600, exist_ok=False)
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("backup_timeout")

    with closing(sqlite3.connect(path)) as target:
        source.backup(target, pages=128, progress=progress)
    sync_path(path)


def backup(data_dir, output, backend_version=None):
    data_dir, output = Path(data_dir).resolve(), Path(output).resolve()
    if output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
        raise ValueError("backup_output_must_be_separate")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        staged.mkdir(mode=0o700)
        with closing(connect(data_dir / "bridge.sqlite3")) as source:
            snapshot(source, staged / "bridge.sqlite3")
        with closing(connect(staged / "bridge.sqlite3")) as database:
            validate_main(database)
            values = settings(database)
        files = {"bridge.sqlite3": fingerprint(staged / "bridge.sqlite3")}
        references = data_dir / "references.sqlite3"
        if references.exists():
            with closing(connect(references)) as source:
                snapshot(source, staged / "references.sqlite3")
            with closing(connect(staged / "references.sqlite3")) as database:
                if not valid(database):
                    raise ValueError("backup_references_invalid")
            files["references.sqlite3"] = fingerprint(staged / "references.sqlite3")
        manifest = {
            "format": 3, "main_schema_version": VERSION, "backend_version": backend_version,
            "deployment_id": values["deployment_id"], "control_id": values["control_id"],
            "created_at": datetime.now(UTC).isoformat(), "files": files,
        }
        manifest_path = staged / "manifest.json"
        manifest_path.touch(mode=0o600)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        sync_path(manifest_path)
        sync_path(staged)
        if output.exists():
            raise FileExistsError(output)
        staged.rename(output)
        sync_path(output.parent)
    return manifest


def restore(source_dir, data_dir):
    source_dir, data_dir = Path(source_dir).resolve(), Path(data_dir).resolve()
    if source_dir.is_relative_to(data_dir) or data_dir.is_relative_to(source_dir):
        raise ValueError("restore_source_must_be_separate")
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files", {})
    if (manifest.get("format") != 3 or manifest.get("main_schema_version") != VERSION
            or not isinstance(files, dict) or set(files) not in (
                {"bridge.sqlite3"}, {"bridge.sqlite3", "references.sqlite3"},
            )):
        raise ValueError("restore_manifest_invalid")
    for name, digest in files.items():
        safe_path(source_dir / name)
        if fingerprint(source_dir / name) != digest:
            raise ValueError("restore_snapshot_mismatch")
    with closing(connect(source_dir / "bridge.sqlite3")) as database:
        validate_main(database)
        values = settings(database)
        if any(values[key] != manifest.get(key) for key in ("deployment_id", "control_id")):
            raise ValueError("restore_identity_mismatch")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (data_dir / "worker.lock").open("a+b") as lock:
        exclusive_lock(lock)
        for name in ("bridge.sqlite3", "references.sqlite3"):
            safe_path(data_dir / name)
        if (data_dir / "runtime.sqlite3").exists():
            raise ValueError("restore_legacy_runtime_present")
        with TemporaryDirectory(dir=data_dir) as temporary:
            staged = Path(temporary)
            for name in files:
                with closing(connect(source_dir / name)) as source:
                    snapshot(source, staged / name)
                with closing(connect(staged / name)) as database:
                    if not valid(database):
                        raise ValueError("restore_snapshot_invalid")
            for name in ("references.sqlite3", "bridge.sqlite3"):
                for suffix in ("-wal", "-shm"):
                    (data_dir / (name + suffix)).unlink(missing_ok=True)
                if name in files:
                    os.replace(staged / name, data_dir / name)
                else:
                    (data_dir / name).unlink(missing_ok=True)
            sync_path(data_dir)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--output", type=Path)
    operation.add_argument("--restore", type=Path)
    parser.add_argument("--backend-version", help="actual Hindsight version; omitted records null (unknown)")
    arguments = parser.parse_args()
    manifest = (restore(arguments.restore, arguments.data_dir) if arguments.restore
                else backup(arguments.data_dir, arguments.output, arguments.backend_version))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
