import argparse
from contextlib import closing
import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch


def pause(marker):
    marker.write_text("ready")
    time.sleep(120)
    raise TimeoutError("migration_probe_not_terminated")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--stage", choices=("seed", "backup", "import", "cutover", "open", "space"), required=True)
    parser.add_argument("--marker", required=True, type=Path)
    parser.add_argument("--connection-source", type=Path)
    arguments = parser.parse_args()
    sys.path.insert(0, str(arguments.source.parent))
    package = arguments.source.name
    outbox = importlib.import_module(package + ".core.outbox")
    storage = importlib.import_module(package + ".core.storage") if arguments.stage != "seed" else None
    connection_source = arguments.connection_source or arguments.runtime / "runtime.sqlite3"
    with closing(sqlite3.connect(connection_source.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        connection = dict(database.execute("SELECT * FROM connections").fetchone())
    options = dict(credential_fingerprint=connection["credential_fingerprint"],
                   address_revision=connection["address_revision"], control_directory=arguments.control)

    def open_queue():
        return outbox.Outbox(arguments.runtime, connection["id"], connection["url"], **options)

    if arguments.stage == "seed":
        with closing(open_queue()) as queue:
            scopes = {row["user_id"]: dict(row) for row in queue.list_scopes()}
            queue.sync_permissions([(row["platform_instance_id"], row["user_id"]) for row in scopes.values()])
            selected = {}
            for user in ("123001", "123002"):
                selected[user] = dict(queue.db.execute(
                    "SELECT * FROM outbox WHERE scope_id=? AND document_id LIKE 'uw1-%' "
                    "AND payload<>'' ORDER BY id DESC LIMIT 1", (scopes[user]["scope_id"],),
                ).fetchone())
            with queue.db:
                queue.db.execute("UPDATE outbox SET state='submitting',accepted=0 WHERE id=?", (selected["123001"]["id"],))
                queue.db.execute("UPDATE outbox SET state='failed',next_at=?,retry_requested=0 WHERE id=?",
                                 (time.time() + 3600, selected["123002"]["id"]))
            queue.request_retry(scopes["123002"]["scope_id"], journal_only=True)
            with queue.control.transaction():
                for row in queue.control.db.execute("SELECT key,value FROM settings WHERE key LIKE 'background:%'").fetchall():
                    value = json.loads(row["value"])
                    if value.get("enabled"):
                        value["format"] = 1
                        value.pop("pages", None)
                        queue.control.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), row["key"]))
            erasure = dict(queue.forget(scopes["123004"]["scope_id"], journal_only=True))
            result = {"tasks": selected, "scopes": scopes, "erasure": erasure}
            arguments.marker.write_text(json.dumps(result))
        return

    if arguments.stage == "backup":
        original = shutil.copytree

        def interrupted(*values, **options):
            result = original(*values, **options)
            pause(arguments.marker)
            return result

        guard = patch.object(shutil, "copytree", side_effect=interrupted)
    elif arguments.stage == "import":
        original = storage.copy_tables

        def interrupted(*values, **options):
            result = original(*values, **options)
            pause(arguments.marker)
            return result

        guard = patch.object(storage, "copy_tables", side_effect=interrupted)
    elif arguments.stage == "cutover":
        original = storage.sync_path

        def interrupted(path):
            result = original(path)
            if (arguments.runtime / "bridge.sqlite3").exists():
                pause(arguments.marker)
            return result

        guard = patch.object(storage, "sync_path", side_effect=interrupted)
    elif arguments.stage == "space":
        guard = patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(1, 1, 0))
    else:
        guard = patch.object(time, "sleep", wraps=time.sleep)
    with guard, closing(open_queue()) as queue:
        arguments.marker.write_text(json.dumps({"deployment_id": queue.deployment_id}))


if __name__ == "__main__":
    main()
