import asyncio
import time
import sqlite3

from .client import HindsightError
from .control import QueueError


class BankStats:
    def __init__(self, plugin):
        self.plugin = plugin
        self.values = {}
        self.tasks = {}
        self.resources = None

    def resource_state(self):
        config = getattr(self.plugin, "config", {})
        return (self.plugin.outbox, self.plugin.client,
                tuple(config.get(key) for key in ("service_id", "api_url", "api_key", "service_address_revision")))

    def get(self, scope):
        plugin = self.plugin
        resources = self.resource_state()
        if resources != self.resources:
            self.values.clear()
            for task in self.tasks.values():
                task.cancel()
            self.tasks.clear()
            self.resources = resources
        queue, client = resources[:2]
        if (not queue or not client or not queue.can_recall(scope, manual=True)
                or getattr(client, "connection", {}).get("connected") is False):
            return None
        key = (queue.connection_id, scope["bank"], scope["generation"])
        value, expires = self.values.get(key, (None, 0))
        if expires <= time.monotonic():
            if key not in self.tasks:
                self.tasks[key] = asyncio.create_task(self.refresh(key, dict(scope), resources))
            return None
        return value

    async def refresh(self, key, scope, resources):
        queue, client = resources[:2]
        value = None
        task = asyncio.current_task()
        try:
            async with self.plugin.using_resources(), asyncio.timeout(4):
                result = await client.stats(scope["bank"])
                if all(type(result.get(field)) is int and result[field] >= 0 for field in ("total_documents", "total_nodes")):
                    value = {"documents": result["total_documents"], "memories": result["total_nodes"]}
        except (HindsightError, TimeoutError, QueueError, sqlite3.Error, OSError):
            pass
        finally:
            current = queue.scope_owner(scope["scope_id"]) if self.plugin.outbox is queue and queue.db else None
            if (self.resources == resources and self.resource_state() == resources and current
                    and current["bank"] == scope["bank"] and current["generation"] == scope["generation"]
                    and queue.can_recall(current, manual=True) and not task.cancelling()):
                self.values[key] = (value, time.monotonic() + 60)
            if self.tasks.get(key) is task:
                self.tasks.pop(key)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.values.clear()
