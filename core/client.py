"""Small adapter for the Hindsight 1.1/1.2 public HTTP API."""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .materials import token_count
from .identity import canonical, sha256

FACT_TYPES = {"world", "experience", "observation"}
RECALL_BUDGETS = ("low", "mid", "high")
BACKGROUND_MAX_BYTES = 256 * 1024


def native_page_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value):
        raise HindsightError("invalid_background_id", permanent=True)
    return value


class HindsightError(Exception):
    """Safe diagnostic labels only; never include response bodies or credentials."""

    def __init__(self, code: str, permanent: bool = False, *, retry_after=0):
        self.code = code
        self.permanent = permanent
        self.retry_after = retry_after
        super().__init__(code)

    @property
    def authentication(self):
        return self.code in {"http_401", "http_403", "server_auth_not_enforced"}


@dataclass(frozen=True)
class Operation:
    id: str
    status: str
    children: tuple[tuple[str, str], ...] = ()
    kind: str | None = None
    completed_at: str | None = None


def _operation_metadata(value):
    for field in ("operation_type", "task_type"):
        if value.get(field) is not None and (not isinstance(value[field], str) or not value[field]):
            raise HindsightError("invalid_operation_type")
    kind = value.get("operation_type") or value.get("task_type")
    if (
        value.get("operation_type") and value.get("task_type")
        and value["operation_type"] != value["task_type"]
    ):
        raise HindsightError("inconsistent_operation_type")
    completed_at = value.get("completed_at")
    if completed_at is not None:
        try:
            if not isinstance(completed_at, str) or datetime.fromisoformat(completed_at).tzinfo is None:
                raise ValueError
        except ValueError:
            raise HindsightError("invalid_operation_completion") from None
    return kind, completed_at


