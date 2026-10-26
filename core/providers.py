"""Reuse public AstrBot 4.29.1 provider APIs for Hindsight 1.1 setup.

The resolve methods return secrets for backend configuration, never UI or logs.
Only ``resolve_checked`` makes a provider request to verify embedding output.
"""

import asyncio
from collections.abc import Mapping
import math
from numbers import Real
import re
from urllib.parse import urlsplit


class SetupError(ValueError):
    """A user-facing setup error with no upstream response or credentials."""


_KINDS = {"chat": "chat_completion", "embedding": "embedding", "rerank": "rerank"}
_ALIBABA = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
_EMBEDDING_TIMEOUT = 15
_DIMENSIONS_ENV = "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"


def _text(value, message):
    if not isinstance(value, str) or not value.strip():
        raise SetupError(message)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SetupError(message)
    return value.strip()


def _url(value):
    value = _text(value, "供应商地址为空或格式无效，请在 AstrBot 中修正。")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and not any(char.isspace() for char in value)
        )
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise SetupError("供应商地址格式不兼容，请使用不含认证信息和查询参数的 HTTP 地址。")
    return value.rstrip("/")


def _config(provider):
    config = provider.provider_config
    if not isinstance(config, Mapping):
        raise SetupError("无法读取供应商配置，请在 AstrBot 中重新保存供应商。")
    if config.get("enable") is False:
        raise SetupError("所选供应商已停用，请先在 AstrBot 中启用。")
    return config


def _common(config):
    # These affect transport or request semantics and cannot be silently dropped.
    for field, message in (
        ("proxy", "供应商使用了自定义代理，暂不能直接复用，请选择无需代理的供应商。"),
        ("custom_headers", "供应商使用了自定义请求头，暂不能直接复用。"),
        ("custom_extra_body", "供应商使用了自定义请求参数，暂不能直接复用。"),
        ("api_version", "Azure 专用接口暂不能作为通用对话接口复用。"),
    ):
        if config.get(field):
            raise SetupError(message)


