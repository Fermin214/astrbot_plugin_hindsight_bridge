"""Replay erasures, cancel every discovered operation, and check retired Banks."""

import asyncio
import time

from .client import HindsightError
from .control import QueueError


class Maintenance:
    def __init__(self, outbox, client):
        self.outbox, self.client = outbox, client
        self.locks = {}
        self.booted = False
        self.attempted_startup = False
        self.next_startup = 0.0

    def _scope(self, scope_id):
        scope = self.outbox.scope_owner(scope_id)
        if not scope or not scope["allowed"]:
            raise QueueError("scope_not_available")
        return scope

    def begin(self, scope_id, *, discard_pending=False):
        scope, queue = self._scope(scope_id), self.outbox
        if queue.recovering:
            raise QueueError("scope_not_available")
        record = queue.control.begin_maintenance(
            queue.connection_id, scope["owner_id"], discard_pending=discard_pending
        )
        queue.apply_control()
        # Remove history anchors at the boundary so a late assistant callback
        # cannot attach an earlier turn after maintenance ends.
        with queue.db:
            queue.db.execute(
                "DELETE FROM window WHERE connection_id=? AND scope_id IN "
                "(SELECT scope_id FROM scopes WHERE connection_id=? AND bank=?)",
                (queue.connection_id, queue.connection_id, scope["bank"]),
            )
        if record["discard_pending"]:
            rows = queue.db.execute(
                "SELECT * FROM outbox WHERE connection_id=? AND bank=? "
                "AND state NOT IN ('done','discarded')",
                (queue.connection_id, scope["bank"]),
            ).fetchall()
            for row in rows:
                if row["id"] in queue.in_flight:
                    continue
                request = queue.control.request(row["operation_id"])
                sent = bool(request and request["dispatched"])
                if (not sent and not row["accepted"]) or (
                    row["state"] == "failed" and row["accepted"]
                ):
                    queue.control.record_request(row, scope["owner_id"], cancel=True)
                    queue.update(row["id"], "discarded", retry_requested=0)
        return queue.control.maintenance(queue.connection_id, scope["bank"])

    async def status(self, scope_id):
        scope, queue = self._scope(scope_id), self.outbox
        record = queue.control.maintenance(queue.connection_id, scope["bank"])
        if record is None:
            return {"phase": "inactive", "ready": False}
        rows = queue.db.execute(
            "SELECT id,operation_id,state,accepted,cancel_intent FROM outbox "
            "WHERE connection_id=? AND bank=? AND state NOT IN ('done','discarded')",
            (queue.connection_id, scope["bank"]),
        ).fetchall()
        pending = [
            row for row in rows
            if row["state"] != "cancelled" or row["accepted"] or (
                (request := queue.control.request(row["operation_id"]))
                and (request["dispatched"] or request["accepted"])
            )
        ]
        bank_tasks = {
            row["id"] for row in queue.db.execute(
                "SELECT id FROM outbox WHERE connection_id=? AND bank=?",
                (queue.connection_id, scope["bank"]),
            )
        }
        inflight = bool(bank_tasks & queue.in_flight)
        remote_active = []
        operations = []
        if not pending and not inflight:
            async with asyncio.timeout(10):
                await self.client.ensure_auth()
                operations = await self.client.list_operations(scope["bank"])
                remote_active = [
                    op.id for op in operations
                    if op.status in {"pending", "processing", "cancelled"}
                ]
        ready = not pending and not inflight and not remote_active
        phase = "ready" if ready else "draining"
        with queue.control.db:
            queue.control.db.execute(
                "UPDATE maintenance SET phase=? WHERE connection_id=? AND bank=?",
                (phase, queue.connection_id, scope["bank"]),
            )
        return {
            **record, "phase": phase, "ready": ready,
            "pending": len(pending), "in_flight": inflight,
            "remote_active": len(remote_active),
            "remote_operations": len(operations),
            "bank_missing": queue.control.bank_state(queue.connection_id, scope["bank"]) == "missing",
        }

    async def resume(self, scope_id):
        scope, queue = self._scope(scope_id), self.outbox
        if queue.recovering:
            raise QueueError("scope_not_available")
        state = await self.status(scope_id)
        if not state["ready"]:
            raise QueueError("maintenance_pending")
        async with asyncio.timeout(10):
            exists = await self.client.bank_exists(scope["bank"])
            uncreated = (
                not exists
                and queue.control.bank_state(queue.connection_id, scope["bank"]) == "new"
                and not state["remote_operations"]
                and not queue.control.db.execute(
                    "SELECT 1 FROM requests WHERE connection_id=? AND bank=? "
                    "AND (dispatched=1 OR accepted=1) LIMIT 1",
                    (queue.connection_id, scope["bank"]),
                ).fetchone()
                and not await self.client.bank_list_contains(scope["bank"])
            )
        latest = self._scope(scope_id)
        if latest["bank"] != scope["bank"] or latest["revision"] != scope["revision"]:
            raise QueueError("scope_not_available")
        if not exists and not uncreated:
            queue.control.set_bank_state(queue.connection_id, scope["bank"], "missing")
            queue.apply_control()
            raise QueueError("bank_missing")
        if exists:
            queue.control.set_bank_state(queue.connection_id, scope["bank"], "established")
        queue.control.finish_maintenance(queue.connection_id, scope["owner_id"])
        queue.apply_control()
        return {"phase": "inactive", "ready": False}

    async def cancel_active(self, bank, operation):
        if operation.status not in {"pending", "processing"}:
            return
        try:
            await self.client.cancel(bank, operation.id)
        except HindsightError as exc:
            if exc.code == "http_409":
                await self.client.operation(bank, operation.id)
            elif exc.code != "http_404":
                raise

    def _operation_limit(self, fid):
        count = self.outbox.control.db.execute(
            "SELECT COUNT(*) FROM erasure_refs WHERE forget_id=? AND kind='operation'",
            (fid,),
        ).fetchone()[0]
        if count > 100000:
            raise QueueError("cleanup_operation_limit")

    async def _cancel_pass(self, fid, bank, pass_number, epoch):
        control, budget = self.outbox.control, 128
        while budget:
            self._operation_limit(fid)
            batch = control.cleanup_refs(fid, "operation", pass_number, limit=budget)
            if not batch:
                return True
            for oid in batch:
                operation = await self.client.operation(bank, oid)
                with control.db:
                    control.add_refs(
                        fid, (("operation", child) for child, _ in operation.children)
                    )
                await self.cancel_active(bank, operation)
                if not control.cleanup_ref_done(fid, "operation", oid, pass_number, epoch):
                    return False
                budget -= 1
        self._operation_limit(fid)
        if control.cleanup_refs(fid, "operation", pass_number, limit=1):
            control.cleanup_state(fid, "cleaning", delay=1, epoch=epoch)
            return False
        return True

    async def _bank_has_data(self, bank):
        return bool(
            await self.client.list_operations(bank)
            or await self.client.bank_exists(bank)
            or await self.client.bank_list_contains(bank)
        )

    def _retired_data_present(self, fid, epoch):
        control = self.outbox.control
        with control.db:
            if control.restart_cleanup(fid, epoch=epoch):
                control.cleanup_state(fid, "cleaning", "retired_data_present")
        return False

    async def _verify_refs(self, fid, bank, epoch):
        control = self.outbox.control
        for kind in ("operation", "document"):
            for ref in control.cleanup_refs(fid, kind, 3):
                exists = (
                    (await self.client.operation(bank, ref)).status != "not_found"
                    if kind == "operation" else await self.client.document_exists(bank, ref)
                )
                if exists:
                    return self._retired_data_present(fid, epoch)
                if not control.cleanup_ref_done(fid, kind, ref, 3, epoch):
                    return False
            if control.cleanup_refs(fid, kind, 3, limit=1):
                control.cleanup_state(fid, "cleaning", delay=1, epoch=epoch)
                return False
        return True

    async def erase(self, record):
        fid, bank = record["forget_id"], record["old_bank"]
        lock = self.locks.setdefault(fid, asyncio.Lock())
        async with lock:
            control = self.outbox.control
            progress = control.db.execute(
                "SELECT * FROM erasures WHERE forget_id=?", (fid,)
            ).fetchone()
            if progress["cleanup_step"] == 7:
                # Verified Banks are checked again, including references no longer listed remotely.
                with control.db:
                    control.restart_cleanup(fid)
                progress = control.db.execute(
                    "SELECT * FROM erasures WHERE forget_id=?", (fid,)
                ).fetchone()
            # Steps: list, cancel, relist, cancel, delete, Bank check, reference check, verified.
            step, epoch = progress["cleanup_step"], progress["cleanup_epoch"]
            control.cleanup_state(fid, "cleaning", epoch=epoch)
            try:
                async with asyncio.timeout(45):
                    # Each sweep and each absent reference survives timeout and process restart.
                    # A late submission or restored reference changes the epoch, invalidating old work.
                    while step < 7:
                        if step in (0, 2):
                            operations = await self.client.list_operations(bank)
                            with control.db:
                                control.add_refs(fid, (("operation", op.id) for op in operations))
                            self._operation_limit(fid)
                        elif step in (1, 3):
                            if not await self._cancel_pass(fid, bank, (step + 1) // 2, epoch):
                                return False
                        elif step == 4:
                            await self.client.delete_bank(bank)
                        elif step == 5:
                            if await self._bank_has_data(bank):
                                return self._retired_data_present(fid, epoch)
                        elif step == 6:
                            if not await self._verify_refs(fid, bank, epoch):
                                return False
                            # Verification can span calls; do not trust an earlier empty Bank listing.
                            if await self._bank_has_data(bank):
                                return self._retired_data_present(fid, epoch)
                            return control.cleanup_state(
                                fid, "verified", delay=86400, step=7, epoch=epoch
                            )
                        step += 1
                        if not control.cleanup_state(fid, "cleaning", step=step, epoch=epoch):
                            return False
            except (HindsightError, TimeoutError, QueueError) as exc:
                error = exc.code if isinstance(exc, HindsightError) else str(exc)
                if isinstance(exc, TimeoutError):
                    error = "cleanup_timeout"
                if isinstance(exc, HindsightError) and exc.authentication:
                    self.outbox.pause_connection(exc.code)
                control.cleanup_state(fid, "cleaning", error, epoch=epoch)
                return False

    async def check(self, *, force=False, owner_id=None):
        success = True
        for record in self.outbox.control.erasures(
            self.outbox.connection_id, owner_id=owner_id
        ):
            if force or record["next_at"] <= time.time():
                success = await self.erase(record) and success
            elif record["phase"] != "verified":
                success = False
        return success

    async def startup(self):
        if self.booted:
            return True
        if time.monotonic() < self.next_startup:
            return False
        self.next_startup = time.monotonic() + 60
        success = await self.check(force=not self.attempted_startup)
        self.attempted_startup = True
        if not success:
            return False
        # Apply the newest control records before allowing either recall or queue submission.
        self.outbox.apply_control()
        if self.outbox.recovering:
            rows = self.outbox.db.execute(
                "SELECT * FROM outbox WHERE connection_id=? AND accepted=1 AND "
                "state NOT IN ('discarded','done','cancelled')",
                (self.outbox.connection_id,),
            ).fetchall()
            for row in rows:
                root = await self.client.operation(row["bank"], row["operation_id"])
                if root.status == "not_found":
                    self.outbox.update(
                        row["id"],
                        "reconcile_required",
                        error="accepted_operation_missing",
                        retry_requested=0,
                    )
        self.outbox.recovering = False
        self.booted = True
        return True
