"""Create a business SQLite snapshot and an optional separate latest control copy."""

import argparse
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings"))


def backup(data_dir, control_dir, output, latest_control=None, backend_version="1.1.0"):
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
    with closing(connect(runtime)) as business, closing(connect(control)) as journal:
        local, authority = settings(business), settings(journal)
        if local.get("deployment_id") != authority.get("deployment_id") or local.get(
            "control_id"
        ) != authority.get("control_id"):
            raise ValueError("backup_identity_mismatch")
        output.mkdir(parents=True, exist_ok=False)
        with closing(sqlite3.connect(output / "runtime.sqlite3")) as target:
            business.backup(target)
        with closing(connect(output / "runtime.sqlite3")) as captured:
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
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        if latest_control:
            latest_control.parent.mkdir(parents=True, exist_ok=True)
            temporary = latest_control.with_name(uuid.uuid4().hex + ".sqlite3")
            try:
                with closing(sqlite3.connect(temporary)) as target:
                    journal.backup(target)
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                temporary.replace(latest_control)
            finally:
                temporary.unlink(missing_ok=True)
        if os.name != "nt":
            os.chmod(output, 0o700)
            for name in ("runtime.sqlite3", "manifest.json"):
                os.chmod(output / name, 0o600)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--control-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--latest-control", type=Path)
    parser.add_argument("--backend-version", default="1.1.0")
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