class ProviderCatalog:
    """List safe options and resolve explicit selections without model fallback.

    ``options()`` returns chat/embedding/rerank lists of dictionaries containing
    id, label, type, model, supported and reason. Incompatible entries remain
    visible with a Chinese explanation. ``resolve(selection)`` returns a fresh
    dict[str, str] of HINDSIGHT_API_* variables, including credentials. Selection
    keys are chat_provider_id, embedding_provider_id and optional
    rerank_provider_id. The latter may be absent, None or "" to select RRF.
    ``await resolve_checked(selection)`` probes the chosen embedding instance
    and can detect dimensions when get_dim() is zero; resolve() requires a
    declared positive dimension and never makes network requests.
    """

    def __init__(self, context):
        self.context = context

    def _providers(self):
        found = {"chat": [], "embedding": [], "rerank": []}
        found["chat"] = list(self.context.get_all_providers())
        found["embedding"] = list(self.context.get_all_embedding_providers())
        # AstrBot has no public get_all_rerank_providers in this version.
        configs = self.context.get_config().get("provider", [])
        if not isinstance(configs, list):
            raise SetupError("AstrBot 供应商列表格式无效，请重新保存模型配置。")
        for config in configs:
            if not isinstance(config, Mapping):
                continue
            provider_id = config.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                meta = provider.meta()
                kind = getattr(meta.provider_type, "value", meta.provider_type)
                if kind == "rerank":
                    found["rerank"].append(provider)
        return found

    @staticmethod
    def _meta(provider, kind):
        meta = provider.meta()
        capability = getattr(meta.provider_type, "value", meta.provider_type)
        if capability != _KINDS[kind]:
            raise SetupError("所选供应商类型不匹配，请分别选择对话、嵌入和重排模型。")
        _text(meta.id, "供应商缺少有效标识，请重新保存模型配置。")
        return meta

    def options(self):
        try:
            result = {}
            for kind, providers in self._providers().items():
                result[kind] = []
                seen = set()
                for provider in providers:
                    meta = self._meta(provider, kind)
                    if meta.id in seen:
                        continue
                    seen.add(meta.id)
                    # Only explicit display fields leave the catalog.
                    option = {
                        "id": meta.id,
                        "label": meta.id,
                        "type": meta.type,
                        "model": "",
                        "supported": True,
                        "reason": "",
                    }
                    try:
                        config = _config(provider)
                        option["model"] = self._model(provider, config, kind)
                        env = self._environment(
                            provider, kind, allow_unknown_dimensions=True
                        )
                        if kind == "embedding" and _DIMENSIONS_ENV not in env:
                            option["reason"] = "安装时自动检测嵌入维数"
                    except SetupError as error:
                        option.update(supported=False, reason=str(error))
                    except Exception:
                        option.update(
                            supported=False,
                            reason="无法读取供应商配置，请在 AstrBot 中检查模型设置。",
                        )
                    result[kind].append(option)
            return result
        except SetupError:
            raise
        except Exception:
            raise SetupError("无法读取 AstrBot 模型列表，请检查供应商是否已成功加载。") from None

    def resolve(self, selection):
        return self._resolve(self._selection(selection))

    async def resolve_checked(self, selection):
        """Verify embedding output before producing installation credentials."""
        providers = self._selection(selection)
        before = self._resolve(providers, allow_unknown_dimensions=True)
        try:
            async with asyncio.timeout(_EMBEDDING_TIMEOUT):
                vector = await providers["embedding"].get_embedding("连接检查")
        except TimeoutError:
            raise SetupError("嵌入模型连接检查超时，请检查供应商服务后重试。") from None
        except Exception:
            raise SetupError("嵌入模型连接检查失败，请检查 AstrBot 中的供应商配置后重试。") from None
        try:
            valid = (
                isinstance(vector, (list, tuple))
                and len(vector) > 0
                and all(
                    not isinstance(value, bool)
                    and isinstance(value, Real)
                    and math.isfinite(float(value))
                    for value in vector
                )
            )
        except Exception:
            valid = False
        if not valid:
            raise SetupError("嵌入模型返回的向量无效，需要非空且每项都是有限数值。")
        if _DIMENSIONS_ENV in before and int(before[_DIMENSIONS_ENV]) != len(vector):
            raise SetupError("嵌入模型实际返回的维数与 AstrBot 配置不一致，请修正配置后重试。")
        env = self._resolve(providers, embedding_dimensions=len(vector))
        # A model/configuration switch during the request invalidates the probe.
        if {k: v for k, v in before.items() if k != _DIMENSIONS_ENV} != {
            k: v for k, v in env.items() if k != _DIMENSIONS_ENV
        }:
            raise SetupError("检查期间模型配置发生变化，请重新执行连接检查。")
        return env

    def _selection(self, selection):
        keys = {kind + "_provider_id" for kind in _KINDS}
        if not isinstance(selection, Mapping) or set(selection) - keys:
            raise SetupError("模型选择格式无效，请重新选择供应商。")
        providers = {}
        for kind in _KINDS:
            provider_id = selection.get(kind + "_provider_id")
            if kind == "rerank" and (provider_id is None or provider_id == ""):
                providers[kind] = None
                continue
            provider_id = _text(provider_id, "请选择有效的供应商，重排模型可以留空。")
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is None:
                    raise SetupError("所选供应商不存在或尚未加载，请重新选择。")
                self._meta(provider, kind)
                providers[kind] = provider
            except SetupError:
                raise
            except Exception:
                raise SetupError("无法读取所选供应商，请在 AstrBot 中检查模型配置。") from None
        return providers

    def _resolve(self, providers, *, embedding_dimensions=None, allow_unknown_dimensions=False):
        env = {}
        for kind, provider in providers.items():
            if provider is None:
                env["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"
                continue
            try:
                env.update(self._environment(
                    provider, kind, dimensions=embedding_dimensions,
                    allow_unknown_dimensions=allow_unknown_dimensions,
                ))
            except SetupError:
                raise
            except Exception:
                raise SetupError("无法读取所选供应商，请在 AstrBot 中检查模型配置。") from None
        return env

    @staticmethod
    def _model(provider, config, kind):
        if kind == "chat":
            model = provider.get_model()
        else:
            # The official embedding and vLLM rerank adapters send self.model,
            # whereas AbstractProvider.get_model() may still return "".
            model = getattr(provider, "model", None)
            if model is None:
                model = provider.get_model()
        return _text(model, "供应商缺少有效模型名称，请在 AstrBot 中明确选择模型。")

    def _environment(self, provider, kind, *, dimensions=None, allow_unknown_dimensions=False):
        meta = self._meta(provider, kind)
        config = _config(provider)
        _common(config)
        model = self._model(provider, config, kind)
        if kind == "chat":
            if meta.type == "openai_responses":
                raise SetupError("Responses 接口暂不能复用，请选择 Chat Completions 类型供应商。")
            if meta.type != "openai_chat_completion":
                raise SetupError("此对话供应商尚不兼容，请选择 OpenAI 兼容的 Chat Completions 供应商。")
            if "api_version" in config:
                raise SetupError("Azure 专用接口暂不能作为通用对话接口复用。")
            base = _url(config.get("api_base") or "https://api.openai.com/v1")
            if urlsplit(base).path.rstrip("/").endswith(("/responses", "/chat/completions")):
                raise SetupError("对话地址必须是 API 根地址，不能是 Responses 或单次请求地址。")
            key = _text(provider.get_current_key(), "对话供应商缺少可用密钥，请先在 AstrBot 中配置。")
            return {
                "HINDSIGHT_API_LLM_PROVIDER": "openai",
                "HINDSIGHT_API_LLM_MODEL": model,
                "HINDSIGHT_API_LLM_BASE_URL": base,
                "HINDSIGHT_API_LLM_API_KEY": key,
            }
        if kind == "embedding":
            if meta.type != "openai_embedding":
                raise SetupError("此嵌入供应商尚不兼容，请选择 OpenAI Embedding 类型供应商。")
            dim = provider.get_dim()
            if type(dim) is not int or dim < 0:
                raise SetupError("嵌入维度配置无效，请在 AstrBot 中修正。")
            if dimensions is not None:
                if type(dimensions) is not int or dimensions <= 0:
                    raise SetupError("检测到的嵌入维度无效，请重新执行连接检查。")
                if dim > 0 and dim != dimensions:
                    raise SetupError("嵌入模型实际返回的维数与 AstrBot 配置不一致，请修正配置后重试。")
                dim = dimensions
            if dim == 0 and not allow_unknown_dimensions:
                raise SetupError("嵌入维度必须是非零正整数，请先在 AstrBot 中设置模型的实际维度。")
            mode = config.get("embedding_dimensions_mode", "auto")
            if mode not in {"auto", "always", "never"}:
                raise SetupError("嵌入维度发送模式无效，请在 AstrBot 中修正。")
            if mode == "never":
                raise SetupError("Hindsight 会发送嵌入维度参数，无法复用禁止发送维度的供应商。")
            base = _url(config.get("embedding_api_base", "https://api.openai.com/v1"))
            base = base.removesuffix("/embeddings")
            if not re.search(r"/v\d+$", base):
                base += "/v1"
            env = {
                "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
                "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": model,
                "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": base,
                "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": _text(
                    config.get("embedding_api_key"), "嵌入供应商缺少密钥，请先在 AstrBot 中配置。"
                ),
                "HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE": "10",
            }
            if dim:
                env[_DIMENSIONS_ENV] = str(dim)
            return env
        if meta.type not in {"bailian_rerank", "vllm_rerank"}:
            raise SetupError("此重排供应商尚不兼容，请选择已支持的供应商或留空使用 RRF。")
        base = _url(config.get(
            "rerank_api_base",
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
            if meta.type == "bailian_rerank" else "http://127.0.0.1:8000",
        ))
        if meta.type == "vllm_rerank":
            suffix = config.get("rerank_api_suffix", "/v1/rerank")
            if suffix is None:
                suffix = "/v1/rerank"
            if not isinstance(suffix, str):
                raise SetupError("重排请求路径无效，请在 AstrBot 中修正。")
            base = _url(base + ("/" + suffix.lstrip("/") if suffix else ""))
        if config.get("instruct"):
            raise SetupError("带自定义指令的重排暂不能直接复用，请清除指令或留空使用 RRF。")
        if base == _ALIBABA:
            if meta.type == "bailian_rerank" and model.strip().lower() != "qwen3-rerank":
                raise SetupError("此百炼模型在 AstrBot 中使用不同请求协议，暂不能直接复用。")
            backend = "alibaba"
        elif meta.type == "vllm_rerank" and base in {
            "https://api.siliconflow.cn/v1/rerank",
            "https://api.siliconflow.com/v1/rerank",
        }:
            backend = "siliconflow"
        elif meta.type == "vllm_rerank" and base in {
            "https://api.cohere.com/v1/rerank", "https://api.cohere.com/v2/rerank",
            "https://api.cohere.ai/v1/rerank", "https://api.cohere.ai/v2/rerank",
        }:
            backend = "cohere"
        else:
            raise SetupError("此重排地址或协议无法准确复用；支持百炼兼容接口、硅基流动和 Cohere 官方接口，也可留空使用 RRF。")
        key = config.get("rerank_api_key")
        if not key and meta.type == "bailian_rerank":
            key = getattr(provider, "api_key", None)
        prefix = "HINDSIGHT_API_RERANKER_" + backend.upper() + "_"
        env = {
            "HINDSIGHT_API_RERANKER_PROVIDER": backend,
            prefix + "API_KEY": _text(key, "重排供应商缺少密钥，请先在 AstrBot 中配置。"),
            prefix + "MODEL": model,
        }
        if backend != "alibaba":
            # Hindsight 1.1 SiliconFlowCrossEncoder appends "/rerank" to its
            # base_url; Cohere's custom base_url is the complete request URL.
            env[prefix + "BASE_URL"] = base.removesuffix("/rerank") if backend == "siliconflow" else base
        return env
