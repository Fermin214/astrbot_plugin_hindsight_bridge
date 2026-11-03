"""Verify native AstrBot providers and real Hindsight SDKs with a test credential.

Run the two stages in their respective pinned official images. The intermediate
environment contains credentials and must stay in a private test directory.
"""

import argparse
import asyncio
from contextlib import contextmanager
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import sys
import threading

from qwen_probe import read_key

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.providers import ProviderCatalog


def private_json(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file)


def save_report(path, value, key):
    encoded = json.dumps(value, indent=2)
    if key and key in encoded:
        raise RuntimeError("Credential found in report")
    Path(path).write_text(encoded + "\n")
    print(encoded)


async def astrbot(args):
    from astrbot.core.config.default import VERSION
    from astrbot.core.provider.sources.bailian_rerank_source import BailianRerankProvider
    from astrbot.core.provider.sources.dashscope_embedding_source import DashScopeEmbeddingProvider

    assert VERSION == "4.29.1", VERSION
    key = read_key(args.secret_file)
    embedding = DashScopeEmbeddingProvider({
        "id": "delivery-embedding", "type": "dashscope_embedding", "enable": True,
        "embedding_model": args.embedding_model, "embedding_api_base": args.base_url,
        "embedding_api_key": key,
    }, {})
    rerank = BailianRerankProvider({
        "id": "delivery-rerank", "type": "bailian_rerank", "enable": True,
        "rerank_model": args.rerank_model,
        "rerank_api_base": args.base_url.rstrip("/") + "/services/rerank/text-rerank/text-rerank",
        "rerank_api_key": key,
    }, {})
    try:
        async with asyncio.timeout(45):
            vectors = await embedding.get_embeddings(["The user prefers jasmine tea.", "What tea does the user like?"])
            scores = await rerank.rerank("What tea does the user like?", ["A laptop needs a battery.", "The user prefers jasmine tea."])
        assert len(vectors) == 2 and len(vectors[0]) == len(vectors[1]) == 1024
        assert all(math.isfinite(float(value)) for vector in vectors for value in vector)
        assert len(scores) == 2 and scores[0].index == 1
        catalog = ProviderCatalog(None)
        environment = {}
        descriptors = {}
        for kind, provider in (("embedding", embedding), ("rerank", rerank)):
            values = catalog._environment(provider, kind, dimensions=1024)
            descriptors.update(json.loads(values.pop("ASTRBOT_MEMORY_MODEL_CONFIG", "{}")))
            environment.update(values)
        environment["ASTRBOT_MEMORY_MODEL_CONFIG"] = json.dumps(descriptors)
        private_json(args.environment_file, environment)
        save_report(args.output, {"astrbot": VERSION, "embedding_dimensions": 1024,
                    "passed": ["native_dashscope_embedding", "native_bailian_rerank", "bridge_provider_descriptors"]}, key)
    finally:
        await rerank.client.close()


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def hindsight(args):
    from hindsight_api.config import clear_config_cache
    from hindsight_api.engine.cross_encoder import create_cross_encoder_from_env
    from hindsight_api.engine.embeddings import create_embeddings_from_env

    environment = json.loads(Path(args.environment_file).read_text())
    descriptors = json.loads(environment.pop("ASTRBOT_MEMORY_MODEL_CONFIG"))
    spec = importlib.util.spec_from_file_location("model_transport", ROOT / "deploy/model_transport.py")
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    server = transport.TransportServer(("127.0.0.1", 0), descriptors, "delivery-transport")
    base = "http://127.0.0.1:" + str(server.server_port)
    for name, value in environment.items():
        os.environ[name] = value.replace("http://memory-models:8765", base).replace(
            "__ASTRBOT_MEMORY_TRANSPORT_KEY__", "delivery-transport")
    clear_config_cache()
    with running(server):
        embeddings = create_embeddings_from_env()
        reranker = create_cross_encoder_from_env()
        await embeddings.initialize()
        await reranker.initialize()
        async with asyncio.timeout(60):
            vectors = await embeddings.encode(["The user prefers jasmine tea.", "What tea does the user like?"])
            scores = await reranker.predict([("What tea does the user like?", "A laptop needs a battery."),
                                            ("What tea does the user like?", "The user prefers jasmine tea.")])
        assert len(vectors) == 2 and all(len(vector) == 1024 for vector in vectors)
        assert all(math.isfinite(float(value)) for vector in vectors for value in vector)
        assert len(scores) == 2 and all(math.isfinite(float(value)) for value in scores) and scores[1] > scores[0]
    save_report(args.output, {"hindsight_image": "1.1.0-slim", "embedding_dimensions": 1024,
                "passed": ["real_hindsight_embedding_via_bridge_transport", "real_hindsight_rerank_via_bridge_transport"]},
                descriptors["embedding"]["api_key"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("astrbot", "hindsight"))
    parser.add_argument("--secret-file")
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default="https://cn-beijing.maas.qianwenaiapi.com/api/v1")
    parser.add_argument("--embedding-model", default="qwen3.7-text-embedding-flash")
    parser.add_argument("--rerank-model", default="qwen3.7-text-rerank")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(astrbot(args) if args.stage == "astrbot" else hindsight(args))
        return 0
    except Exception as error:
        print(json.dumps({"stage": args.stage, "passed": False, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
