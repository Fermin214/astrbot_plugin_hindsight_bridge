"""Official HTTP shapes, scoped reads, and all-or-error export behavior."""

import copy
import json
import unittest

import httpx

from plugins.astrbot_plugin_hindsight_bridge.catalog import (
    export_bytes,
    render_memory,
    render_page,
)
from plugins.astrbot_plugin_hindsight_bridge.client import (
    EXPORT_FORMAT,
    HindsightClient,
    HindsightError,
)

BANK = "abm-v1-owner"
TIME = "2026-10-24T08:00:00Z"


def fact(memory_id="m1", kind="world", document_id="session/1"):
    return {
        "id": memory_id,
        "text": "喜欢策略游戏",
        "context": "私聊",
        "fact_type": kind,
        "document_id": document_id,
        "date": TIME,
        "mentioned_at": TIME,
        "occurred_start": TIME,
        "occurred_end": None,
        "entities": "用户",
        "tags": ["private"],
        "metadata": {},
        "source_memory_ids": ["m1"] if kind == "observation" else [],
    }


class CatalogService:
    def __init__(self):
        self.rows = [fact(), fact("m2", "experience"), fact("m3", "observation", None)]
        self.requests = []
        self.mutate = lambda path, value: value
        self.fail_offset = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer service-secret"
        prefix = "/v1/default/banks/" + BANK
        assert request.url.path.startswith(prefix + "/"), request.url
        path = request.url.path[len(prefix) :]
        if path == "/memories/list":
            offset, limit = (
                int(request.url.params["offset"]),
                int(request.url.params["limit"]),
            )
            if offset == self.fail_offset:
                return httpx.Response(503, json={"detail": "service-secret"})
            # A service may return short pages: exhaustion is determined by total.
            value = {
                "items": copy.deepcopy(self.rows[offset : offset + min(2, limit)]),
                "total": len(self.rows),
                "limit": limit,
                "offset": offset,
            }
        elif path.startswith("/memories/"):
            value = copy.deepcopy(
                next(row for row in self.rows if row["id"] == path.rsplit("/", 1)[1])
            )
            value["type"] = value.pop("fact_type")
            value["entities"] = [{"id": "entity-1", "name": "用户"}]
        elif path == "/documents/session/1":
            value = {
                "id": "session/1",
                "bank_id": BANK,
                "original_text": "我喜欢策略游戏。",
                "content_hash": None,
                "created_at": TIME,
                "updated_at": TIME,
                "memory_unit_count": 2,
                "tags": ["private"],
                "document_metadata": {"api_key": "service-secret"},
                "retain_params": {"bank_id": "other-bank", "api_key": "service-secret"},
                "attachments": [{"url": "/banks/other-bank/secret"}],
            }
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json=self.mutate(path, value))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = CatalogService()
        self.client = HindsightClient(
            "https://hindsight.test",
            "service-secret",
            transport=httpx.MockTransport(self.service),
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def test_official_list_detail_document_shapes_and_bank_scope(self):
        page = await self.client.list_memories(BANK, offset=1, limit=1)
        self.assertEqual((page["total"], page["offset"], len(page["items"])), (3, 1, 1))
        self.assertEqual(page["items"][0]["type"], "experience")
        detail = await self.client.get_memory(BANK, "m3")
        self.assertEqual(detail["fact_type"], "observation")
        self.assertEqual(detail["source_memory_ids"], ["m1"])
        self.assertEqual(detail["mentioned_at"], TIME)
        document = await self.client.get_document(BANK, "session/1")
        self.assertEqual(document["original_text"], "我喜欢策略游戏。")
        self.assertNotIn("retain_params", document)
        self.assertNotIn("service-secret", json.dumps(document))

    async def test_export_reads_all_short_pages_details_and_unique_documents(self):
        payload = await self.client.export_memories(BANK)
        self.assertEqual(payload["format"], EXPORT_FORMAT)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(
            [row["type"] for row in payload["memories"]],
            ["world", "experience", "observation"],
        )
        self.assertEqual(len(payload["documents"]), 1)
        requests = self.service.requests
        pages = [r for r in requests if r.url.path.endswith("/memories/list")]
        self.assertEqual([int(r.url.params["offset"]) for r in pages], [0, 2, 0])
        self.assertEqual(sum("/documents/" in r.url.path for r in requests), 1)
        data = export_bytes(payload)
        self.assertIn("策略".encode(), data)
        self.assertEqual(json.loads(data)["total"], 3)
        self.assertNotIn(b"service-secret", data)
        self.assertNotIn(b"other-bank", data)

    async def test_limit_and_http_failure_never_return_partial_export(self):
        with self.assertRaisesRegex(HindsightError, "^export_limit_exceeded$"):
            await self.client.export_memories(BANK, max_items=2)
        self.assertEqual(len(self.service.requests), 1)
        self.service.fail_offset = 2
        with self.assertRaisesRegex(HindsightError, "^http_503$"):
            await self.client.export_memories(BANK)

    async def test_document_failure_never_returns_complete_export(self):
        original = self.service

        def service(request):
            if "/documents/" in request.url.path:
                return httpx.Response(404, json={"detail": "service-secret"})
            return original(request)

        client = HindsightClient(
            "https://hindsight.test",
            "service-secret",
            transport=httpx.MockTransport(service),
        )
        try:
            with self.assertRaisesRegex(HindsightError, "^http_404$"):
                await client.export_memories(BANK)
        finally:
            await client.close()

    async def test_malformed_page_shapes_fail_with_safe_codes(self):
        patches = (
            {"items": {}},
            {"total": True},
            {"offset": 9},
            {"limit": False},
            {"items": []},
            {"total": 0},
            {"items": [fact(), fact()]},
            {"items": [{**fact(), "fact_type": ["world"]}]},
            {"items": [{**fact(), "document_id": {"api_key": "service-secret"}}]},
            {"items": [{**fact(), "tags": "private"}]},
        )
        for patch in patches:
            with self.subTest(patch=patch):
                self.service.mutate = lambda path, value, patch=patch: {
                    **value,
                    **patch,
                }
                with self.assertRaises(HindsightError) as raised:
                    await self.client.list_memories(BANK)
                self.assertRegex(str(raised.exception), "^invalid_[a-z_]+$")
                self.assertNotIn("service-secret", str(raised.exception))

    async def test_wrong_identity_and_cross_bank_document_fail(self):
        self.service.mutate = lambda path, value: {**value, "id": "other"}
        with self.assertRaisesRegex(HindsightError, "^invalid_memory_response$"):
            await self.client.get_memory(BANK, "m1")
        self.service.mutate = lambda path, value: {**value, "bank_id": "other-bank"}
        with self.assertRaisesRegex(HindsightError, "^invalid_document_response$"):
            await self.client.get_document(BANK, "session/1")
        with self.assertRaisesRegex(HindsightError, "^invalid_memory_response$"):
            await self.client.get_memory(BANK, "m1")

    async def test_changed_total_and_repeated_page_fail_export(self):
        def changed(path, value):
            if path == "/memories/list" and value["offset"] == 2:
                value["total"] += 1
            return value

        self.service.mutate = changed
        with self.assertRaisesRegex(HindsightError, "^export_changed$"):
            await self.client.export_memories(BANK)
        self.service.mutate = lambda path, value: (
            {**value, "items": [fact()]}
            if path == "/memories/list" and value["offset"] == 2
            else value
        )
        with self.assertRaisesRegex(HindsightError, "^export_pagination_incomplete$"):
            await self.client.export_memories(BANK)

    async def test_missing_observation_source_rejects_export(self):
        self.service.rows[-1]["source_memory_ids"] = ["foreign-memory"]
        with self.assertRaisesRegex(HindsightError, "^export_source_missing$"):
            await self.client.export_memories(BANK)

    async def test_invalid_identifiers_and_arguments_make_no_requests(self):
        for bank in ("..", "other/bank", "", "bank\\name"):
            with self.assertRaisesRegex(HindsightError, "^invalid_identifier$"):
                await self.client.list_memories(bank)
        for offset, limit in ((True, 20), (-1, 20), (0, 0)):
            with self.assertRaisesRegex(HindsightError, "^invalid_pagination$"):
                await self.client.list_memories(BANK, offset=offset, limit=limit)
        with self.assertRaisesRegex(HindsightError, "^invalid_identifier$"):
            await self.client.get_document(BANK, "../other-bank/doc")
        self.assertEqual(self.service.requests, [])

    async def test_empty_bank_export_is_complete(self):
        self.service.rows = []
        payload = await self.client.export_memories(BANK)
        self.assertEqual(json.loads(export_bytes(payload))["memories"], [])

    async def test_logical_pages_fill_short_http_pages_without_skipping(self):
        self.service.rows = [fact(f"m{index}") for index in range(13)]
        first = await self.client.memory_page(BANK, page=1)
        second = await self.client.memory_page(BANK, page=2)
        self.assertEqual(
            [r["id"] for r in first["items"]], [f"m{i}" for i in range(10)]
        )
        self.assertEqual([r["id"] for r in second["items"]], ["m10", "m11", "m12"])
        offsets = [int(r.url.params["offset"]) for r in self.service.requests]
        self.assertEqual(offsets, [0, 2, 4, 6, 8, 10, 12])

    async def test_logical_page_detects_changed_total_during_fill(self):
        self.service.rows = [fact(f"m{index}") for index in range(13)]

        def change(path, value):
            if path == "/memories/list" and value["offset"] > 0:
                value["total"] += 1
            return value

        self.service.mutate = change
        with self.assertRaisesRegex(HindsightError, "^catalog_changed$"):
            await self.client.memory_page(BANK)

    async def test_renderers_show_chinese_types_time_and_sources_with_bounds(self):
        page = await self.client.list_memories(BANK)
        page["items"] *= 100
        page["items"][0]["text"] = "长文本\n" * 10000
        rendered = render_page(page, offset=20)
        self.assertLessEqual(len(rendered), 3500)
        for text in ("记忆ID：m1", "事实", "经历", TIME, "session/1", "下一起始位置"):
            self.assertIn(text, rendered)
        detail = await self.client.get_memory(BANK, "m3")
        detail["context"] = "语境" * 10000
        rendered = render_memory(detail)
        self.assertLessEqual(len(rendered), 6000)
        for text in ("整合观察", "来源记忆：m1", TIME):
            self.assertIn(text, rendered)

    async def test_serializer_rejects_incomplete_and_foreign_payloads(self):
        payload = await self.client.export_memories(BANK)
        incomplete = copy.deepcopy(payload)
        incomplete["memories"].pop()
        with self.assertRaisesRegex(HindsightError, "^invalid_export_payload$"):
            export_bytes(incomplete)
        payload["documents"][0]["bank_id"] = "other-bank"
        with self.assertRaisesRegex(HindsightError, "^invalid_document_response$"):
            export_bytes(payload)


if __name__ == "__main__":
    unittest.main()
