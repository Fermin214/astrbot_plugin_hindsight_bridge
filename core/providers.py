"""Reuse public AstrBot 4.29.1 provider APIs for Hindsight 1.1 setup.

The resolve methods return secrets for backend configuration, never UI or logs.
Only ``resolve_checked`` makes a provider request to verify embedding output.
"""

import asyncio
from collections.abc import Mapping
import json
import math
from numbers import Real
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


class SetupError(ValueError):
    """A user-facing setup error with no upstream response or credentials."""


_KINDS = {"chat": "chat_completion", "embedding": "embedding", "rerank": "rerank"}
_ALIBABA = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
_EMBEDDING_TIMEOUT = 15
_DIMENSIONS_ENV = "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"
_TRANSPORT = "http://memory-models:8765"
_TRANSPORT_KEY = "__ASTRBOT_MEMORY_TRANSPORT_KEY__"
_CHAT_OPENAI = {
    "openai_chat_completion", "groq_chat_completion", "openrouter_chat_completion",
    "longcat_chat_completion", "mirarouter_chat_completion", "aihubmix_chat_completion",
    "ssycloud_chat_completion", "xai_chat_completion", "xiaomi_chat_completion",
    "zhipu_chat_completion",
}


def _text(value, message):
    if not isinstance(value, str) or not value.strip():
        raise SetupError(message)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SetupError(message)
    return value.strip()


def _url(value, *, query=False):
    value = _text(value, "供应商地址为空或格式无效，请在 AstrBot 中修正。")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
            and (query or not parts.query)
            and not parts.fragment
            and not any(char.isspace() for char in value)
        )
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise SetupError("供应商地址格式无效，请使用不含内嵌账号密码的 HTTP(S) 地址。")
    return value.rstrip("/")


def _config(provider):
    config = provider.provider_config
    if not isinstance(config, Mapping):
        raise SetupError("无法读取供应商配置，请在 AstrBot 中重新保存供应商。")
    if config.get("enable") is False:
        raise SetupError("所选供应商已停用，请先在 AstrBot 中启用。")
    return config


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise SetupError("供应商请求参数必须是有效的 JSON 对象。") from None


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SetupError("供应商请求参数必须是有效的 JSON 对象。")
    _json(value)
    return dict(value)


def _transport(provider, config, *, protocol, base, model, key):
    headers = _object(getattr(provider, "request_headers", None))
    headers.update(_object(config.get("custom_headers")))
    for name, value in headers.items():
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
            raise SetupError("供应商请求头名称无效。")
        _text(value, "供应商请求头必须是不含换行的字符串。")
    proxy = config.get("proxy") or ""
    if proxy:
        proxy = _url(proxy)
    timeout = config.get("timeout", 60)
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SetupError("供应商超时必须是正数。") from None
    return {
        "protocol": protocol, "base_url": base, "model": model, "api_key": key,
        "headers": headers, "proxy": proxy, "timeout": timeout,
    }


def _bridge(kind, descriptor):
    return {"ASTRBOT_MEMORY_MODEL_CONFIG": _json({kind: descriptor})}