def _operation_children(value):
    raw = value.get("child_operations")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise HindsightError("invalid_operation_children")
    children = []
    for child in raw:
        if (
            not isinstance(child, dict)
            or not isinstance(child.get("operation_id"), str)
            or not child["operation_id"]
            or not isinstance(child.get("status"), str)
            or not child["status"]
        ):
            raise HindsightError("invalid_operation_children")
        children.append((child["operation_id"], child["status"]))
    return tuple(children)


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class HindsightClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: float = 15,
        *,
        transport=None,
        tokenizer="o200k_base",
        auth_verified=False,
    ):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not api_key.strip()
        ):
            raise ValueError("Invalid backend URL or missing API key")
        self.url = str(httpx.URL(url)).rstrip("/")
        self.api_key = api_key
        self.tokenizer = tokenizer
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )
        self.auth_until = 0.0
        self.auth_verified = self.connection_fingerprint(self.url, self.api_key) if auth_verified else ""
        self.auth_rejected = ""
        self.auth_lock = asyncio.Lock()
        self.connection = {"connected": None, "code": "", "checked_at": None}
        self.request_sequence = self.connection_sequence = 0
        self.page_metadata = {}

    def next_connection_sequence(self):
        self.request_sequence += 1
        return self.request_sequence

    def connection_result(self, connected, code="", *, sequence=None):
        sequence = self.next_connection_sequence() if sequence is None else sequence
        if sequence < self.connection_sequence:
            return
        self.connection_sequence = sequence
        self.connection = {"connected": connected, "code": code, "checked_at": time.time()}

    @staticmethod
    def bank_path(bank: str) -> str:
        return "/v1/default/banks/" + quote(bank, safe="")

    @staticmethod
    def connection_fingerprint(url, api_key):
        return sha256(canonical([str(httpx.URL(url)).rstrip("/"), api_key]))

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        key: str | None = None,
        timeout: float | None = None,
        _connection_sequence: int | None = None,
    ) -> dict:
        sequence = _connection_sequence
        if key is None and sequence is None:
            sequence = self.next_connection_sequence()
        credential = self.api_key if key is None else key
        headers = {"Authorization": "Bearer " + credential} if credential else {}
        options = {"timeout": httpx.Timeout(timeout, connect=min(3, timeout))} if timeout is not None else {}
        try:
            resp = await self.http.request(
                method, self.url + path, json=body, headers=headers, **options
            )
        except httpx.TimeoutException as exc:
            code = (
                "connect_timeout" if isinstance(exc, httpx.ConnectTimeout)
                else "read_timeout" if isinstance(exc, httpx.ReadTimeout)
                else "write_timeout" if isinstance(exc, httpx.WriteTimeout)
                else "pool_timeout" if isinstance(exc, httpx.PoolTimeout)
                else "timeout"
            )
            if key is None:
                self.connection_result(False, code, sequence=sequence)
            raise HindsightError(code) from None
        except httpx.HTTPError:
            if key is None:
                self.connection_result(False, "connection_error", sequence=sequence)
            raise HindsightError("connection_error") from None
        if not resp.is_success:
            if key is None and resp.status_code in {401, 403}:
                self.connection_result(False, f"http_{resp.status_code}", sequence=sequence)
            raise HindsightError(
                f"http_{resp.status_code}",
                permanent=400 <= resp.status_code < 500
                and resp.status_code not in {408, 429},
                retry_after=retry_after(resp.headers.get("Retry-After")),
            )
        try:
            value = resp.json()
        except ValueError:
            raise HindsightError("invalid_response") from None
        if not isinstance(value, dict):
            raise HindsightError("invalid_response")
        if key is None and time.monotonic() < self.auth_until:
            self.connection_result(True, sequence=sequence)
        return value

    async def read_bounded(
        self, path, *, max_bytes=BACKGROUND_MAX_BYTES, method="GET", body=None,
        expected=dict, timeout=None,
    ):
        """Bound decoded response bytes while streaming, not after buffering JSON."""
        sequence = self.next_connection_sequence()
        try:
            async with self.http.stream(
                method, self.url + path, json=body,
                headers={"Authorization": "Bearer " + self.api_key},
                **({"timeout": timeout} if timeout is not None else {}),
            ) as response:
                if not response.is_success:
                    if response.status_code in {401, 403}:
                        self.connection_result(False, f"http_{response.status_code}", sequence=sequence)
                    raise HindsightError(
                        f"http_{response.status_code}",
                        permanent=400 <= response.status_code < 500
                        and response.status_code not in {408, 429},
                        retry_after=retry_after(response.headers.get("Retry-After")),
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > max_bytes:
                        raise HindsightError("background_response_too_large")
                    data.extend(chunk)
            value = json.loads(data)
            if not isinstance(value, expected):
                raise ValueError
            if time.monotonic() < self.auth_until:
                self.connection_result(True, sequence=sequence)
            return value
        except (ValueError, RecursionError):
            raise HindsightError("invalid_background_response") from None
        except httpx.TimeoutException:
            self.connection_result(False, "background_timeout", sequence=sequence)
            raise HindsightError("background_timeout") from None
        except httpx.HTTPError:
            self.connection_result(False, "connection_error", sequence=sequence)
            raise HindsightError("connection_error") from None

    async def reflect(self, bank, query, *, max_tokens, settled):
        sequence = self.next_connection_sequence()
        try:
            async with self.http.stream(
                "POST", self.url + self.bank_path(bank) + "/reflect",
                json={"query": query, "budget": "low", "max_tokens": max_tokens, "include": {"facts": {}}},
                headers={"Authorization": "Bearer " + self.api_key},
                timeout=httpx.Timeout(120, connect=3),
            ) as response:
                if not response.is_success:
                    if response.status_code in {401, 403}:
                        self.connection_result(False, f"http_{response.status_code}", sequence=sequence)
                    raise HindsightError(f"http_{response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > BACKGROUND_MAX_BYTES:
                        raise HindsightError("reflect_response_too_large")
                    data.extend(chunk)
            value = json.loads(data)
            if (
                not isinstance(value, dict) or not isinstance(value.get("text"), str)
                or value.get("based_on") is not None and not isinstance(value["based_on"], dict)
            ):
                raise ValueError
            settled()
            if time.monotonic() < self.auth_until:
                self.connection_result(True, sequence=sequence)
            return value
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError):
            settled()
            self.connection_result(False, "connection_error", sequence=sequence)
            raise HindsightError("connection_error") from None
        except httpx.TimeoutException:
            self.connection_result(False, "reflect_transport_timeout", sequence=sequence)
            raise HindsightError("reflect_transport_timeout") from None
        except httpx.HTTPError:
            self.connection_result(False, "connection_error", sequence=sequence)
            raise HindsightError("connection_error") from None
        except (ValueError, RecursionError):
            raise HindsightError("invalid_reflect_response") from None

    async def _background_read(self, bank, suffix, *, expected=dict):
        try:
            return await self.read_bounded(self.bank_path(bank) + suffix, expected=expected)
        except HindsightError as exc:
            if exc.code != "http_404":
                raise
            try:
                await self.read_bounded(self.bank_path(bank) + "/config")
            except HindsightError as bank_error:
                if bank_error.code == "http_404":
                    raise HindsightError("bank_missing") from None
                raise
            raise HindsightError("background_missing") from None

    async def knowledge_pages(self, bank):
        value = await self._background_read(bank, "/knowledge-base/tree")
        if not isinstance(value.get("roots"), list):
            raise HindsightError("invalid_background_response")
        stack = [(node, ()) for node in reversed(value["roots"])]
        pages, seen, models = [], set(), set()
        while stack:
            node, parents = stack.pop()
            if not isinstance(node, dict):
                raise HindsightError("invalid_background_response")
            identity = native_page_id(node.get("id"))
            name, kind, children = node.get("name"), node.get("kind"), node.get("children")
            if (
                identity in seen or not isinstance(name, str) or not name.strip()
                or not isinstance(kind, str) or kind not in {"folder", "page"} or not isinstance(children, list)
                or (kind == "page" and children)
            ):
                raise HindsightError("invalid_background_response")
            seen.add(identity)
            if kind == "page":
                model = native_page_id(node.get("mental_model_id"))
                if model in models:
                    raise HindsightError("invalid_background_response")
                models.add(model)
                pages.append({
                    "page_id": identity, "mental_model_id": model,
                    "name": name, "path": " / ".join((*parents, name)),
                    "index_stale": node["is_stale"] if type(node.get("is_stale")) is bool else None,
                })
            stack.extend((child, (*parents, name)) for child in reversed(children))
        self.page_metadata = {key: value for key, value in self.page_metadata.items() if key[:3] != (self.url, self.api_key, bank)}
        expires = time.monotonic() + 30
        for page in pages:
            metadata = {"name": page["name"], "path": page["path"]}
            if type(page["index_stale"]) is bool:
                metadata["is_stale"] = page["index_stale"]
            self.page_metadata[(self.url, self.api_key, bank, page["page_id"], page["mental_model_id"])] = (expires, metadata)
        return pages

    def cached_page_metadata(self, bank, page_id, model_id):
        key = (self.url, self.api_key, bank, page_id, model_id)
        cached = self.page_metadata.get(key)
        if cached is None:
            return {}
        expires, metadata = cached
        if expires <= time.monotonic():
            self.page_metadata.pop(key, None)
            return {}
        return dict(metadata)

    async def knowledge_page(self, bank, page_id):
        value = await self._background_read(
            bank, "/knowledge-base/pages/" + quote(native_page_id(page_id), safe=""),
        )
        if (
            value.get("id") != page_id or not isinstance(value.get("name"), str)
            or (value.get("body") is not None and not isinstance(value["body"], str))
        ):
            raise HindsightError("invalid_background_response")
        result = {"id": page_id, "name": value["name"], "body": value.get("body") or ""}
        for key in ("timestamp", "description"):
            if key in value:
                if value[key] is not None and not isinstance(value[key], str):
                    raise HindsightError("invalid_background_response")
                result[key] = value[key]
        return result

    async def background_page(self, bank, model_id):
        value = await self._background_read(
            bank, "/mental-models/" + quote(native_page_id(model_id), safe="") + "?detail=content",
        )
        if (
            value.get("id") != model_id or value.get("bank_id") != bank
            or value.get("is_knowledge_page") is not True
            or (value.get("content") is not None and not isinstance(value["content"], str))
        ):
            raise HindsightError("invalid_background_response")
        result = {"id": model_id, "content": value.get("content") or ""}
        for key in ("last_refreshed_at", "last_memory_seen_at", "last_refresh_failed_at"):
            stamp = value.get(key)
            if stamp is not None:
                try:
                    if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is None:
                        raise ValueError
                except ValueError:
                    raise HindsightError("invalid_background_response") from None
            result[key] = stamp
        return result

    async def ensure_auth(self, force: bool = False, *, timeout: float | None = None,
                          _connection_sequence: int | None = None) -> None:
        fingerprint = self.connection_fingerprint(self.url, self.api_key)
        if self.auth_rejected == fingerprint:
            raise HindsightError("server_auth_not_enforced")
        if self.auth_verified and self.auth_verified != fingerprint:
            self.auth_until = 0.0
        if not force and time.monotonic() < self.auth_until:
            return
        previous_auth = self.auth_until
        async with self.auth_lock:
            if time.monotonic() < self.auth_until and (
                not force or self.auth_until != previous_auth
            ):
                return
            path = "/v1/default/banks"
            sequence = _connection_sequence if _connection_sequence is not None else self.next_connection_sequence()
            try:
                await self.request("GET", path, timeout=timeout, _connection_sequence=sequence)
                if self.auth_verified != fingerprint:
                    try:
                        await self.request("GET", path, key="invalid-" + uuid.uuid4().hex, timeout=timeout)
                    except HindsightError as exc:
                        if exc.code not in {"http_401", "http_403"}:
                            raise
                    else:
                        self.auth_rejected = fingerprint
                        raise HindsightError("server_auth_not_enforced")
                    self.auth_verified = fingerprint
            except HindsightError as exc:
                self.auth_until = 0.0
                self.connection_result(False, exc.code, sequence=sequence)
                raise
            self.auth_until = time.monotonic() + 60
            self.connection_result(True, sequence=sequence)

    async def check_connection(self, *, timeout=10, force=False):
        sequence = self.next_connection_sequence()
        try:
            async with asyncio.timeout(timeout):
                await self.ensure_auth(force=force, timeout=timeout, _connection_sequence=sequence)
        except TimeoutError:
            self.connection_result(False, "auth_timeout", sequence=sequence)
            raise HindsightError("auth_timeout") from None

    async def create_bank(self, bank: str) -> None:
        if not bank.startswith("abm-v1-"):
            raise HindsightError("unowned_bank", permanent=True)
        await self.request("PUT", self.bank_path(bank), body={"name": "AstrBot Memory"})

    async def recall(
        self,
        bank: str,
        query: str,
        max_tokens: int = 1200,
        *,
        timestamp: str,
        timeout: float | None = None,
        bounded: bool = False,
        types: list[str] | None = None,
        budget: str = "mid",
    ) -> list[dict]:
        if budget not in RECALL_BUDGETS:
            raise HindsightError("invalid_recall_budget", permanent=True)
        if types == []:
            return []
        if not query.strip() or token_count(query, self.tokenizer) > 400:
            raise HindsightError("invalid_query_budget", permanent=True)
        await self.ensure_auth(timeout=timeout)
        try:
            options = dict(
                body={
                    "query": query, "budget": budget, "max_tokens": max_tokens,
                    "query_timestamp": timestamp,
                    "types": types if types is not None else ["world", "experience", "observation"],
                    "include": {"source_facts": {"max_tokens": 2048, "max_tokens_per_observation": 256}},
                }, timeout=timeout,
            )
            result = await self.read_bounded(
                self.bank_path(bank) + "/memories/recall", method="POST", **options,
            ) if bounded else await self.request(
                "POST",
                self.bank_path(bank) + "/memories/recall",
                **options,
            )
        except HindsightError as exc:
            if exc.code == "http_404":
                if bounded:
                    raise HindsightError("bank_missing") from None
                return []
            raise
        rows = result.get("results")
        if not isinstance(rows, list):
            raise HindsightError("invalid_recall_response")
        if types is not None:
            from .reading import attributed_records

            return attributed_records(rows, bank, result.get("source_facts"), strict=bounded)
        return rows

    async def submit(self, bank: str, payload: dict) -> None:
        await self.ensure_auth()
        result = await self.request(
            "POST", self.bank_path(bank) + "/memories", body=payload
        )
        if (
            result.get("success") is not True
            or result.get("async") is not True
            or result.get("operation_id") != payload["operation_id"]
        ):
            # Keep the original ID, even if the server acknowledged unexpectedly.
            raise HindsightError("invalid_retain_ack")

    async def operation(self, bank: str, operation_id: str) -> Operation:
        await self.ensure_auth()
        try:
            result = await self.request(
                "GET",
                self.bank_path(bank) + "/operations/" + quote(operation_id, safe=""),
            )
        except HindsightError as exc:
            if exc.code == "http_404":
                return Operation(operation_id, "not_found")
            raise
        if (
            result.get("operation_id") != operation_id
            or (result.get("id") is not None and result["id"] != operation_id)
            or result.get("bank_id", bank) != bank
        ):
            raise HindsightError("invalid_operation_response")
        status = result.get("status")
        if not isinstance(status, str) or not status:
            raise HindsightError("invalid_operation_status")
        children = _operation_children(result)
        kind, completed_at = _operation_metadata(result)
        return Operation(operation_id, status, children, kind, completed_at)

    async def retry(self, bank: str, operation_id: str, *, before_send=None) -> None:
        await self.ensure_auth()
        # Source checks must follow authentication I/O, including cache renewal.
        if before_send is not None:
            await before_send()
        await self.request(
            "POST",
            self.bank_path(bank)
            + "/operations/"
            + quote(operation_id, safe="")
            + "/retry",
        )

    async def cancel(self, bank: str, operation_id: str) -> None:
        await self.ensure_auth()
        await self.request(
            "DELETE",
            self.bank_path(bank) + "/operations/" + quote(operation_id, safe=""),
        )

    async def bank_exists(self, bank: str) -> bool:
        await self.ensure_auth()
        try:
            await self.request("GET", self.bank_path(bank) + "/config")
        except HindsightError as exc:
            if exc.code == "http_404":
                return False
            raise
        return True

    async def bank_list_contains(self, bank: str) -> bool:
        await self.ensure_auth()
        offset = 0
        while True:
            result = await self.request(
                "GET",
                "/v1/default/banks?"
                + urlencode({"q": bank, "limit": 100, "offset": offset}),
            )
            rows = result.get("banks")
            if not isinstance(rows, list):
                raise HindsightError("invalid_bank_list")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("bank_id"), str):
                    raise HindsightError("invalid_bank_list")
                if row["bank_id"] == bank:
                    return True
            if len(rows) < 100:
                return False
            offset += len(rows)
            if offset > 100000:
                raise HindsightError("bank_list_limit")

    async def list_operations(self, bank: str) -> list[Operation]:
        await self.ensure_auth()
        offset, operations = 0, {}
        while True:
            try:
                result = await self.request(
                    "GET",
                    self.bank_path(bank)
                    + "/operations?"
                    + urlencode(
                        {"limit": 100, "offset": offset, "exclude_parents": "false"}
                    ),
                )
            except HindsightError as exc:
                if exc.code == "http_404":
                    return []
                raise
            rows, total = result.get("operations"), result.get("total")
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise HindsightError("invalid_operations_list")
            for row in rows:
                oid = (
                    (row.get("id") or row.get("operation_id"))
                    if isinstance(row, dict)
                    else None
                )
                if (
                    not isinstance(row, dict)
                    or not isinstance(oid, str)
                    or not oid
                    or not isinstance(row.get("status"), str)
                    or not row["status"]
                ):
                    raise HindsightError("invalid_operations_list")
                if (
                    row.get("operation_id")
                    and row.get("id")
                    and row["operation_id"] != row["id"]
                ):
                    raise HindsightError("inconsistent_operation_identity")
                if row.get("bank_id", bank) != bank:
                    raise HindsightError("inconsistent_operation_bank")
                kind, completed_at = _operation_metadata(row)
                operations[oid] = Operation(oid, row["status"], _operation_children(row), kind, completed_at)
            offset += len(rows)
            if offset >= total:
                return list(operations.values())
            if not rows or offset > 100000:
                raise HindsightError("operations_pagination_incomplete")

    async def document_exists(self, bank: str, document_id: str) -> bool:
        await self.ensure_auth()
        try:
            await self.request(
                "GET",
                self.bank_path(bank) + "/documents/" + quote(document_id, safe=""),
            )
        except HindsightError as exc:
            if exc.code == "http_404":
                return False
            raise
        return True

    async def delete_bank(self, bank: str) -> None:
        await self.ensure_auth()
        if not bank.startswith("abm-v1-"):
            raise HindsightError("unowned_bank", permanent=True)
        try:
            result = await self.request("DELETE", self.bank_path(bank))
        except HindsightError as exc:
            if exc.code == "http_404":
                return
            raise
        if result.get("success") is not True:
            raise HindsightError("invalid_delete_response")

    async def stats(self, bank: str) -> dict:
        await self.ensure_auth()
        try:
            result = await self.request("GET", self.bank_path(bank) + "/stats")
        except HindsightError as exc:
            if exc.code == "http_404":
                return {}
            raise
        return {
            key: result.get(key)
            for key in (
                "total_documents",
                "total_nodes",
                "pending_consolidation",
                "failed_consolidation",
                "total_observations",
                "last_consolidated_at",
            )
        }

    async def close(self) -> None:
        await self.http.aclose()
