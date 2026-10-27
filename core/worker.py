"""Recover original operations and bound both remote and local concurrency."""

import asyncio
import random
import time
from collections import deque

from .client import HindsightClient, HindsightError, Operation
from .maintenance import Maintenance
from .outbox import Outbox, QueueError


class Worker:
    def __init__(
        self,
        outbox: Outbox,
        client: HindsightClient,
        logger,
        *,
        poll_interval=2,
        refresh_permissions=None,
    ):
        self.outbox, self.client, self.logger = outbox, client, logger
        self.poll_interval = poll_interval
        self.refresh_permissions = refresh_permissions
        self.wake = asyncio.Event()
        self.running = {}
        self.last_prune = 0.0
        self.maintenance = Maintenance(outbox, client)
        self.cleanup_task = None

    async def step(self) -> bool:
        if not await self.maintenance.startup():
            return False
        row = self.outbox.due()
        if row is None:
            return False
        await self.process(self.outbox.claim(row))
        return True

    async def tree(self, row, root: Operation) -> dict:
        """Save public parent/child relations before following another HTTP edge."""
        nodes = self.outbox.operations(row["id"])
        nodes[root.id] = {"parent_id": None, "status": root.status}
        pending, expanded = deque([root.id, *nodes]), set()
        while pending:
            operation_id = pending.popleft()
            if operation_id in expanded:
                continue
            operation = (
                root
                if operation_id == root.id
                else await self.client.operation(row["bank"], operation_id)
            )
            if operation.status == "not_found":
                if nodes[operation_id]["status"] != "completed":
                    raise QueueError("child_operation_missing")
                expanded.add(operation_id)
                continue
            nodes[operation_id]["status"] = operation.status
            expanded.add(operation_id)
            for child_id, status in operation.children:
                if (
                    child_id == root.id
                    or child_id == operation_id
                    or (
                        child_id in nodes
                        and nodes[child_id]["parent_id"] not in {None, operation_id}
                    )
                ):
                    raise QueueError("invalid_operation_tree")
                nodes[child_id] = {"parent_id": operation_id, "status": status}
            if len(nodes) > 256:
                raise QueueError("operation_tree_limit")
            self.outbox.save_operations(row["id"], nodes)
            pending.extend(key for key in nodes if key not in expanded)
        return nodes

    async def process(self, row):
        task_id = row["id"]
        self.outbox.in_flight.add(task_id)
        try:
            if not self.outbox.current(row, writing=False):
                return
            if self.outbox.connection_error():
                if row["state"] == "submitting":
                    self.outbox.update(task_id, "unknown", delay=self.poll_interval)
                return
            if self.outbox.get(task_id)["cancel_intent"]:
                await self.cancel_task(row)
                return
            payload = self.outbox.verify(row)
            root = await self.client.operation(row["bank"], row["operation_id"])
            if not self.outbox.current(row, writing=False):
                return
            if self.outbox.get(task_id)["cancel_intent"]:
                await self.cancel_task(row, root)
                return
            if root.status == "not_found":
                if row["accepted"] or row["state"] == "cancel_requested":
                    raise QueueError("accepted_operation_missing")
                if self.outbox.control.maintenance(row["connection_id"], row["bank"]):
                    raise QueueError("maintenance_unknown_operation")
                if not self.outbox.current(row):
                    self.outbox.update(task_id, "unknown", delay=self.poll_interval)
                    return
                first_creation = self.outbox.control.bank_state(
                    row["connection_id"], row["bank"]
                ) == "new"
                await self.client.ensure_auth()
                exists = await self.client.bank_exists(row["bank"])
                if not self.outbox.current(row):
                    self.outbox.update(task_id, "unknown", delay=self.poll_interval)
                    return
                if exists or first_creation:
                    self.outbox.control.set_bank_state(
                        row["connection_id"], row["bank"], "established"
                    )
                else:
                    raise HindsightError("bank_missing", permanent=True)
                if not exists:
                    # Only the creation PUT crosses the durable first-creation
                    # boundary; a failed read must leave a new Bank retryable.
                    await self.client.create_bank(row["bank"])
                if not self.outbox.current(row, writing=False):
                    self.outbox.control.retired_request_returned(
                        row["connection_id"], row["bank"]
                    )
                    return
                if not self.outbox.current(row) or self.outbox.connection_error():
                    self.outbox.update(task_id, "unknown", delay=self.poll_interval)
                    return
                self.outbox.update(task_id, "submitting")
                self.outbox.before_submit(row)
                await self.client.submit(row["bank"], payload)
                if not self.outbox.current(row, writing=False):
                    self.outbox.control.retired_request_returned(
                        row["connection_id"], row["bank"]
                    )
                    return
                self.outbox.update(
                    task_id,
                    "tracking",
                    accepted=1,
                    delay=self.poll_interval,
                    attempts=0,
                    retry_requested=0,
                )
                return
            self.outbox.update(task_id, "tracking", accepted=1)
            nodes = await self.tree(row, root)
            if not self.outbox.current(row, writing=False):
                return
            statuses = {node["status"] for node in nodes.values()}
            if statuses == {"completed"}:
                self.outbox.update(task_id, "done", attempts=0, retry_requested=0)
            elif statuses & {"pending", "processing"}:
                self.outbox.update(
                    task_id,
                    "cancel_requested" if "cancelled" in statuses else "tracking",
                    delay=self.poll_interval,
                    attempts=0,
                )
            elif "failed" in statuses:
                if row["retry_requested"] and self.outbox.current(row):
                    await self.retry_failed(row, root, nodes)
                else:
                    self.outbox.update(task_id, "failed", error="remote_failed")
            else:
                self.outbox.update(
                    task_id,
                    "cancelled",
                    error="remote_cancelled",
                    retry_requested=0,
                )
        except QueueError as exc:
            if str(exc) == "scope_changed":
                return
            self.outbox.update(
                task_id,
                "failed"
                if str(exc) == "payload_digest_conflict"
                else "reconcile_required",
                error=str(exc),
                retry_requested=0,
            )
        except HindsightError as exc:
            current = self.outbox.get(task_id)
            attempts = current["attempts"] + 1
            if exc.code == "bank_missing":
                self.outbox.control.set_bank_state(
                    row["connection_id"], row["bank"], "missing"
                )
                self.outbox.apply_control()
            if exc.authentication:
                self.outbox.pause_connection(exc.code)
            state = current["state"]
            if state == "submitting":
                state = "unknown"
            if exc.permanent and not exc.authentication:
                state = "failed"
            self.outbox.update(
                task_id,
                state,
                error=exc.code,
                attempts=attempts,
                delay=max(
                    exc.retry_after,
                    min(300, 2 ** min(attempts, 9) * random.uniform(1, 1.2)),
                ),
            )
            if attempts == 1 or exc.permanent:
                self.logger.warning("Hindsight queue: %s", exc.code)
        except Exception as exc:  # noqa: BLE001 -- preserve durable work on adapter failures
            self.outbox.update(
                task_id,
                "failed",
                error="local_" + type(exc).__name__,
                retry_requested=0,
            )
            self.logger.error("Hindsight worker: %s", type(exc).__name__)
        finally:
            self.outbox.in_flight.discard(task_id)

    async def retry_failed(self, row, root: Operation, nodes: dict):
        failed = {key for key, node in nodes.items() if node["status"] == "failed"}
        cancelled = any(node["status"] == "cancelled" for node in nodes.values())
        if not cancelled and root.status == "failed":
            targets = [root.id]
        else:
            parents = {node["parent_id"] for node in nodes.values()}
            targets = sorted(failed - parents)
        if not targets:
            raise QueueError("failed_operation_requires_reconciliation")
        # A lost retry acknowledgement is followed by reads, never an unbounded
        # loop of failed executions under the same explicit retry request.
        self.outbox.update(
            row["id"],
            "tracking",
            retry_requested=0,
            delay=self.poll_interval,
        )
        for operation_id in targets:
            if not self.outbox.current(row) or self.outbox.connection_error():
                return
            try:
                await self.client.retry(row["bank"], operation_id)
            except HindsightError as exc:
                if exc.code == "http_409":
                    self.outbox.update(
                        row["id"],
                        "tracking",
                        error="retry_state_changed",
                        delay=0,
                    )
                    return
                if exc.code == "http_404":
                    raise QueueError("retry_operation_missing") from None
                raise

    async def cancel_task(self, row, root=None):
        root = root or await self.client.operation(row["bank"], row["operation_id"])
        if not self.outbox.current(row, writing=False):
            return
        if root.status == "not_found":
            self.outbox.update(
                row["id"],
                "cancelled",
                delay=60,
                error="cancel_target_missing",
                retry_requested=0,
            )
            return
        nodes = await self.tree(row, root)
        for oid, node in nodes.items():
            if not self.outbox.current(row, writing=False):
                return
            await self.maintenance.cancel_active(
                row["bank"], Operation(oid, node["status"])
            )
        root = await self.client.operation(row["bank"], row["operation_id"])
        if root.status == "not_found":
            nodes = {}
        else:
            nodes = await self.tree(row, root)
        if not self.outbox.current(row, writing=False):
            return
        active = any(n["status"] in {"pending", "processing"} for n in nodes.values())
        self.outbox.update(
            row["id"],
            "cancel_requested" if active else "cancelled",
            accepted=int(root.status != "not_found"),
            delay=self.poll_interval if active else 60,
            error="cancellation_registered",
            retry_requested=0,
        )

    def start_due(self):
        while len(self.running) < 2:
            row = self.outbox.due(tuple(self.running))
            if row is None:
                break
            claimed = self.outbox.claim(row)
            self.running[row["owner_id"]] = asyncio.create_task(
                self.process(claimed),
                name=f"hindsight-retain-{row['id']}",
            )

    async def run(self) -> None:
        try:
            while True:
                for scope_id, task in list(self.running.items()):
                    if task.done():
                        del self.running[scope_id]
                        error = None if task.cancelled() else task.exception()
                        if error:
                            self.logger.error(
                                "Hindsight worker: %s", type(error).__name__
                            )
                if self.refresh_permissions:
                    self.refresh_permissions()
                if time.monotonic() - self.last_prune > 60:
                    with self.outbox.db:
                        self.outbox.prune_window()
                    self.last_prune = time.monotonic()
                self.wake.clear()
                try:
                    if await self.maintenance.startup():
                        self.start_due()
                        if self.cleanup_task is None or self.cleanup_task.done():
                            if self.cleanup_task and not self.cleanup_task.cancelled():
                                error = self.cleanup_task.exception()
                                if error:
                                    self.logger.error(
                                        "Hindsight cleanup: %s", type(error).__name__
                                    )
                            self.cleanup_task = asyncio.create_task(
                                self.maintenance.check()
                            )
                except (HindsightError, QueueError, OSError) as exc:
                    self.outbox.recovering = True
                    if isinstance(exc, HindsightError) and exc.authentication:
                        self.outbox.pause_connection(exc.code)
                try:
                    await asyncio.wait_for(
                        self.wake.wait(),
                        timeout=max(0.01, min(self.poll_interval, 0.5)),
                    )
                except TimeoutError:
                    pass
        finally:
            tasks = list(self.running.values())
            if self.cleanup_task:
                tasks.append(self.cleanup_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.running.clear()
