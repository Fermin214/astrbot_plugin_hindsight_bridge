"""Local model protocol transport for Hindsight, using only the standard library."""

from collections.abc import Mapping
import functools
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PORT = 8765
MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
_ERROR = "Model transport failed"
_CHAT_HEADERS = {
    "authorization", "api-key", "anthropic-version", "x-api-key", "content-type",
    "x-goog-api-key",
}


class TransportError(ValueError):
    """A transport failure safe to return to callers."""


def _safe(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise TransportError(_ERROR) from None
    return call


def _reject_constant(value):
    raise ValueError("Invalid JSON number")


def _loads(body):
    return json.loads(body, parse_constant=_reject_constant)


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object(value):
    if not isinstance(value, Mapping):
        raise ValueError("Expected object")
    return value


def _positive_int(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid dimension")
    return value


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid number")
    return value


def _texts(value):
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Expected text input")
    return value


def _url(base, suffix=""):
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"} or not parts.hostname
        or parts.username is not None or parts.password is not None or parts.fragment
        or any(char.isspace() or ord(char) < 32 for char in base)
    ):
        raise ValueError("Invalid URL")
    parts.port
    tail = urlsplit(suffix)
    if tail.scheme or tail.netloc or tail.fragment:
        raise ValueError("Invalid suffix")
    path = parts.path
    if tail.path:
        path = path.rstrip("/") + "/" + tail.path.lstrip("/")
    query = "&".join(value for value in (parts.query, tail.query) if value)
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _headers(config, incoming=None, *, auth="authorization"):
    headers = {"content-type": "application/json"}
    if incoming is not None:
        headers.update({
            name.lower(): value for name, value in incoming.items()
            if name.lower() in _CHAT_HEADERS
        })
    key = config.get("api_key", "")
    if not isinstance(key, str):
        raise ValueError("Invalid key")
    if key:
        for name in ("authorization", "api-key", "x-api-key", "x-goog-api-key"):
            headers.pop(name, None)
        headers[auth] = "Bearer " + key if auth == "authorization" else key
    custom = _object(config.get("headers", {}))
    for name, value in custom.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Invalid header")
        headers[name.lower()] = value
    for name, value in headers.items():
        if not name or any(char in name + value for char in "\r\n"):
            raise ValueError("Invalid header")
    return headers


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(config, url, headers, body):
    proxy = config.get("proxy", "")
    if not isinstance(proxy, str):
        raise ValueError("Invalid proxy")
    if proxy:
        _url(proxy)
    timeout = _finite(config.get("timeout", 30))
    if timeout <= 0:
        raise ValueError("Invalid timeout")
    opener = build_opener(
        ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        _NoRedirect(),
    )
    request = Request(_url(url), data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Upstream failed")
            result = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        error.close()
        raise ValueError("Upstream failed") from None
    if len(result) > MAX_RESPONSE:
        raise ValueError("Response too large")
    _loads(result)
    return result


def _payload(config, fields):
    body = dict(_object(config.get("extra_body", {})))
    # Descriptor-derived routing fields always win over extra request fields.
    body.update(fields)
    return body


def _vectors(items, count, dimensions, *, vector_key, index_key=None):
    if not isinstance(items, list) or len(items) != count:
        raise ValueError("Wrong vector count")
    ordered = [None] * count
    for position, item in enumerate(items):
        if index_key is None:
            index, vector = position, item
        else:
            item = _object(item)
            index = item.get(index_key)
            if index_key == "text_index" and index is None:
                index = item.get("index")
            vector = item.get(vector_key)
        if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
            raise ValueError("Invalid vector index")
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise ValueError("Wrong vector dimension")
        ordered[index] = [_finite(value) for value in vector]
    return ordered


@_safe
def request_embedding(config, texts):
    """Return OpenAI-shaped embeddings using a single embedding descriptor."""
    _object(config)
    texts = _texts(texts)
    dimensions = _positive_int(config["dimensions"])
    send_dimensions = config.get("send_dimensions", False)
    if type(send_dimensions) is not bool:
        raise ValueError("Invalid dimension policy")
    model, protocol = config["model"], config["protocol"]
    if not isinstance(model, str) or not model:
        raise ValueError("Invalid model")
    base = config["base_url"]
    if protocol == "openai":
        url = _url(base, "embeddings")
        fields = {"model": model, "input": texts}
        if send_dimensions:
            fields["dimensions"] = dimensions
        payload = _payload(config, fields)
        if not send_dimensions:
            payload.pop("dimensions", None)
        headers = _headers(config)
    elif protocol == "dashscope":
        url = _url(base, "services/embeddings/text-embedding/text-embedding")
        payload = _payload(config, {"model": model, "input": {"texts": texts}})
        parameters = dict(_object(payload.get("parameters", {})))
        parameters.pop("dimension", None)
        if send_dimensions:
            parameters["dimension"] = dimensions
        payload["parameters"] = parameters
        headers = _headers(config)
    elif protocol == "gemini":
        parts = urlsplit(base)
        if not parts.path.rstrip("/").endswith(("/v1", "/v1beta", "/v1alpha")):
            base = _url(base, "v1beta")
        model_path = model if model.startswith("models/") else "models/" + model
        if any(char in model_path for char in "?#") or ".." in model_path.split("/"):
            raise ValueError("Invalid model")
        url = _url(base, model_path + ":batchEmbedContents")
        requests = []
        for text in texts:
            entry = {"model": model_path, "content": {"parts": [{"text": text}]}}
            if send_dimensions:
                entry["outputDimensionality"] = dimensions
            requests.append(entry)
        payload = _payload(config, {"requests": requests})
        headers = _headers(config, auth="x-goog-api-key")
    elif protocol == "ollama":
        url = _url(base, "api/embed")
        fields = {"model": model, "input": texts}
        if send_dimensions:
            fields["dimensions"] = dimensions
        payload = _payload(config, fields)
        if not send_dimensions:
            payload.pop("dimensions", None)
        headers = _headers(config)
    else:
        raise ValueError("Unsupported protocol")
    result = _object(_loads(_post(config, url, headers, _dumps(payload))))
    if protocol == "openai":
        vectors = _vectors(
            result["data"], len(texts), dimensions, vector_key="embedding", index_key="index",
        )
    elif protocol == "dashscope":
        vectors = _vectors(
            _object(result["output"])["embeddings"], len(texts), dimensions,
            vector_key="embedding", index_key="text_index",
        )
    elif protocol == "gemini":
        items = result["embeddings"]
        if not isinstance(items, list):
            raise ValueError("Invalid embeddings")
        vectors = _vectors(
            [_object(item)["values"] for item in items], len(texts), dimensions,
            vector_key=None,
        )
    else:
        vectors = _vectors(result["embeddings"], len(texts), dimensions, vector_key=None)
    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
    }


@_safe
def request_rerank(config, query, documents):
    """Return Cohere-shaped reranking results using a rerank descriptor."""
    _object(config)
    if not isinstance(query, str) or not isinstance(documents, list):
        raise ValueError("Invalid rerank input")
    documents = _texts(documents)
    protocol = config["protocol"]
    if protocol == "cohere":
        payload = _payload(config, {
            "model": config["model"], "query": query, "documents": documents,
            "top_n": len(documents),
        })
        instruct = config.get("instruct", "")
        if not isinstance(instruct, str):
            raise ValueError("Invalid rerank instruction")
        payload.pop("instruct", None)
        if instruct:
            payload["instruct"] = instruct
    elif protocol == "bailian_native":
        payload = _payload(config, {
            "model": config["model"], "input": {"query": query, "documents": documents},
        })
        parameters = dict(_object(payload.get("parameters", {})))
        instruct = config.get("instruct", "")
        return_documents = config.get("return_documents", False)
        if not isinstance(instruct, str) or type(return_documents) is not bool:
            raise ValueError("Invalid rerank options")
        parameters["top_n"] = len(documents)
        parameters.pop("instruct", None)
        parameters.pop("return_documents", None)
        if instruct and config["model"].strip().lower() == "qwen3-rerank":
            parameters["instruct"] = instruct
        if return_documents:
            parameters["return_documents"] = True
        payload["parameters"] = parameters
    elif protocol == "tei":
        fields = dict(_object(config.get("tei_options", {})))
        fields.update(query=query, texts=documents)
        payload = _payload(config, fields)
    else:
        raise ValueError("Unsupported protocol")
    result = _loads(_post(
        config, config["base_url"], _headers(config), _dumps(payload),
    ))
    if protocol == "bailian_native":
        items = _object(_object(result)["output"])["results"]
    elif protocol == "tei":
        items = result
    else:
        items = _object(result)["results"]
    if not isinstance(items, list) or len(items) != len(documents):
        raise ValueError("Invalid rerank results")
    seen, results = set(), []
    for item in items:
        item = _object(item)
        index = item["index"]
        if type(index) is not int or not 0 <= index < len(documents) or index in seen:
            raise ValueError("Invalid rerank index")
        seen.add(index)
        results.append({
            "index": index,
            "relevance_score": _finite(item["score" if protocol == "tei" else "relevance_score"]),
        })
    return {"results": results}


@_safe
def forward_chat(config, path, headers, body):
    """Forward raw non-streaming JSON and return the upstream JSON bytes."""
    _object(config)
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        raise ValueError("Expected raw JSON")
    payload = _object(_loads(body))
    if payload.get("stream"):
        raise ValueError("Streaming is unsupported")
    protocol = config.get("protocol", "openai")
    auth = {
        "azure": "api-key", "anthropic": "x-api-key", "gemini": "x-goog-api-key",
    }.get(protocol, "authorization")
    return _post(
        config, _url(config["base_url"], path), _headers(config, headers, auth=auth), body,
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, _dumps({"ok": True}))
        else:
            self._send(404, _dumps({"error": "Not found"}))

    def _authorized(self):
        authorization = self.headers.get("Authorization", "")
        scheme, _, bearer = authorization.partition(" ")
        bearer = bearer if scheme.lower() == "bearer" else ""
        alternate = self.headers.get("X-Astrbot-Transport-Key", "")
        token = self.server.transport_key
        return (
            hmac.compare_digest(bearer.encode("utf-8"), token)
            | hmac.compare_digest(alternate.encode("utf-8"), token)
        )

    def do_POST(self):
        if not self._authorized():
            self._send(401, _dumps({"error": "Unauthorized"}))
            return
        route = urlsplit(self.path).path
        if route not in {"/v1/embeddings", "/rerank"} and not route.startswith("/chat/"):
            self._send(404, _dumps({"error": "Not found"}))
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid body")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            body = _object(_loads(raw))
            config = self.server.model_config
            if route == "/v1/embeddings":
                descriptor = config["embedding"]
                if "dimensions" in body and (
                    type(body["dimensions"]) is not int
                    or body["dimensions"] != descriptor["dimensions"]
                ):
                    raise ValueError("Dimension mismatch")
                result = _dumps(request_embedding(descriptor, body["input"]))
            elif route == "/rerank":
                result = _dumps(request_rerank(config["rerank"], body["query"], body["documents"]))
            else:
                # The gateway credential belongs to this process, never the upstream.
                headers = dict(self.headers.items())
                authorization = self.headers.get("Authorization", "")
                scheme, _, bearer = authorization.partition(" ")
                if scheme.lower() == "bearer" and hmac.compare_digest(
                    bearer.encode("utf-8"), self.server.transport_key,
                ):
                    headers = {
                        name: value for name, value in headers.items()
                        if name.lower() != "authorization"
                    }
                result = forward_chat(config["chat"], self.path[len("/chat/"):], headers, raw)
            self._send(200, result)
        except Exception:
            self._send(502, _dumps({"error": _ERROR}))


class TransportServer(ThreadingHTTPServer):
    """Threaded server with explicit descriptors and no request/error logging."""

    daemon_threads = True

    def __init__(self, address, config, token):
        self.model_config = dict(_object(config))
        if not isinstance(token, str) or not token.strip():
            raise TransportError(_ERROR)
        self.transport_key = token.encode("utf-8")
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address):
        pass


def main():
    try:
        config = _loads(os.environ["ASTRBOT_MEMORY_MODEL_CONFIG"])
        token = os.environ["ASTRBOT_MEMORY_TRANSPORT_KEY"]
        with TransportServer(("0.0.0.0", PORT), config, token) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
