import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

from live_e2e import ALICE, BOB, NAME, ROOT, OneBot, database, jsonl, until
from gateway_e2e import IMAGE


CAROL = 123004


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def replace_directory(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for path in target.iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    shutil.copytree(source, target, dirs_exist_ok=True)


def install_files(archive, target):
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            relative = Path(entry.filename).relative_to(NAME)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid_package_member")
            destination = target / relative
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.read(entry))


async def probe(acceptance, stage, marker, *, interrupted=False):
    marker.unlink(missing_ok=True)
    source = ROOT / "upgrade-source/runtime/runtime.sqlite3"
    connection_arguments = ["--connection-source", str(source)] if source.exists() else []
    process = await asyncio.create_subprocess_exec(
        sys.executable, "/candidate-source/tests/integration/migration_probe.py",
        "--runtime", str(acceptance.data_directory), "--control", str(acceptance.control_database.parent),
        "--source", str(ROOT / "verification/data/plugins" / NAME), "--stage", stage, "--marker", str(marker),
        *connection_arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        if interrupted:
            await until(lambda: marker.exists() or process.returncode is not None, timeout=30, label="migration interruption point")
            assert marker.exists(), (await process.communicate())[1].decode()[-500:]
            process.kill()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        return process.returncode, stderr.decode()[-1000:]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def start_host(acceptance):
    await acceptance.control("start_astrbot")
    acceptance.bot = OneBot(acceptance.session, acceptance.settings)
    await acceptance.bot.connect()
    await acceptance.bot.chat(ALICE, (await acceptance.command_prefix()) + " status")
    await acceptance.usable()


async def stop_host(acceptance):
    await acceptance.bot.close()
    await acceptance.control("stop_astrbot")


async def settled(acceptance):
    await until(lambda: all(row["state"] in {"done", "discarded"} for row in acceptance.tasks()),
                timeout=180, label="native queued retry and accepted operation recovery")
    await acceptance.drained()


async def run_backup(acceptance, output, *, restore=False):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "/candidate-source/scripts/backup.py", "--data-dir", str(acceptance.data_directory),
        "--restore" if restore else "--output", str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    assert process.returncode == 0, stderr.decode()[-1000:]
    return json.loads(stdout)


async def verify(acceptance, page, bank, selected_page):
    await acceptance.config(allowed_accounts=[
        {"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(user)}
        for user in (ALICE, BOB, CAROL)
    ])
    await acceptance.confirm(CAROL)
    for user in (ALICE, BOB, CAROL):
        await acceptance.bot.chat(user, "HB-UPGRADE baseline history one Cedar-120")
        await acceptance.bot.chat(user, "HB-UPGRADE baseline history two Cedar-120")
    await acceptance.drained()
    rows = (await acceptance.bootstrap())["scopes"]
    alice = next(row for row in rows if row["user_id"] == str(ALICE))
    bob = next(row for row in rows if row["user_id"] == str(BOB))
    route = acceptance.prefix + "/scopes/" + alice["scope_id"]
    options = await acceptance.http("GET", route + "/references")
    await acceptance.http("POST", route + "/references", payload={
        **options["selection"], "prompt": "HB-UPGRADE custom memory prompt", "detail_limit": 7, "history_limit": 12,
        "scope_revision": options["scope_revision"], "config_revision": options["config_revision"],
    })
    await acceptance.control("hold_extraction")
    try:
        await acceptance.bot.chat(ALICE, "HB-UPGRADE accepted ack-lost material Cedar-120")
        await acceptance.bot.chat(BOB, "HB-UPGRADE retry backlog material Cedar-120")

        def accepted():
            return all(database(acceptance.runtime,
                "SELECT 1 FROM outbox WHERE scope_id=? AND accepted=1 AND payload<>'' AND document_id LIKE 'uw1-%'",
                (scope["scope_id"],),
            ) for scope in (alice, bob))

        await until(accepted, timeout=30, label="native accepted tasks before migration")
        await stop_host(acceptance)
        code, error = await probe(acceptance, "seed", ROOT / "migration-seed.json")
        assert code == 0, error
    finally:
        await acceptance.control("release_extraction")
    seed = json.loads((ROOT / "migration-seed.json").read_text())
    for task in seed["tasks"].values():
        await acceptance.completed(task["bank"], task["operation_id"])
    acceptance.record_baseline()
    source = ROOT / "upgrade-source"
    shutil.copytree(acceptance.data_directory, source / "runtime")
    control_directory = acceptance.control_database.parent
    shutil.copytree(control_directory, source / "control")
    original_history = database(source / "runtime/references.sqlite3", "SELECT id,binding FROM turns ORDER BY id")
    identities = database(source / "control/control.sqlite3", "SELECT * FROM authorizations ORDER BY user_id")
    control_id = database(source / "control/control.sqlite3", "SELECT value FROM settings WHERE key='control_id'")[0]["value"]
    bindings = {hashlib.sha256(json.dumps([
        control_id, grant["connection_id"], seed["scopes"][grant["user_id"]]["owner_id"],
        grant["source_id"], seed["scopes"][grant["user_id"]]["bank"],
        seed["scopes"][grant["user_id"]]["generation"], grant["revision"],
    ], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
                for grant in identities if grant["user_id"] in {str(ALICE), str(BOB)}}
    expected_history = [row for row in original_history if row["binding"] in bindings]
    assert len(expected_history) >= 4
    plugin = ROOT / "verification/data/plugins" / NAME
    marker = ROOT / "migration-probe.marker"

    def originals_intact():
        for path in (acceptance.data_directory / "runtime.sqlite3", control_directory / "control.sqlite3"):
            assert path.is_file()
            assert database(path, "PRAGMA quick_check")[0]["quick_check"] == "ok"
        assert not (acceptance.data_directory / "bridge.sqlite3").exists()

    install_files(ROOT / "candidate-upgrade.zip", plugin)
    for failure in ("missing_control", "missing_runtime", "mismatch", "space"):
        replace_directory(source / "runtime", acceptance.data_directory)
        replace_directory(source / "control", control_directory)
        if failure.startswith("missing"):
            path = (control_directory / "control.sqlite3" if failure == "missing_control"
                    else acceptance.data_directory / "runtime.sqlite3")
            path.rename(path.with_suffix(".saved"))
        if failure == "mismatch":
            with closing(sqlite3.connect(control_directory / "control.sqlite3")) as database_connection, database_connection:
                database_connection.execute("UPDATE settings SET value='foreign' WHERE key='control_id'")
        code, error = await probe(acceptance, "space" if failure == "space" else "open", marker)
        assert code != 0 and "migration_" in error, (failure, error)
        assert not (acceptance.data_directory / "bridge.sqlite3").exists()
    replace_directory(source / "runtime", acceptance.data_directory)
    replace_directory(source / "control", control_directory)
    originals_intact()
    acceptance.passed("native_v050_missing_either_database_mismatch_and_space_refuse_empty_initialization")

    code, error = await probe(acceptance, "backup", marker, interrupted=True)
    assert code < 0, error
    originals_intact()
    code, error = await probe(acceptance, "import", marker, interrupted=True)
    assert code < 0, error
    originals_intact()
    legacy_backup = acceptance.data_directory / "legacy-v0.5-backup"
    rollback_source = ROOT / "rollback-source"
    shutil.copytree(legacy_backup, rollback_source)
    replace_directory(rollback_source / "runtime", acceptance.data_directory)
    replace_directory(rollback_source / "control", control_directory)
    install_files(ROOT / "baseline-install.zip", plugin)
    await start_host(acceptance)
    await settled(acceptance)
    assert acceptance.runtime.name == "runtime.sqlite3"
    assert database(acceptance.control_database, "PRAGMA user_version")[0]["user_version"] == 7
    for user, task in seed["tasks"].items():
        restored = database(acceptance.runtime, "SELECT operation_id FROM outbox WHERE id=?", (task["id"],))[0]
        assert restored["operation_id"] == task["operation_id"]
    acceptance.passed("native_pre_activation_rollback_restores_complete_v050_runtime_and_control")
    await stop_host(acceptance)
    install_files(ROOT / "candidate-upgrade.zip", plugin)

    for stage in ("backup", "import", "cutover"):
        replace_directory(source / "runtime", acceptance.data_directory)
        replace_directory(source / "control", control_directory)
        before_trace = len(jsonl("retain-trace"))
        before_counts = {scope["bank"]: (await acceptance.native("GET", scope["bank"] + "/stats?refresh=true"))["total_documents"]
                         for scope in (alice, bob)}
        code, error = await probe(acceptance, stage, marker, interrupted=True)
        assert code < 0, error
        if stage != "cutover":
            originals_intact()
        await start_host(acceptance)
        await settled(acceptance)

        async def cleaned():
            rows = database(acceptance.runtime, "SELECT phase FROM erasures WHERE forget_id=?", (seed["erasure"]["forget_id"],))
            return rows and rows[0]["phase"] == "verified"

        await until(cleaned, timeout=120, label="migration resumes native erasure")
        assert acceptance.runtime.name == "bridge.sqlite3"
        assert not (acceptance.data_directory / "runtime.sqlite3").exists()
        assert not (control_directory / "control.sqlite3").exists()
        assert database(acceptance.runtime, "SELECT * FROM authorizations ORDER BY user_id") == identities
        assert database(acceptance.data_directory / "references.sqlite3", "SELECT id,binding FROM turns ORDER BY id") == expected_history
        for user, previous in seed["tasks"].items():
            task = database(acceptance.runtime, "SELECT * FROM outbox WHERE id=?", (previous["id"],))[0]
            assert task["state"] == "done" and task["operation_id"] == previous["operation_id"]
            scope = database(acceptance.runtime, "SELECT * FROM scopes WHERE scope_id=?", (previous["scope_id"],))[0]
            for key in ("owner_id", "principal_id", "bank", "generation", "scope_id"):
                assert scope[key] == seed["scopes"][user][key]
            after_count = (await acceptance.native("GET", scope["bank"] + "/stats?refresh=true"))["total_documents"]
            acceptance.report["measurements"].setdefault("migration_document_counts", []).append({
                "stage": stage, "user": user, "before": before_counts[scope["bank"]], "after": after_count,
            })
            assert after_count == before_counts[scope["bank"]], (stage, user, before_counts[scope["bank"]], after_count)
        assert not {item["body"]["operation_id"] for item in jsonl("retain-trace")[before_trace:]} & {
            task["operation_id"] for task in seed["tasks"].values()
        }
        background = await acceptance.http("GET", route + "/background")
        assert background["selection"]["pages"][0]["page_id"] == selected_page
        references = await acceptance.http("GET", route + "/references")
        assert references["selection"]["prompt"] == "HB-UPGRADE custom memory prompt"
        assert references["selection"]["detail_limit"] == 7 and references["selection"]["history_limit"] == 12
        acceptance.passed("native_" + stage + "_interruption_recovers_ids_history_cards_and_erasure_without_duplicate_documents")
        if stage != "cutover":
            await stop_host(acceptance)

    await verify_cards(acceptance, route)
    snapshot = ROOT / "single-main-snapshot"
    manifest = await run_backup(acceptance, snapshot)
    assert set(manifest["files"]) == {"bridge.sqlite3", "references.sqlite3"}
    await verify_locked_clear(acceptance, route, alice, bob)
    await stop_host(acceptance)
    await run_backup(acceptance, snapshot, restore=True)
    await start_host(acceptance)
    before_trace = len(jsonl("retain-trace"))
    await acceptance.bot.chat(ALICE, "HB-UPGRADE restored deleted-bank protection")

    async def bank_missing():
        return (await acceptance.scope())["summary"].get("bank_missing")

    await until(bank_missing, timeout=30, label="restored deleted Bank is paused")
    assert (await acceptance.scope())["bank"] == bank
    assert not any(bank in item["path"] for item in jsonl("retain-trace")[before_trace:])
    acceptance.passed("native_online_single_main_restore_rolls_back_state_and_deleted_bank_is_not_recreated")
    await page.goto(acceptance.dashboard + "/#/plugin-view/" + NAME + "/memory")
    await acceptance.dismiss_host_notice(page)


async def verify_cards(acceptance, route):
    reading = await acceptance.http("GET", route + "/reading")
    references = await acceptance.http("GET", route + "/references")
    payload = {"mode": "tools", "revision": reading["selection"]["revision"],
               "scope_revision": reading["scope_revision"], "config_revision": reading["config_revision"]}
    await acceptance.http("POST", route + "/reading", payload=payload)
    await acceptance.http("POST", route + "/references", payload={
        **references["selection"], "detail_limit": 8,
        "scope_revision": references["scope_revision"], "config_revision": references["config_revision"],
    })
    try:
        await acceptance.http("POST", route + "/reading", payload=payload)
    except RuntimeError:
        pass
    else:
        raise AssertionError("stale_same_card_was_accepted")
    acceptance.passed("native_cross_card_saves_are_independent_and_stale_same_card_is_rejected")


async def verify_locked_clear(acceptance, route, alice, bob):
    references = acceptance.data_directory / "references.sqlite3"
    old_reading = await acceptance.http("GET", route + "/reading")
    for user in (ALICE, BOB):
        await acceptance.bot.send(user, "", segments=[{"type": "image", "data": {"file": IMAGE}}])
    await until(lambda: len(database(references, "SELECT binding FROM skips")) >= 2, timeout=20, label="native skip rows before erasure")
    before = database(references, "SELECT id,binding FROM turns ORDER BY id")
    old_alice = {row["binding"] for row in before if row["id"] in {
        turn["id"] for turn in (await acceptance.http("GET", route + "/usage"))["turns"]
    }}
    old_bob = [row for row in before if row["binding"] not in old_alice]
    with closing(sqlite3.connect(references, timeout=1)) as locked:
        locked.execute("BEGIN EXCLUSIVE")
        prepared = await acceptance.action(alice["scope_id"], "forget_prepare")
        await acceptance.action(alice["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])
        await until(lambda: database(acceptance.runtime, "SELECT 1 FROM erasures WHERE old_bank=?", (alice["bank"],)), timeout=10)
        assert database(acceptance.runtime, "SELECT phase FROM erasures WHERE old_bank=?", (alice["bank"],))[0]["phase"] != "verified"
        try:
            await acceptance.http("POST", route + "/reading", payload={
                "mode": "automatic", "revision": old_reading["selection"]["revision"],
                "scope_revision": old_reading["scope_revision"], "config_revision": old_reading["config_revision"],
            })
        except RuntimeError:
            pass
        else:
            raise AssertionError("cleared_scope_accepted_old_page")
        before_tasks = len(acceptance.tasks())
        await acceptance.bot.chat(BOB, "HB-UPGRADE another user continues while history is locked")
        await until(lambda: len(acceptance.tasks()) > before_tasks, timeout=10, label="other user admits while history locked")
        locked.rollback()

    async def verified():
        return database(acceptance.runtime, "SELECT phase FROM erasures WHERE old_bank=?", (alice["bank"],))[0]["phase"] == "verified"

    await until(verified, timeout=120, label="unlocked history erasure is verified")
    after = database(references, "SELECT id,binding FROM turns ORDER BY id")
    assert all(row in after for row in old_bob)
    assert not old_alice & {row["binding"] for row in after}
    assert not database(references, "SELECT 1 FROM deliveries WHERE turn_id NOT IN (SELECT id FROM turns)")
    assert not old_alice & {row["binding"] for row in database(references, "SELECT binding FROM skips")}
    acceptance.passed("native_locked_history_accepts_clear_without_false_verified_keeps_other_user_and_deletes_all_record_tables")
