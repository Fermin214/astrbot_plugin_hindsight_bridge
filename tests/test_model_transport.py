"""Exercise the standalone transport against local HTTP fixtures only."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "model_transport.py"
SPEC = importlib.util.spec_from_file_location("model_transport", SOURCE)
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


@contextmanager
def running(server):
    worker = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True,
    )
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = {
            "path": self.path,
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "body": body,
            "json": json.loads(body),
        }
        self.server.requests.append(request)
        status, headers, response = self.server.respond(request)
        if not isinstance(response, bytes):
            response = json.dumps(response).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.upstream.requests = []
        self.upstream.respond = lambda request: (200, {}, {})
        self.addCleanup(self.upstream.server_close)
        self.upstream_context = running(self.upstream)
        self.upstream_context.__enter__()
        self.addCleanup(self.upstream_context.__exit__, None, None, None)
        self.base = "http://127.0.0.1:" + str(self.upstream.server_port)
        self.opener = build_opener(ProxyHandler({}))

    def descriptor(self, protocol, **values):
        config = {
            "protocol": protocol, "base_url": self.base, "api_key": "upstream-secret",
            "model": "configured-model", "dimensions": 2, "send_dimensions": True,
            "headers": {"X-Custom": "custom-value"}, "extra_body": {},
            "proxy": "", "timeout": 2,
        }
        config.update(values)
        return config

    def reply(self, body, status=200, headers=None):
        self.upstream.respond = lambda request: (status, headers or {}, body)

    def latest(self):
        return self.upstream.requests[-1]

    def request(self, server, path, body=None, headers=None):
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        url = "http://127.0.0.1:" + str(server.server_port) + path
        request = Request(url, data=body, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def gateway(self, config):
        return running(transport.TransportServer(("127.0.0.1", 0), config, "local-secret"))

    def test_openai_path_payload_auth_and_index_order(self):
        config = self.descriptor(
            "openai", base_url=self.base + "/v1?region=local",
            extra_body={"encoding_format": "float", "model": "wrong", "input": ["wrong"]},
        )
        self.reply({"data": [
            {"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]},
        ]})
        result = transport.request_embedding(config, ["first", "second"])
        request = self.latest()
        self.assertEqual(request["path"], "/v1/embeddings?region=local")
        self.assertEqual(request["json"], {
            "model": "configured-model", "input": ["first", "second"],
            "dimensions": 2, "encoding_format": "float",
        })
        self.assertEqual(request["headers"]["authorization"], "Bearer upstream-secret")
        self.assertEqual(request["headers"]["x-custom"], "custom-value")
        self.assertEqual([item["embedding"] for item in result["data"]], [[1, 2], [3, 4]])
        self.assertEqual([item["index"] for item in result["data"]], [0, 1])

    def test_embedding_dimension_send_policy(self):
        for protocol, response in (
            ("openai", {"data": [{"index": 0, "embedding": [1, 2]}]}),
            ("ollama", {"embeddings": [[1, 2]]}),
        ):
            with self.subTest(protocol=protocol):
                config = self.descriptor(
                    protocol, send_dimensions=False, extra_body={"dimensions": 99},
                )
                self.reply(response)
                transport.request_embedding(config, "text")
                self.assertNotIn("dimensions", self.latest()["json"])

    def test_dashscope_native_path_parameters_and_order(self):
        config = self.descriptor(
            "dashscope", base_url=self.base + "/api/v1",
            extra_body={"parameters": {"text_type": "document", "dimension": 99}},
        )
        self.reply({"output": {"embeddings": [
            {"text_index": 1, "embedding": [3, 4]},
            {"text_index": 0, "embedding": [1, 2]},
        ]}})
        result = transport.request_embedding(config, ["first", "second"])
        self.assertEqual(
            self.latest()["path"],
            "/api/v1/services/embeddings/text-embedding/text-embedding",
        )
        self.assertEqual(self.latest()["json"], {
            "model": "configured-model", "input": {"texts": ["first", "second"]},
            "parameters": {"text_type": "document", "dimension": 2},
        })
        self.assertEqual(result["data"][0]["embedding"], [1, 2])
        config["send_dimensions"] = False
        transport.request_embedding(config, ["first", "second"])
        self.assertEqual(self.latest()["json"]["parameters"], {"text_type": "document"})

    def test_gemini_versions_payload_and_no_normalization(self):
        self.reply({"embeddings": [{"values": [3, 4]}, {"values": [6, 8]}]})
        for base, model in (
            (self.base, "gemini-embedding-001"),
            (self.base + "/v1beta/", "models/gemini-embedding-001"),
        ):
            with self.subTest(base=base):
                config = self.descriptor("gemini", base_url=base, model=model)
                result = transport.request_embedding(config, ["first", "second"])
                self.assertEqual(
                    self.latest()["path"],
                    "/v1beta/models/gemini-embedding-001:batchEmbedContents",
                )
                self.assertEqual(self.latest()["json"]["requests"], [
                    {
                        "model": "models/gemini-embedding-001",
                        "content": {"parts": [{"text": text}]},
                        "outputDimensionality": 2,
                    } for text in ["first", "second"]
                ])
                self.assertEqual(self.latest()["headers"]["x-goog-api-key"], "upstream-secret")
                self.assertEqual(result["data"][0]["embedding"], [3, 4])
        config["send_dimensions"] = False
        transport.request_embedding(config, ["first", "second"])
        self.assertNotIn("outputDimensionality", self.latest()["json"]["requests"][0])

    def test_ollama_native_path_and_payload(self):
        self.reply({"embeddings": [[1, 2]]})
        result = transport.request_embedding(self.descriptor("ollama"), ["first"])
        self.assertEqual(self.latest()["path"], "/api/embed")
        self.assertEqual(self.latest()["json"], {
            "model": "configured-model", "input": ["first"], "dimensions": 2,
        })
        self.assertEqual(result["data"][0]["embedding"], [1, 2])

    def test_embedding_validation(self):
        invalid = [
            {"data": []},
            {"data": [{"index": 0, "embedding": [1]}]},
            {"data": [{"index": 0, "embedding": [True, 2]}]},
            {"data": [{"index": 0, "embedding": [float("nan"), 2]}]},
            {"data": [{"index": 0, "embedding": [float("inf"), 2]}]},
            {"data": [{"index": 1, "embedding": [1, 2]}]},
            {"data": [{"index": True, "embedding": [1, 2]}]},
            {"data": [{"index": 0, "embedding": [1, 2]}] * 2},
        ]
        for response in invalid:
            with self.subTest(response=response):
                self.reply(response)
                texts = ["a", "b"] if len(response["data"]) == 2 else ["a"]
                with self.assertRaisesRegex(transport.TransportError, "^Model transport failed$"):
                    transport.request_embedding(self.descriptor("openai"), texts)
        for dimension in (0, -1, True, 2.5, "2"):
            with self.subTest(dimension=dimension), self.assertRaises(transport.TransportError):
                transport.request_embedding(self.descriptor("openai", dimensions=dimension), ["a"])

    def test_cohere_full_url_and_descriptor_fields(self):
        self.reply({"results": [
            {"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1},
        ]})
        config = self.descriptor(
            "cohere", base_url=self.base + "/v2/rerank?region=local",
            instruct="Prefer exact matches",
            extra_body={
                "model": "wrong", "documents": ["wrong"], "return_documents": False,
                "instruct": "wrong", "top_n": 1,
            },
        )
        result = transport.request_rerank(config, "query", ["a", "b"])
        self.assertEqual(self.latest()["path"], "/v2/rerank?region=local")
        self.assertEqual(self.latest()["json"], {
            "model": "configured-model", "query": "query", "documents": ["a", "b"],
            "return_documents": False, "instruct": "Prefer exact matches", "top_n": 2,
        })
        self.assertEqual(result["results"][0], {"index": 1, "relevance_score": 0.9})
        config["instruct"] = ""
        transport.request_rerank(config, "query", ["a", "b"])
        self.assertNotIn("instruct", self.latest()["json"])
        self.assertEqual(self.latest()["json"]["top_n"], 2)

    def test_bailian_native_preserves_instruction_and_return_documents(self):
        self.reply({"output": {"results": [
            {"index": 0, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.4},
        ]}})
        config = self.descriptor(
            "bailian_native", base_url=self.base + "/api/v1/native-rerank",
            model="qwen3-rerank", instruct="Prefer exact matches", return_documents=True,
            extra_body={"parameters": {"top_n": 1, "instruct": "wrong"}},
        )
        transport.request_rerank(config, "query", ["a", "b"])
        self.assertEqual(self.latest()["path"], "/api/v1/native-rerank")
        self.assertEqual(self.latest()["json"], {
            "model": "qwen3-rerank", "input": {"query": "query", "documents": ["a", "b"]},
            "parameters": {
                "instruct": "Prefer exact matches", "return_documents": True, "top_n": 2,
            },
        })

    def test_bailian_native_omits_inapplicable_optional_parameters(self):
        self.reply({"output": {"results": [{"index": 0, "relevance_score": 0.8}]}})
        for model, instruct, expected_instruction in (
            ("gte-rerank", "Prefer exact matches", None),
            ("qwen3-rerank", "", None),
            (" QWEN3-RERANK ", "Prefer exact matches", "Prefer exact matches"),
        ):
            with self.subTest(model=model, instruct=instruct):
                config = self.descriptor(
                    "bailian_native", model=model, instruct=instruct, return_documents=False,
                    extra_body={
                        "parameters": {"top_n": 99, "return_documents": True, "instruct": "wrong"},
                    },
                )
                transport.request_rerank(config, "query", ["a"])
                expected = {"top_n": 1}
                if expected_instruction is not None:
                    expected["instruct"] = expected_instruction
                self.assertEqual(self.latest()["json"]["parameters"], expected)

    def test_rerank_requests_all_results_beyond_upstream_default_ten(self):
        documents = ["document-" + str(index) for index in range(12)]
        for protocol in ("cohere", "bailian_native"):
            with self.subTest(protocol=protocol):
                def respond(request):
                    body = request["json"]
                    options = body["parameters"] if protocol == "bailian_native" else body
                    count = options.get("top_n", 10)
                    items = [
                        {"index": index, "relevance_score": 1 / (index + 1)}
                        for index in range(min(count, len(documents)))
                    ]
                    response = {"results": items}
                    if protocol == "bailian_native":
                        response = {"output": response}
                    return 200, {}, response
                self.upstream.respond = respond
                result = transport.request_rerank(self.descriptor(protocol), "query", documents)
                self.assertEqual(len(result["results"]), 12)
                self.assertEqual({item["index"] for item in result["results"]}, set(range(12)))

    def test_all_rerank_protocols_reject_incomplete_result_coverage(self):
        documents = ["document-" + str(index) for index in range(12)]
        for protocol in ("cohere", "bailian_native", "tei"):
            for count in (0, 10):
                with self.subTest(protocol=protocol, returned=count):
                    score = "score" if protocol == "tei" else "relevance_score"
                    items = [{"index": index, score: 0.5} for index in range(count)]
                    response = items if protocol == "tei" else {"results": items}
                    if protocol == "bailian_native":
                        response = {"output": response}
                    self.reply(response)
                    with self.assertRaisesRegex(
                        transport.TransportError, "^Model transport failed$",
                    ):
                        transport.request_rerank(self.descriptor(protocol), "query", documents)

    def test_tei_full_url_options_and_score_conversion(self):
        self.reply([{"index": 1, "score": 4.5}, {"index": 0, "score": -2.0}])
        config = self.descriptor(
            "tei", base_url=self.base + "/rerank",
            tei_options={
                "truncate": True, "truncation_direction": "left", "raw_scores": True,
                "return_text": True, "texts": ["wrong"],
            },
        )
        result = transport.request_rerank(config, "query", ["a", "b"])
        self.assertEqual(self.latest()["path"], "/rerank")
        self.assertEqual(self.latest()["json"], {
            "query": "query", "texts": ["a", "b"], "truncate": True,
            "truncation_direction": "left", "raw_scores": True, "return_text": True,
        })
        self.assertEqual(result, {"results": [
            {"index": 1, "relevance_score": 4.5}, {"index": 0, "relevance_score": -2.0},
        ]})

    def test_rerank_rejects_duplicate_out_of_range_and_nonfinite_results(self):
        for items in (
            [{"index": 0, "relevance_score": 1}] * 2,
            [{"index": -1, "relevance_score": 1}],
            [{"index": 1, "relevance_score": 1}],
            [{"index": True, "relevance_score": 1}],
            [{"index": 0, "relevance_score": True}],
            [{"index": 0, "relevance_score": float("inf")}],
            [{"index": 0, "relevance_score": float("nan")}],
        ):
            with self.subTest(items=items):
                self.reply({"results": items})
                with self.assertRaises(transport.TransportError):
                    documents = ["a", "b"] if len(items) == 2 else ["a"]
                    transport.request_rerank(self.descriptor("cohere"), "query", documents)

    def test_chat_preserves_raw_body_response_and_azure_query(self):
        raw = b'{ "model":"hindsight-model", "messages": [], "stream":false }'
        response = b'{ "id": "unchanged", "choices": [] }\n'
        self.reply(response)
        config = self.descriptor(
            "azure", base_url=self.base + "/openai/deployments/example?api-version=2024-10-21",
            headers={"X-Custom": "configured", "anthropic-version": "custom-version"},
            extra_body={"must_not_be_merged": True},
        )
        result = transport.forward_chat(config, "chat/completions?trace=1", {
            "Authorization": "Bearer client-key", "api-key": "wrong",
            "Content-Type": "application/json; charset=utf-8",
            "anthropic-version": "client-version", "X-Unrelated": "drop",
        }, raw)
        self.assertEqual(result, response)
        request = self.latest()
        self.assertEqual(
            request["path"],
            "/openai/deployments/example/chat/completions?api-version=2024-10-21&trace=1",
        )
        self.assertEqual(request["body"], raw)
        self.assertEqual(request["headers"]["api-key"], "upstream-secret")
        self.assertNotIn("authorization", request["headers"])
        self.assertEqual(request["headers"]["anthropic-version"], "custom-version")
        self.assertEqual(request["headers"]["content-type"], "application/json; charset=utf-8")
        self.assertNotIn("x-unrelated", request["headers"])

    def test_chat_anthropic_headers_and_stream_rejection(self):
        self.reply({"content": []})
        transport.forward_chat(self.descriptor("anthropic"), "v1/messages", {
            "x-api-key": "wrong", "anthropic-version": "2023-06-01",
        }, b'{"messages":[]}')
        self.assertEqual(self.latest()["headers"]["x-api-key"], "upstream-secret")
        self.assertEqual(self.latest()["headers"]["anthropic-version"], "2023-06-01")
        with self.assertRaises(transport.TransportError):
            transport.forward_chat(self.descriptor("openai"), "responses", {}, b'{"stream":true}')

    def test_health_authentication_and_pinned_model(self):
        self.reply({"data": [{"index": 0, "embedding": [1, 2]}]})
        with self.gateway({"embedding": self.descriptor("openai")}) as server:
            status, body = self.request(server, "/health")
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            for headers in (
                {}, {"Authorization": "Bearer wrong"},
                {"X-Astrbot-Transport-Key": "wrong"},
            ):
                with self.subTest(headers=headers):
                    status, body = self.request(server, "/v1/embeddings", {"input": "a"}, headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body), {"error": "Unauthorized"})
            self.assertEqual(self.upstream.requests, [])
            for headers in (
                {"Authorization": "Bearer local-secret"},
                {"X-Astrbot-Transport-Key": "local-secret"},
                {"Authorization": "Bearer wrong", "X-Astrbot-Transport-Key": "local-secret"},
            ):
                with self.subTest(headers=headers):
                    status, body = self.request(server, "/v1/embeddings", {
                        "input": "a", "model": "attacker-model", "dimensions": 2,
                    }, headers)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["model"], "configured-model")
                    self.assertEqual(self.latest()["json"]["model"], "configured-model")

    def test_gateway_rerank_and_chat_routes(self):
        config = {
            "rerank": self.descriptor("cohere", base_url=self.base + "/rerank"),
            "chat": self.descriptor("openai", base_url=self.base + "/v1"),
        }
        with self.gateway(config) as server:
            headers = {"Authorization": "Bearer local-secret"}
            self.reply({"results": [{"index": 0, "relevance_score": 0.5}]})
            status, body = self.request(
                server, "/rerank", {"model": "wrong", "query": "q", "documents": ["a"]}, headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["results"][0]["relevance_score"], 0.5)
            self.reply(b'{ "output": [] }')
            status, body = self.request(
                server, "/chat/responses?trace=1", b'{ "model": "native-model" }', headers,
            )
            self.assertEqual((status, body), (200, b'{ "output": [] }'))
            self.assertEqual(self.latest()["path"], "/v1/responses?trace=1")
            self.assertEqual(self.latest()["headers"]["authorization"], "Bearer upstream-secret")
            self.assertNotIn("local-secret", str(self.latest()))

    def test_gateway_errors_are_generic_and_silent(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.gateway({"embedding": self.descriptor("openai")}) as server:
                headers = {"Authorization": "Bearer local-secret"}
                for response, status in (
                    ({"error": "upstream-secret private-content"}, 401),
                    (b"invalid-json upstream-secret", 200),
                    ({"data": [{"index": 0, "embedding": [1]}]}, 200),
                ):
                    with self.subTest(response=response):
                        self.reply(response, status)
                        actual, body = self.request(server, "/v1/embeddings", {"input": ["a"]}, headers)
                        self.assertEqual((actual, json.loads(body)), (
                            502, {"error": "Model transport failed"},
                        ))
                for body in (b"{", b'{"input":"a","dimensions":3}', b'{"input":[1]}'):
                    actual, response = self.request(server, "/v1/embeddings", body, headers)
                    self.assertEqual((actual, json.loads(response)), (
                        502, {"error": "Model transport failed"},
                    ))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_alternate_gateway_key_preserves_protocol_authorization(self):
        self.reply({"choices": []})
        with self.gateway({"chat": self.descriptor("openai", api_key="")}) as server:
            status, _ = self.request(server, "/chat/chat/completions", {"messages": []}, {
                "X-Astrbot-Transport-Key": "local-secret",
                "Authorization": "Bearer upstream-client-key",
            })
            self.assertEqual(status, 200)
            self.assertEqual(
                self.latest()["headers"]["authorization"], "Bearer upstream-client-key",
            )
            self.assertNotIn("x-astrbot-transport-key", self.latest()["headers"])

    def test_gemini_chat_authentication_with_alternate_gateway_key(self):
        raw = b'{ "contents": [{"parts": [{"text": "hello"}]}] }'
        response = b'{ "candidates": [] }\n'
        self.reply(response)
        for key, expected in (
            ("upstream-secret", "upstream-secret"), ("", "sdk-client-key"),
        ):
            with self.subTest(api_key_configured=bool(key)):
                config = self.descriptor(
                    "gemini", api_key=key, base_url=self.base + "/v1beta",
                )
                with self.gateway({"chat": config}) as server:
                    status, body = self.request(
                        server, "/chat/models/gemini-test:generateContent", raw,
                        {
                            "X-Astrbot-Transport-Key": "local-secret",
                            "X-Goog-Api-Key": "sdk-client-key",
                            "Content-Type": "application/json",
                        },
                    )
                self.assertEqual((status, body), (200, response))
                request = self.latest()
                self.assertEqual(request["path"], "/v1beta/models/gemini-test:generateContent")
                self.assertEqual(request["body"], raw)
                self.assertEqual(request["headers"]["x-goog-api-key"], expected)
                self.assertNotIn("authorization", request["headers"])
                self.assertNotIn("x-astrbot-transport-key", request["headers"])
                self.assertNotIn("local-secret", str(request))

    def test_proxy_configuration_timeout_and_explicit_no_system_proxy(self):
        self.reply({"embeddings": [[1, 2]]})
        real_build = transport.build_opener
        with patch.object(transport, "build_opener", wraps=real_build) as build:
            transport.request_embedding(self.descriptor("ollama", timeout=1.5), ["a"])
            self.assertEqual(build.call_args.args[0].proxies, {})
        # The local fixture acts as an HTTP proxy for a deliberately unresolvable host.
        config = self.descriptor(
            "ollama", base_url="http://model.invalid", proxy=self.base,
        )
        with patch.dict(os.environ, {"no_proxy": "", "NO_PROXY": ""}):
            transport.request_embedding(config, ["a"])
        self.assertEqual(self.latest()["path"], "http://model.invalid/api/embed")
        for timeout in (0, -1, True, float("inf"), "1"):
            with self.subTest(timeout=timeout), self.assertRaises(transport.TransportError):
                transport.request_embedding(self.descriptor("ollama", timeout=timeout), ["a"])
        opener = unittest.mock.Mock()
        opener.open.side_effect = TimeoutError("upstream-secret")
        with patch.object(transport, "build_opener", return_value=opener):
            with self.assertRaisesRegex(transport.TransportError, "^Model transport failed$"):
                transport.request_embedding(self.descriptor("ollama", timeout=1.5), ["a"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 1.5)

    def test_redirects_do_not_forward_credentials(self):
        self.reply(b"", 307, {"Location": self.base + "/credential-leak"})
        with self.assertRaises(transport.TransportError):
            transport.request_embedding(self.descriptor("openai"), ["a"])
        self.assertEqual(len(self.upstream.requests), 1)

    def test_invalid_configuration_is_silent_and_never_opens_a_server(self):
        with patch.dict(os.environ, {
            "ASTRBOT_MEMORY_MODEL_CONFIG": "{upstream-secret",
            "ASTRBOT_MEMORY_TRANSPORT_KEY": "local-secret",
        }):
            with patch.object(transport, "TransportServer") as server:
                self.assertEqual(transport.main(), 1)
                server.assert_not_called()
        for token in ("", " ", None):
            with self.subTest(token=token), self.assertRaises(transport.TransportError):
                transport.TransportServer(("127.0.0.1", 0), {}, token)


if __name__ == "__main__":
    unittest.main()
