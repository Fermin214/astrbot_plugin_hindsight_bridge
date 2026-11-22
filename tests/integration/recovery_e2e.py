from live_e2e import ALICE, ROOT, database, jsonl, until
from upgrade_e2e import run_backup, start_host, stop_host


async def verify(acceptance):
    state = await acceptance.bootstrap()
    scope = next(row for row in state["scopes"] if row["user_id"] == str(ALICE))
    bank = scope["bank"]
    snapshot = ROOT / "single-main-before-forget"
    await acceptance.action(scope["scope_id"], "mode", mode="active")
    await acceptance.drained()
    await run_backup(acceptance, snapshot)
    original = database(snapshot / "bridge.sqlite3", "SELECT bank,generation,revision FROM owners WHERE owner_id=?", (scope["owner_id"],))
    assert original
    assert (snapshot / "references.sqlite3").exists()
    acceptance.passed("native_online_single_main_backup_includes_owner_queue_and_reference_history")
    prepared = await acceptance.action(scope["scope_id"], "forget_prepare")
    await acceptance.action(scope["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])

    async def erased():
        current = await acceptance.bootstrap()
        row = next(item for item in current["scopes"] if item["user_id"] == str(ALICE))
        cleanup = database(acceptance.runtime, "SELECT phase FROM erasures WHERE old_bank=?", (bank,))
        return row if row["bank"] != bank and cleanup and all(item["phase"] == "verified" for item in cleanup) else None

    current = await until(erased, timeout=180)
    assert not (await acceptance.memories(bank))["total"]
    retired = database(acceptance.runtime, "SELECT state,payload FROM outbox WHERE bank=?", (bank,))
    assert retired and all(row["state"] == "discarded" and not row["payload"] for row in retired)
    acceptance.passed("native_forget_rotates_generation_erases_remote_bank_and_discards_old_payloads")
    await stop_host(acceptance)
    await run_backup(acceptance, snapshot, restore=True)
    assert database(acceptance.runtime, "SELECT bank,generation,revision FROM owners WHERE owner_id=?", (scope["owner_id"],)) == original
    boundary = len(jsonl("retain-trace"))
    await start_host(acceptance)
    await acceptance.bot.chat(ALICE, "A message after restoring a snapshot whose memory bank was deleted.")

    async def protected():
        state = await acceptance.bootstrap()
        row = next(item for item in state["scopes"] if item["user_id"] == str(ALICE))
        return row if row["summary"].get("bank_missing") else None

    restored = await until(protected, timeout=90)
    assert restored["bank"] == bank and restored["generation"] == scope["generation"]
    async with acceptance.session.get(
        acceptance.settings["hindsight_url"] + "/v1/default/banks/" + bank + "/config",
        headers={"Authorization": "Bearer " + acceptance.settings["memory_key"]},
    ) as response:
        assert response.status == 404
    assert not any(bank in row["path"] for row in jsonl("retain-trace")[boundary:])
    acceptance.passed("native_whole_snapshot_restore_rolls_back_owner_and_deleted_bank_is_not_recreated")
    prepared = await acceptance.action(restored["scope_id"], "forget_prepare")
    await acceptance.action(restored["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])
    await until(erased, timeout=180)
    return current
