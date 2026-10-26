"""Contract tests for the public provider catalog and secret isolation."""

import asyncio
from enum import Enum
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_hindsight_bridge.core.providers import (
    ProviderCatalog,
    SetupError,
)


class Kind(Enum):
    CHAT = "chat_completion"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class Provider:
    def __init__(self, provider_id, kind, adapter, model, **config):
        self.id = provider_id
        self.kind = kind
        self.adapter = adapter
        self.model = model
        self.provider_config = {
            "id": provider_id, "type": adapter, "enable": True, **config
        }

    def meta(self):
        return SimpleNamespace(
            id=self.id, provider_type=self.kind, type=self.adapter,
            model=self.get_model(),
        )

    def get_model(self):
        return self.model if self.kind == Kind.CHAT else ""

    def get_current_key(self):
        return "chat-secret"

    def get_dim(self):
        return self.provider_config.get("embedding_dimensions", 1024)


class Context:
    def __init__(self, *providers):
        self.providers = {provider.id: provider for provider in providers}
        self.lookups = []

    def get_all_providers(self):
        return [p for p in self.providers.values() if p.kind == Kind.CHAT]

    def get_all_embedding_providers(self):
        return [p for p in self.providers.values() if p.kind == Kind.EMBEDDING]

    def get_config(self):
        return {"provider": [p.provider_config for p in self.providers.values()]}

    def get_provider_by_id(self, provider_id):
        self.lookups.append(provider_id)
        return self.providers.get(provider_id)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.chat = Provider(
            "chat", Kind.CHAT, "openai_chat_completion", "actual-chat",
            api_base="https://api.deepseek.com/v1", key=["old-secret"],
            provider="deepseek",
        )
        self.embedding = Provider(
            "embed", Kind.EMBEDDING, "openai_embedding", "text-embedding-v4",
            embedding_api_key="embedding-secret",
            embedding_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            embedding_dimensions_mode="always",
            embedding_dimensions=1024,
        )
        self.context = Context(self.chat, self.embedding)
        self.catalog = ProviderCatalog(self.context)
        self.selection = {"chat_provider_id": "chat", "embedding_provider_id": "embed"}

    def resolve_rerank(self, adapter, base, model="chosen-reranker", **config):
        rerank = Provider(
            "rerank", Kind.RERANK, adapter, model,
            rerank_api_base=base, rerank_api_key="rerank-secret", **config,
        )
        self.context.providers["rerank"] = rerank
        return self.catalog.resolve({**self.selection, "rerank_provider_id": "rerank"})

    def test_openai_compatible_chat_uses_actual_model_key_and_exact_base(self):
        self.chat.provider_config["model"] = "stale-model"
        env = self.catalog.resolve(self.selection)
        self.assertEqual(env["HINDSIGHT_API_LLM_PROVIDER"], "openai")
        self.assertEqual(env["HINDSIGHT_API_LLM_MODEL"], "actual-chat")
        self.assertEqual(env["HINDSIGHT_API_LLM_API_KEY"], "chat-secret")
        self.assertEqual(env["HINDSIGHT_API_LLM_BASE_URL"], "https://api.deepseek.com/v1")
        self.assertNotIn("stale-model", env.values())
        self.assertNotIn("old-secret", env.values())
        self.assertTrue(all(isinstance(value, str) for value in env.values()))

    def test_embedding_uses_adapter_fields_and_runtime_model(self):
        self.embedding.provider_config.update(
            model="wrong-chat-model", key=["wrong-key"], api_base="https://wrong.invalid",
            embedding_model="stale-embedding-model",
        )
        env = self.catalog.resolve(self.selection)
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"], "text-embedding-v4")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"], "embedding-secret")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"], "1024")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE"], "10")
        self.assertEqual(
            env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"],
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def test_embedding_url_normalization_matches_astrbot(self):
        for base in ("https://example.test", "https://example.test/v1/embeddings/"):
            with self.subTest(base=base):
                self.embedding.provider_config["embedding_api_base"] = base
                env = self.catalog.resolve(self.selection)
                self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"], "https://example.test/v1")

    def test_optional_rerank_is_rrf_without_model_or_key(self):
        for value in (None, ""):
            env = self.catalog.resolve({**self.selection, "rerank_provider_id": value})
            self.assertEqual(
                {k: v for k, v in env.items() if "RERANKER" in k},
                {"HINDSIGHT_API_RERANKER_PROVIDER": "rrf"},
            )

    def test_options_are_allowlisted_and_contain_no_credentials_or_addresses(self):
        self.resolve_rerank("vllm_rerank", "https://api.siliconflow.cn")
        options = self.catalog.options()
        self.assertEqual(set(options), {"chat", "embedding", "rerank"})
        serialized = json.dumps(options)
        for value in (
            "chat-secret", "old-secret", "embedding-secret", "rerank-secret",
            "https://", "api_key",
        ):
            self.assertNotIn(value, serialized)
        for group in options.values():
            for option in group:
                self.assertEqual(set(option), {"id", "label", "type", "model", "supported", "reason"})

    def test_rerank_enumeration_uses_config_ids_and_public_lookup(self):
        self.resolve_rerank("vllm_rerank", "https://api.siliconflow.cn")
        self.context.lookups.clear()
        options = self.catalog.options()
        self.assertEqual(options["rerank"][0]["id"], "rerank")
        self.assertIn("rerank", self.context.lookups)

    def test_bailian_compatible_endpoint_has_no_invented_base_env(self):
        env = self.resolve_rerank(
            "bailian_rerank",
            "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            model="qwen3-rerank",
        )
        self.assertEqual(env["HINDSIGHT_API_RERANKER_PROVIDER"], "alibaba")
        self.assertEqual(env["HINDSIGHT_API_RERANKER_ALIBABA_MODEL"], "qwen3-rerank")
        self.assertEqual(env["HINDSIGHT_API_RERANKER_ALIBABA_API_KEY"], "rerank-secret")
        self.assertNotIn("HINDSIGHT_API_RERANKER_ALIBABA_BASE_URL", env)

    def test_siliconflow_and_cohere_use_exact_vllm_request_url(self):
        for backend, base, suffix, expected in (
            ("siliconflow", "https://api.siliconflow.cn", "/v1/rerank", "https://api.siliconflow.cn/v1"),
            ("cohere", "https://api.cohere.com", "/v2/rerank", "https://api.cohere.com/v2/rerank"),
        ):
            with self.subTest(backend=backend):
                env = self.resolve_rerank("vllm_rerank", base, rerank_api_suffix=suffix)
                prefix = "HINDSIGHT_API_RERANKER_" + backend.upper() + "_"
                self.assertEqual(env["HINDSIGHT_API_RERANKER_PROVIDER"], backend)
                self.assertEqual(env[prefix + "BASE_URL"], expected)
                self.assertEqual(env[prefix + "MODEL"], "chosen-reranker")
                self.assertEqual(env[prefix + "API_KEY"], "rerank-secret")
                if backend == "siliconflow":
                    # Hindsight 1.1 appends this path to the emitted base.
                    self.assertEqual(
                        env[prefix + "BASE_URL"].rstrip("/") + "/rerank",
                        base.rstrip("/") + suffix,
                    )

    def test_unknown_and_mismatched_selections_never_fall_back(self):
        for selection in (
            {}, None, [], {"chat_provider_id": []}, {**self.selection, "extra": "x"},
            {**self.selection, "chat_provider_id": "missing"},
            {**self.selection, "chat_provider_id": "embed"},
            {**self.selection, "embedding_provider_id": "chat"},
            {**self.selection, "rerank_provider_id": "chat"},
            {**self.selection, "rerank_provider_id": "missing"},
        ):
            with self.subTest(selection=selection), self.assertRaises(SetupError):
                self.catalog.resolve(selection)

    def test_unknown_adapters_are_rejected_and_explained(self):
        for adapter in ("deepseek", "custom_adapter"):
            self.chat.adapter = adapter
            with self.subTest(adapter=adapter):
                with self.assertRaises(SetupError):
                    self.catalog.resolve(self.selection)
                option = self.catalog.options()["chat"][0]
                self.assertFalse(option["supported"])
                self.assertTrue(option["reason"])

    def test_custom_transport_and_request_options_are_not_dropped(self):
        self.chat.adapter = "openai_responses"
        self.chat.provider_config.update(
            proxy="http://proxy.test:8080",
            custom_headers={"X-Route": "header-secret"},
            custom_extra_body={"temperature": 0.4, "instructions": "It's $a\\b"},
        )
        env = self.catalog.resolve(self.selection)
        self.assertEqual(env["HINDSIGHT_API_LLM_PROVIDER"], "openai-responses")
        self.assertEqual(json.loads(env["HINDSIGHT_API_LLM_EXTRA_BODY"])["temperature"], 0.4)
        descriptor = json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["chat"]
        self.assertEqual(descriptor["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(descriptor["proxy"], "http://proxy.test:8080")
        self.assertEqual(descriptor["headers"]["X-Route"], "header-secret")
        self.assertNotIn("secret", json.dumps(self.catalog.options()))
        self.chat.provider_config["custom_extra_body"] = {"model": "different-model"}
        with self.assertRaisesRegex(SetupError, "覆盖"):
            self.catalog.resolve(self.selection)

    def test_dimension_is_explicit_positive_integer(self):
        for dim in (0, -1, None, True, "1024", 1024.0):
            self.embedding.provider_config["embedding_dimensions"] = dim
            with self.subTest(dim=dim), self.assertRaises(SetupError):
                self.catalog.resolve(self.selection)

    def test_dimension_suppression_keeps_declared_storage_dimension(self):
        self.embedding.provider_config["embedding_dimensions_mode"] = "never"
        env = self.catalog.resolve(self.selection)
        descriptor = json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["embedding"]
        self.assertFalse(descriptor["send_dimensions"])
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"], "1024")

    def test_embedding_adapter_and_key_are_not_taken_from_chat(self):
        self.embedding.adapter = "unknown_embedding"
        with self.assertRaises(SetupError):
            self.catalog.resolve(self.selection)
        self.embedding.adapter = "openai_embedding"
        self.embedding.provider_config["embedding_api_key"] = ""
        with self.assertRaisesRegex(SetupError, "嵌入供应商缺少密钥"):
            self.catalog.resolve(self.selection)
        self.assertFalse(self.catalog.options()["embedding"][0]["supported"])

    def test_chat_missing_current_key_does_not_pick_another_config_key(self):
        self.chat.get_current_key = lambda: None
        with self.assertRaisesRegex(SetupError, "对话供应商缺少可用密钥"):
            self.catalog.resolve(self.selection)

    def test_local_rerank_can_omit_key_and_preserves_custom_path(self):
        self.resolve_rerank("vllm_rerank", "https://api.siliconflow.cn")
        rerank = self.context.providers["rerank"]
        rerank.provider_config["rerank_api_key"] = ""
        env = self.catalog.resolve({**self.selection, "rerank_provider_id": "rerank"})
        self.assertEqual(json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["rerank"]["api_key"], "")
        rerank.provider_config["rerank_api_suffix"] = "/custom"
        env = self.catalog.resolve({**self.selection, "rerank_provider_id": "rerank"})
        self.assertEqual(
            json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["rerank"]["base_url"],
            "https://api.siliconflow.cn/custom",
        )

    def test_bad_urls_and_request_endpoints_are_rejected_safely(self):
        for base in (
            "file:///secret", "https://user:secret@example.test/v1",
            "https://example.test/v1#secret",
            "https://example.test/responses", "https://example.test/chat/completions",
            "https://example.test:bad/v1", "https://example.test/\nsecret",
        ):
            self.chat.provider_config["api_base"] = base
            with self.subTest(base=base), self.assertRaises(SetupError) as caught:
                self.catalog.resolve(self.selection)
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("https://", str(caught.exception))

    def test_native_bailian_and_tei_preserve_request_semantics(self):
        env = self.resolve_rerank(
            "bailian_rerank",
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            instruct="排序指令",
        )
        descriptor = json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["rerank"]
        self.assertEqual(descriptor["protocol"], "bailian_native")
        self.assertEqual(descriptor["instruct"], "排序指令")
        env = self.resolve_rerank(
            "tei_rerank", "http://tei.test", model="",
            tei_rerank_truncate=True, tei_rerank_raw_scores=True,
        )
        descriptor = json.loads(env["ASTRBOT_MEMORY_MODEL_CONFIG"])["rerank"]
        self.assertEqual(descriptor["base_url"], "http://tei.test/rerank")
        self.assertEqual(descriptor["tei_options"], {
            "truncate": True, "raw_scores": True, "truncation_direction": "right",
        })
        with self.assertRaises(SetupError):
            self.resolve_rerank("cohere_rerank", "https://api.cohere.com/v2/rerank")

    def test_provider_failures_do_not_expose_raw_error(self):
        def fail():
            raise RuntimeError("https://secret.test key=secret response-body")
        self.chat.get_current_key = fail
        with self.assertRaises(SetupError) as caught:
            self.catalog.resolve(self.selection)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("response-body", str(caught.exception))
        option = self.catalog.options()["chat"][0]
        self.assertFalse(option["supported"])
        self.assertNotIn("secret", option["reason"])

    def test_missing_model_and_disabled_provider_never_use_config_fallback(self):
        self.chat.model = ""
        self.chat.provider_config["model"] = "tempting-fallback"
        with self.assertRaises(SetupError):
            self.catalog.resolve(self.selection)
        self.assertFalse(self.catalog.options()["chat"][0]["supported"])
        self.chat.model = "actual"
        self.chat.provider_config["enable"] = False
        with self.assertRaises(SetupError):
            self.catalog.resolve(self.selection)


class CheckedProviderTests(unittest.IsolatedAsyncioTestCase):
    setUp = ProviderTests.setUp

    async def test_zero_dimensions_are_selectable_and_detected_only_on_install(self):
        self.embedding.provider_config["embedding_dimensions"] = 0
        self.embedding.get_embedding = AsyncMock(return_value=[0.5, -0.25, 1.0])
        option = self.catalog.options()["embedding"][0]
        self.assertTrue(option["supported"])
        self.assertEqual(option["reason"], "安装时自动检测嵌入维数")
        self.embedding.get_embedding.assert_not_called()
        with self.assertRaises(SetupError):
            self.catalog.resolve(self.selection)
        env = await self.catalog.resolve_checked(self.selection)
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"], "3")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE"], "10")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL"], "text-embedding-v4")
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"], "embedding-secret")
        self.embedding.get_embedding.assert_awaited_once_with("连接检查")
        self.assertEqual(self.embedding.get_dim(), 0)

    async def test_declared_dimensions_must_match_actual_output(self):
        self.embedding.provider_config["embedding_dimensions"] = 2
        self.embedding.get_embedding = AsyncMock(return_value=[0.5, 0.25])
        env = await self.catalog.resolve_checked(self.selection)
        self.assertEqual(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"], "2")
        self.embedding.get_embedding.return_value = [0.5]
        with self.assertRaisesRegex(SetupError, "维数.*不一致"):
            await self.catalog.resolve_checked(self.selection)

        async def cleared_dimension(text):
            self.embedding.provider_config["embedding_dimensions"] = 0
            return [0.5]

        self.embedding.get_embedding = cleared_dimension
        with self.assertRaisesRegex(SetupError, "维数.*不一致"):
            await self.catalog.resolve_checked(self.selection)

    async def test_invalid_vectors_are_rejected_without_echoing_response(self):
        self.embedding.provider_config["embedding_dimensions"] = 0
        for vector in (
            [], None, "response-secret", {"secret": 1}, [float("nan")],
            [float("inf")], [float("-inf")], [True], ["response-secret"],
            [[0.5]], [None], [10 ** 1000],
        ):
            self.embedding.get_embedding = AsyncMock(return_value=vector)
            with self.subTest(vector_type=type(vector).__name__):
                with self.assertRaisesRegex(SetupError, "向量无效") as caught:
                    await self.catalog.resolve_checked(self.selection)
                self.assertNotIn("response-secret", str(caught.exception))

    async def test_probe_errors_are_sanitized_including_provider_setup_errors(self):
        for error in (
            RuntimeError("https://secret.test key=secret response-body"),
            SetupError("https://secret.test key=secret response-body"),
            TimeoutError("https://secret.test key=secret response-body"),
        ):
            self.embedding.get_embedding = AsyncMock(side_effect=error)
            with self.subTest(error_type=type(error).__name__):
                with self.assertRaises(SetupError) as caught:
                    await self.catalog.resolve_checked(self.selection)
                for secret in ("https://", "secret", "response-body"):
                    self.assertNotIn(secret, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    async def test_probe_timeout_cancels_request_and_caller_cancel_propagates(self):
        cancelled = asyncio.Event()

        async def wait_forever(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.embedding.get_embedding = wait_forever
        with patch(
            "plugins.astrbot_plugin_hindsight_bridge.core.providers._EMBEDDING_TIMEOUT",
            0.01,
        ):
            with self.assertRaisesRegex(SetupError, "超时"):
                await self.catalog.resolve_checked(self.selection)
        self.assertTrue(cancelled.is_set())
        self.embedding.get_embedding = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.catalog.resolve_checked(self.selection)

    async def test_incompatible_or_wrong_selection_never_calls_embedding(self):
        self.embedding.provider_config["embedding_dimensions"] = 0
        self.embedding.get_embedding = AsyncMock(return_value=[0.5])
        self.embedding.provider_config["custom_headers"] = {"X-Key": "secret\ninvalid"}
        self.assertFalse(self.catalog.options()["embedding"][0]["supported"])
        with self.assertRaises(SetupError):
            await self.catalog.resolve_checked(self.selection)
        self.embedding.provider_config.pop("custom_headers")
        with self.assertRaises(SetupError):
            await self.catalog.resolve_checked({**self.selection, "embedding_provider_id": "chat"})
        self.embedding.get_embedding.assert_not_called()

    async def test_configuration_change_during_probe_requires_retry(self):
        async def changed(text):
            self.embedding.model = "different-model"
            return [0.5]

        self.embedding.provider_config["embedding_dimensions"] = 0
        self.embedding.get_embedding = changed
        with self.assertRaisesRegex(SetupError, "配置发生变化"):
            await self.catalog.resolve_checked(self.selection)


if __name__ == "__main__":
    unittest.main()