def _without_dimensions(environment):
    result = {k: v for k, v in environment.items() if k != _DIMENSIONS_ENV}
    if "ASTRBOT_MEMORY_MODEL_CONFIG" in result:
        models = json.loads(result["ASTRBOT_MEMORY_MODEL_CONFIG"])
        models.get("embedding", {}).pop("dimensions", None)
        result["ASTRBOT_MEMORY_MODEL_CONFIG"] = _json(models)
    if "ASTRBOT_MEMORY_EMBEDDING_IDENTITY" in result:
        identity = json.loads(result["ASTRBOT_MEMORY_EMBEDDING_IDENTITY"])
        identity.pop(_DIMENSIONS_ENV, None)
        result["ASTRBOT_MEMORY_EMBEDDING_IDENTITY"] = _json(identity)
    return result


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
                        option["model"] = (
                            "" if meta.type == "tei_rerank"
                            else self._model(provider, config, kind)
                        )
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
        if _without_dimensions(before) != _without_dimensions(env):
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
        transports = {}
        for kind, provider in providers.items():
            if provider is None:
                env["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"
                continue
            try:
                resolved = self._environment(
                    provider, kind, dimensions=embedding_dimensions,
                    allow_unknown_dimensions=allow_unknown_dimensions,
                )
                transports.update(json.loads(resolved.pop("ASTRBOT_MEMORY_MODEL_CONFIG", "{}")))
                env.update(resolved)
            except SetupError:
                raise
            except Exception:
                raise SetupError("无法读取所选供应商，请在 AstrBot 中检查模型配置。") from None
        if transports:
            env["ASTRBOT_MEMORY_MODEL_CONFIG"] = _json(transports)
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
        model = "" if meta.type == "tei_rerank" else self._model(provider, config, kind)
        if kind == "chat":
            return self._chat(provider, meta, config, model)
        if kind == "embedding":
            return self._embedding(
                provider, meta, config, model, dimensions, allow_unknown_dimensions
            )
        return self._rerank(provider, meta, config, model)

    def _chat(self, provider, meta, config, model):
        if meta.type in _CHAT_OPENAI:
            protocol, default = "openai", "https://api.openai.com/v1"
        elif meta.type == "openai_responses":
            protocol, default = "openai-responses", "https://api.openai.com/v1"
        elif meta.type in {"anthropic_chat_completion", "kimi_code_chat_completion"}:
            protocol, default = "anthropic", "https://api.anthropic.com"
        elif meta.type == "googlegenai_chat_completion":
            protocol, default = "gemini", "https://generativelanguage.googleapis.com"
        else:
            raise SetupError("此对话适配器未实现；支持 OpenAI Chat、Responses、Anthropic 和 Gemini 协议。")
        runtime_base = getattr(getattr(provider, "client", None), "base_url", None)
        base = _url(str(runtime_base or config.get("api_base") or default), query=True)
        if urlsplit(base).path.rstrip("/").endswith(("/responses", "/chat/completions", "/messages")):
            raise SetupError("对话地址必须是 API 根地址，不能是单次请求地址。")
        key = _text(provider.get_current_key(), "对话供应商缺少可用密钥，请先在 AstrBot 中配置。")
        descriptor = _transport(provider, config, protocol=protocol, base=base, model=model, key=key)
        extra = _object(config.get("custom_extra_body"))
        if set(extra) & {"model", "messages", "input", "stream", "tools", "tool_choice", "response_format"}:
            raise SetupError("自定义请求参数覆盖了模型、输入或结构化输出字段，无法用于记忆处理。")
        headers = descriptor["headers"]
        if config.get("api_version"):
            if protocol not in {"openai", "openai-responses"}:
                raise SetupError("api_version 仅适用于 Azure OpenAI 协议。")
            parts = urlsplit(base)
            path = parts.path.rstrip("/")
            if "/deployments/" not in path:
                path += "/deployments/" + quote(model, safe="")
            query = dict(parse_qsl(parts.query))
            query["api-version"] = _text(config["api_version"], "Azure API 版本无效。")
            base = urlunsplit(parts._replace(path=path, query=urlencode(query)))
            headers["api-key"] = key
        if protocol == "anthropic":
            base = base.removesuffix("/v1")
            thinking = _object(config.get("anth_thinking_config"))
            if thinking.get("type") == "adaptive":
                extra.setdefault("thinking", {"type": "adaptive"})
                if thinking.get("effort"):
                    extra.setdefault("output_config", {"effort": thinking["effort"]})
            elif not thinking.get("type") and thinking.get("budget"):
                extra.setdefault("thinking", {"type": "enabled", "budget_tokens": thinking["budget"]})
        env = {
            "HINDSIGHT_API_LLM_PROVIDER": protocol,
            "HINDSIGHT_API_LLM_MODEL": model,
            "HINDSIGHT_API_LLM_BASE_URL": base,
            "HINDSIGHT_API_LLM_API_KEY": key,
            "HINDSIGHT_API_LLM_TIMEOUT": str(descriptor["timeout"]),
        }
        if protocol == "gemini":
            thinking = _object(config.get("gm_thinking_config"))
            if model.startswith("gemini-2.5"):
                extra.setdefault("thinking_config", {"thinking_budget": thinking.get("budget", 0)})
            elif model.startswith(("gemini-3-", "gemini-3.")):
                extra.setdefault("thinking_config", {"thinking_level": thinking.get("level", "HIGH")})
            # Gemini's Hindsight client accepts the endpoint per request.
            # Relay preserves AstrBot's endpoint, headers and per-provider proxy.
            descriptor["base_url"] = base
            env.update(_bridge("chat", descriptor))
            http = _object(extra.get("http_options"))
            http.update(base_url=_TRANSPORT + "/chat", headers={"X-Astrbot-Transport-Key": _TRANSPORT_KEY})
            extra["http_options"] = http
            safety = _object(config.get("gm_safety_settings"))
            categories = {
                "harassment": "HARM_CATEGORY_HARASSMENT", "hate_speech": "HARM_CATEGORY_HATE_SPEECH",
                "sexually_explicit": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "dangerous_content": "HARM_CATEGORY_DANGEROUS_CONTENT",
            }
            if safety:
                env["HINDSIGHT_API_LLM_GEMINI_SAFETY_SETTINGS"] = _json([
                    {"category": category, "threshold": safety[name]}
                    for name, category in categories.items() if name in safety
                ])
        elif descriptor["proxy"]:
            descriptor["base_url"] = base
            env.update(_bridge("chat", descriptor))
            env["HINDSIGHT_API_LLM_BASE_URL"] = _TRANSPORT + "/chat"
            headers = {**headers, "X-Astrbot-Transport-Key": _TRANSPORT_KEY}
        if config.get("ollama_disable_thinking") and config.get("provider") == "ollama":
            extra.setdefault("think", False)
        if headers and protocol != "gemini":
            env["HINDSIGHT_API_LLM_DEFAULT_HEADERS"] = _json(headers)
        if extra:
            env["HINDSIGHT_API_LLM_EXTRA_BODY"] = _json(extra)
        return env

    def _embedding(self, provider, meta, config, model, dimensions, allow_unknown_dimensions):
        if meta.type not in {"openai_embedding", "dashscope_embedding", "gemini_embedding", "ollama_embedding"}:
            raise SetupError("此嵌入适配器未实现；支持 OpenAI、百炼 DashScope、Gemini 和 Ollama。")
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
        defaults = {
            "openai_embedding": "https://api.openai.com/v1",
            "dashscope_embedding": "https://dashscope.aliyuncs.com/api/v1",
            "gemini_embedding": "https://generativelanguage.googleapis.com",
            "ollama_embedding": "http://localhost:11434",
        }
        base = _url(config.get("embedding_api_base") or defaults[meta.type])
        if meta.type == "openai_embedding":
            base = base.removesuffix("/embeddings")
            if not re.search(r"/v\d+$", base):
                base += "/v1"
        elif meta.type == "ollama_embedding":
            base = base.removesuffix("/api/embed")
        elif meta.type == "dashscope_embedding" and (
            "vl-embedding" in model.lower() or "multimodal" in model.lower()
        ):
            raise SetupError("多模态嵌入需图文输入适配，请选择文本嵌入模型。")
        key = (
            getattr(provider, "api_key", None) if meta.type == "dashscope_embedding"
            else config.get("embedding_api_key")
        )
        if key is None:
            key = config.get("embedding_api_key")
        if meta.type != "ollama_embedding":
            key = _text(key, "嵌入供应商缺少密钥，请先在 AstrBot 中配置。")
        descriptor = _transport(
            provider, config, protocol=meta.type.removesuffix("_embedding"),
            base=base, model=model, key=key or "",
        )
        descriptor["dimensions"] = dim
        if meta.type == "openai_embedding":
            parts = urlsplit(base)
            name = model.lower().rsplit("/", 1)[-1]
            send_dimensions = "embedding_dimensions" in config and (
                mode == "always" or (mode == "auto" and parts.scheme == "https" and (
                    (parts.hostname == "api.openai.com" and parts.path == "/v1" and name.startswith("text-embedding-3"))
                    or (parts.hostname == "api.siliconflow.cn" and name.startswith("qwen"))
                ))
            )
        else:
            send_dimensions = meta.type != "ollama_embedding" or bool(config.get("embedding_dimensions"))
        descriptor["send_dimensions"] = send_dimensions
        descriptor["extra_body"] = _object(config.get("custom_extra_body"))
        if set(descriptor["extra_body"]) & {"model", "input", "dimensions"}:
            raise SetupError("嵌入请求参数覆盖了模型、输入或维度，请修正供应商配置。")
        env = {
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": model,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": base,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": key or "local",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE": "10",
        }
        if dim:
            env[_DIMENSIONS_ENV] = str(dim)
        identity = {
            k: v for k, v in env.items()
            if not k.endswith(("_API_KEY", "_BATCH_SIZE"))
        }
        if meta.type != "openai_embedding":
            identity["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] = meta.type
        if descriptor["extra_body"]:
            identity["HINDSIGHT_API_EMBEDDINGS_REQUEST_PARAMETERS"] = _json(descriptor["extra_body"])
        if (
            meta.type != "openai_embedding" or not send_dimensions
            or descriptor["proxy"] or config.get("custom_headers")
            or descriptor["extra_body"]
        ):
            env.update(_bridge("embedding", descriptor))
            env["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"] = _TRANSPORT + "/v1"
            env["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] = _TRANSPORT_KEY
            env["ASTRBOT_MEMORY_EMBEDDING_IDENTITY"] = _json(identity)
        return env

    def _rerank(self, provider, meta, config, model):
        if meta.type not in {"bailian_rerank", "vllm_rerank", "tei_rerank"}:
            raise SetupError("此重排适配器未实现；支持百炼、Cohere 兼容接口和 TEI，也可留空。")
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
        key = config.get("rerank_api_key")
        if not key and meta.type == "bailian_rerank":
            key = getattr(provider, "api_key", None)
        use_bridge = bool(
            config.get("proxy") or config.get("custom_headers")
            or config.get("instruct") or config.get("custom_extra_body")
            or meta.type == "tei_rerank" or not key
        )
        if base == _ALIBABA and not use_bridge:
            if meta.type == "bailian_rerank" and model.strip().lower() != "qwen3-rerank":
                raise SetupError("此百炼模型在 AstrBot 中使用不同请求协议，暂不能直接复用。")
            backend = "alibaba"
        elif not use_bridge and meta.type == "vllm_rerank" and base in {
            "https://api.siliconflow.cn/v1/rerank",
            "https://api.siliconflow.com/v1/rerank",
        }:
            backend = "siliconflow"
        elif not use_bridge and meta.type == "vllm_rerank" and key:
            backend = "cohere"
        else:
            protocol = "cohere"
            if meta.type == "bailian_rerank":
                protocol = (
                    "cohere" if model.lower() == "qwen3-rerank" and urlsplit(base).path.endswith((
                        "/compatible-api/v1/reranks", "/compatible-mode/v1/reranks"
                    )) else "bailian_native"
                )
            elif meta.type == "tei_rerank":
                protocol, base = "tei", base + "/rerank"
            if meta.type == "bailian_rerank":
                key = _text(key, "重排供应商缺少密钥，请先在 AstrBot 中配置。")
            descriptor = _transport(
                provider, config, protocol=protocol, base=base, model=model, key=key or ""
            )
            descriptor.update(
                instruct=config.get("instruct") or "",
                return_documents=bool(config.get("return_documents")),
                extra_body=_object(config.get("custom_extra_body")),
                tei_options={},
            )
            if meta.type == "tei_rerank":
                for name in ("truncate", "raw_scores", "return_text"):
                    if config.get("tei_rerank_" + name):
                        descriptor["tei_options"][name] = True
                if descriptor["tei_options"].get("truncate"):
                    descriptor["tei_options"]["truncation_direction"] = config.get(
                        "tei_rerank_truncation_direction", "right"
                    ).lower()
            return {
                **_bridge("rerank", descriptor),
                "HINDSIGHT_API_RERANKER_PROVIDER": "cohere",
                "HINDSIGHT_API_RERANKER_COHERE_API_KEY": _TRANSPORT_KEY,
                "HINDSIGHT_API_RERANKER_COHERE_MODEL": model or "tei",
                "HINDSIGHT_API_RERANKER_COHERE_BASE_URL": _TRANSPORT + "/rerank",
                "HINDSIGHT_API_RERANKER_COHERE_TIMEOUT": str(descriptor["timeout"] + 5),
            }
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
