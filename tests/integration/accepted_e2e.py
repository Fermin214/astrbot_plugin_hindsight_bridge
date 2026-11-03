"""Real accepted-operation recovery, cancellation and late writes after forget."""

import asyncio
import hashlib
import json

import aiohttp

from live_e2e import Acceptance, ALICE, NAME, OneBot, ROOT, database, jsonl, until


class Accepted(Acceptance):
    async def operation(self, bank, operation):
        async with self.session.get(self.settings["hindsight_url"] + "/v1/default/banks/" + bank + "/operations/" + operation,
                                    headers={"Authorization": "Bearer " + self.settings["memory_key"]}) as response:
            if response.status == 404:
                return {"status": "not_found"}
            assert response.status == 200
            return await response.json()

    async def held_task(self, text):
        before = max([row["id"] for row in self.tasks()] or [0])
        held_before = len(jsonl("held-extraction-trace"))
        await self.control("hold_extraction")
        await self.bot.chat(ALICE, text)
        await until(lambda: len(jsonl("held-extraction-trace")) > held_before, timeout=90)

        async def accepted():
            rows = database(self.runtime, "SELECT * FROM outbox WHERE id>? AND accepted=1", (before,))
            for row in rows:
                operation = await self.operation(row["bank"], row["operation_id"])
                active = operation["status"] == "processing" or any(
                    child.get("status") == "processing" for child in operation.get("child_operations", [])
                )
                if active:
                    (ROOT / "accepted-operation-evidence.jsonl").open("a").write(json.dumps({
                        "task_id": row["id"], "operation_id": row["operation_id"], "status": operation["status"],
                        "children": [{"operation_id": child["operation_id"], "status": child["status"]}
                                     for child in operation.get("child_operations", [])],
                    }) + "\n")
                    return row
        return await until(accepted, timeout=60)

    async def reconnect(self, action):
        await self.bot.close()
        await self.control(action)
        await asyncio.sleep(3)
        self.bot = OneBot(self.session, self.settings)
        await self.bot.connect()
        await self.usable()

    def assert_single_submission(self, row):
        traces = [trace for trace in jsonl("retain-trace") if trace["body"]["operation_id"] == row["operation_id"]]
        assert len(traces) == 1, "Accepted immutable operation was re-submitted"

    async def run(self):
        self.report.update(acceptance_scope="real accepted operations and retired-bank late-write cleanup",
                           not_covered=["real QQ account and platform delivery", "managed installation (separate suite)"])
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.bot = OneBot(session, self.settings)
            try:
                login = await self.http("POST", "/api/v1/auth/login", payload={"username": "astrbot", "password": self.settings["initial_password"]}, auth=False)
                self.token = login["token"]
                login = await self.http("POST", "/api/v1/auth/setup", payload={"username": self.settings["admin_name"], "password": self.settings["admin_password"],
                                                                                "confirm_password": self.settings["admin_password"]})
                self.token = login["token"]
                await self.bot.connect()
                core = ROOT / "verification/data/cmd_config.json"
                core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
                await self.install("candidate-install.zip")
                await self.config(api_url=self.settings["memory_url"], api_key=self.settings["memory_key"], service_id="accepted-service",
                                  allowed_accounts=[{"__template_key": "account", "platform_instance_id": "delivery-onebot", "user_id": str(ALICE)}])
                await self.usable()
                await self.bot.chat(ALICE, "Remember: my permanent baseline marker is Orchid-18. Reply briefly.")
                await self.drained()
                scope = (await self.bootstrap())["scopes"][0]

                row = await self.held_task("Remember: my host restart marker is Willow-27. Reply briefly.")
                await self.reconnect("restart_astrbot")
                assert database(self.runtime, "SELECT operation_id,accepted FROM outbox WHERE id=?", (row["id"],))[0] == {
                    "operation_id": row["operation_id"], "accepted": 1}
                await self.control("release_extraction")
                await self.drained()
                self.assert_single_submission(row)
                assert "Willow-27" in json.dumps(await self.memories(scope["bank"]))
                self.passed("real_host_restart_after_remote_acceptance_tracks_same_operation_once")

                row = await self.held_task("Remember: my backend restart marker is Birch-36. Reply briefly.")
                await self.control("stop_hindsight")
                await self.control("release_extraction")
                await self.control("start_hindsight")
                await self.backend_ready()
                await self.usable()
                await self.drained()
                self.assert_single_submission(row)
                assert "Birch-36" in json.dumps(await self.memories(scope["bank"]))
                self.passed("real_backend_stop_after_acceptance_recovers_without_duplicate_submission")

                row = await self.held_task("Remember: the cancellable work marker is Elm-45. Reply briefly.")
                answer = await self.bot.chat(ALICE, "/hindsight cancel " + str(row["id"]))
                assert str(row["id"]) in answer
                async def cancelled():
                    local = database(self.runtime, "SELECT state,cancel_intent FROM outbox WHERE id=?", (row["id"],))[0]
                    remote = await self.operation(row["bank"], row["operation_id"])
                    return remote if local["state"] == "cancelled" and local["cancel_intent"] == 1 and remote["status"] == "cancelled" else None
                remote = await until(cancelled, timeout=30)
                for child in remote.get("child_operations", []):
                    assert (await self.operation(row["bank"], child["operation_id"]))["status"] not in {"pending", "processing"}
                await self.control("release_extraction")
                await until(lambda: all(task["state"] in {"done", "cancelled"} for task in self.tasks()), timeout=120)
                self.assert_single_submission(row)
                assert "Orchid-18" in json.dumps(await self.memories(scope["bank"]))
                self.passed("actual_cancel_command_cancels_real_operation_tree_and_keeps_existing_memories")

                row = await self.held_task("Remember: the late-write marker after forget is Aspen-54. Reply briefly.")
                await self.control("hold_cleanup")
                prepared = await self.action(scope["scope_id"], "forget_prepare")
                await self.action(scope["scope_id"], "forget_confirm", confirmation=prepared["confirmation"])
                current = (await self.bootstrap())["scopes"][0]
                assert current["bank"] != scope["bank"] and current["mode"] == "paused"
                await until(lambda: bool(jsonl("held-cleanup-trace")), timeout=20)
                await self.control("release_extraction")
                async def late_written():
                    data = await self.memories(scope["bank"])
                    return data if "Aspen-54" in json.dumps(data) else None
                evidence = await until(late_written, timeout=40)
                (ROOT / "late-write-evidence.json").write_text(json.dumps({"retired_bank": scope["bank"], "total": evidence["total"],
                                                                          "marker_written_after_forget": True}, indent=2))
                assert not (await self.memories(current["bank"]))["total"]
                await self.control("release_cleanup")
                await self.usable()
                async def erased():
                    return not (await self.memories(scope["bank"]))["total"] and (await self.operation(scope["bank"], row["operation_id"]))["status"] == "not_found"
                await until(erased, timeout=90)
                retired = database(self.runtime, "SELECT state,payload FROM outbox WHERE bank=?", (scope["bank"],))
                assert retired and all(task["state"] == "discarded" and not task["payload"] for task in retired)
                await self.reconnect("restart_astrbot")
                await asyncio.sleep(3)
                assert await erased()
                assert hashlib.sha256(core.read_bytes()).hexdigest() == core_hash
                self.passed("real_memory_write_after_forget_is_erased_with_retired_operations_and_stays_erased_after_restart")
                self.report["complete"] = True
                self.save()
            finally:
                await self.control("release_extraction")
                await self.control("release_cleanup")
                await self.bot.close()


async def main():
    acceptance = Accepted()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
