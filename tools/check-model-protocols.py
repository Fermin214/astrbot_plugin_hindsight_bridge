"""Run local protocol fixtures with the pinned Hindsight 1.1.0 SDKs."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent.parent))
from plugins.astrbot_plugin_hindsight_bridge.core.providers import ProviderCatalog

spec = importlib.util.spec_from_file_location("model_transport", ROOT / "deploy/model_transport.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

logging.disable(logging.CRITICAL)


class Probe(BaseModel):
    ok: bool


class Provider:
    def __init__(self, kind, adapter, model, **config):
        self.kind, self.adapter, self.model = kind, adapter, model
        self.provider_config = config

    def meta(self):
        return SimpleNamespace(id=self.kind, provider_type=self.kind, type=self.adapter)

    def get_model(self):
        return self.model

    def get_dim(self):
        return 3

    def get_current_key(self):
        return "fixture-key"


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, dict(self.headers), body))
        path = urlsplit(self.path).path
        if path.endswith("/chat/completions"):
            result = {"id": "c", "object": "chat.completion", "model": "fixture",
                      "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"ok":true}'},
                                   "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        elif path.endswith("/responses"):
            result = {"id": "r", "object": "response", "status": "completed", "model": "fixture",
                      "output": [{"id": "m", "type": "message", "role": "assistant", "status": "completed",
                                  "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}]}],
                      "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        elif self.path.endswith("/messages"):
            content = [{"type": "text", "text": '{"ok":true}'}]
            if body.get("tools"):
                content = [{"type": "tool_use", "id": "t", "name": body["tools"][0]["name"],
                            "input": {"ok": True}}]
            result = {"id": "m", "type": "message", "role": "assistant", "model": "fixture",
                      "content": content, "stop_reason": "end_turn",
                      "usage": {"input_tokens": 1, "output_tokens": 1}}
        elif ":generateContent" in self.path:
            result = {"candidates": [{"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        elif ":batchEmbedContents" in self.path:
            result = {"embeddings": [{"values": [0.2, 0.3, 0.4]} for _ in body["requests"]]}
        elif "/api/embed" in self.path:
            result = {"embeddings": [[0.2, 0.3, 0.4] for _ in body["input"]]}
        elif "text-embedding/text-embedding" in self.path:
            result = {"output": {"embeddings": [
                {"text_index": i, "embedding": [0.2, 0.3, 0.4]}
                for i, _ in enumerate(body["input"]["texts"])
            ]}}
        elif self.path.endswith("/embeddings"):
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            result = {"data": [{"index": i, "embedding": [0.2, 0.3, 0.4]} for i, _ in enumerate(texts)]}
        elif "/tei/rerank" in self.path:
            result = [{"index": i, "score": 0.9 - i * 0.2} for i, _ in enumerate(body["texts"])]
        elif "/native-rerank" in self.path:
            result = {"output": {"results": [
                {"index": i, "relevance_score": 0.9 - i * 0.2}
                for i, _ in enumerate(body["input"]["documents"])
            ]}}
        else:
            result = {"results": [
                {"index": i, "relevance_score": 0.9 - i * 0.2}
                for i, _ in enumerate(body["documents"])
            ]}
        raw = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def main():
    from hindsight_api.config import clear_config_cache
    from hindsight_api.engine.cross_encoder import create_cross_encoder_from_env
    from hindsight_api.engine.embeddings import create_embeddings_from_env
    from hindsight_api.engine.llm_wrapper import LLMProvider

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    fixture.requests = []
    gateway = transport.TransportServer(("127.0.0.1", 0), {}, "transport-key")
    with running(fixture), running(gateway):
        upstream = "http://127.0.0.1:" + str(fixture.server_port)
        relay = "http://127.0.0.1:" + str(gateway.server_port)

        def configure(provider, kind):
            env = ProviderCatalog(None)._environment(provider, kind)
            gateway.model_config = json.loads(env.pop("ASTRBOT_MEMORY_MODEL_CONFIG", "{}"))
            env.pop("ASTRBOT_MEMORY_EMBEDDING_IDENTITY", None)
            for key in list(os.environ):
                if key.startswith(("HINDSIGHT_API_LLM_", "HINDSIGHT_API_EMBEDDINGS_", "HINDSIGHT_API_RERANKER_")):
                    del os.environ[key]
            os.environ.update({
                key: value.replace("http://memory-models:8765", relay).replace(
                    "__ASTRBOT_MEMORY_TRANSPORT_KEY__", "transport-key"
                ) for key, value in env.items()
            })
            clear_config_cache()

        checked = []
        for adapter, model, options in (
            ("openai_chat_completion", "fixture", {}),
            ("openai_responses", "fixture", {}),
            ("anthropic_chat_completion", "fixture", {}),
            ("googlegenai_chat_completion", "gemini-2.5-flash", {}),
            ("openai_chat_completion", "fixture", {"api_version": "2024-10-21"}),
            ("openai_responses", "fixture", {"api_version": "2024-10-21"}),
            ("openai_responses", "fixture", {
                "api_base": "http://fixture-upstream.invalid/api", "proxy": upstream,
                "custom_headers": {"X-Fixture": "It's $literal\\value"},
            }),
        ):
            config = {"api_base": upstream, "custom_extra_body": {"temperature": 0.4}, **options}
            configure(Provider("chat_completion", adapter, model, **config), "chat")
            llm = LLMProvider.from_env()
            try:
                await llm.verify_connection()
                result = await llm.call([{"role": "user", "content": "probe"}], response_format=Probe,
                                        max_completion_tokens=64, max_retries=0)
                assert result is not None
            finally:
                await llm.cleanup()
            if options.get("api_version"):
                path, headers, _ = fixture.requests[-1]
                assert "/deployments/fixture/" in path and "api-version=2024-10-21" in path
                assert {k.lower(): v for k, v in headers.items()}["api-key"] == "fixture-key"
            if options.get("proxy"):
                path, headers, body = fixture.requests[-1]
                assert path.startswith("http://fixture-upstream.invalid/api/responses")
                assert {k.lower(): v for k, v in headers.items()}["x-fixture"] == "It's $literal\\value"
                assert body["temperature"] == 0.4
            checked.append(adapter + ("_azure" if options.get("api_version") else "_proxy" if options.get("proxy") else ""))

        for adapter in ("openai_embedding", "dashscope_embedding", "gemini_embedding", "ollama_embedding"):
            configure(Provider("embedding", adapter, "fixture", embedding_api_base=upstream,
                               embedding_api_key="fixture-key", embedding_dimensions=3,
                               embedding_dimensions_mode="never"), "embedding")
            embedding = create_embeddings_from_env()
            await embedding.initialize()
            vectors = await embedding.encode(["one", "two"])
            assert [list(vector) for vector in vectors] == [[0.2, 0.3, 0.4]] * 2
            checked.append(adapter)

        for adapter, path in (
            ("vllm_rerank", "/v1/rerank"), ("bailian_rerank", "/native-rerank"),
            ("tei_rerank", "/tei"),
        ):
            configure(Provider("rerank", adapter, "fixture", rerank_api_base=upstream + path,
                               rerank_api_suffix="", rerank_api_key="fixture-key",
                               custom_headers={"X-Fixture": "yes"}), "rerank")
            reranker = create_cross_encoder_from_env()
            await reranker.initialize()
            scores = await reranker.predict([("query", "one"), ("query", "two")])
            assert abs(scores[0] - 0.9) < 1e-6 and abs(scores[1] - 0.7) < 1e-6
            checked.append(adapter)
        print(json.dumps({"sdk_protocols_passed": checked, "requests": len(fixture.requests)}))


if __name__ == "__main__":
    asyncio.run(main())
