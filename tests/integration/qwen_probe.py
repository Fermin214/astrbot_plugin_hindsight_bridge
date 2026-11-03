"""Small live native-protocol probe; credentials never enter the report."""

import argparse
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def read_key(path):
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.removeprefix("export ").partition("=")
        if separator and name.strip().lower() in {"key", "api_key", "dashscope_api_key", "qwen_api_key"}:
            value = value.strip().strip("\"'")
            if value and not any(ord(char) < 32 for char in value):
                return value
    raise ValueError("No test API key found")


def post(opener, key, url, body):
    request = Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    started = time.monotonic()
    try:
        with opener.open(request, timeout=45) as response:
            result = json.load(response)
        return result, {"http_status": response.status, "seconds": round(time.monotonic() - started, 3)}
    except HTTPError as error:
        status = error.code
        try:
            body = json.load(error)
            code = body.get("code", "")
        except (ValueError, TypeError):
            code = ""
        finally:
            error.close()
        return None, {"http_status": status, "code": code if re.fullmatch(r"[\w.-]{1,80}", str(code)) else ""}
    except (URLError, TimeoutError):
        return None, {"error": "network_or_timeout"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default="https://cn-beijing.maas.qianwenaiapi.com/api/v1")
    parser.add_argument("--embedding-model", default="qwen3.7-text-embedding-flash")
    parser.add_argument("--rerank-model", default="qwen3.7-text-rerank")
    args = parser.parse_args()
    key = read_key(args.secret_file)
    base = args.base_url.rstrip("/")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    texts = ["The user prefers jasmine tea.", "What tea does the user like?", "A laptop needs a battery."]
    body, embedding = post(opener, key, base + "/services/embeddings/text-embedding/text-embedding", {
        "model": args.embedding_model, "input": {"texts": texts},
    })
    embedding["model"] = args.embedding_model
    embedding["passed"] = False
    if isinstance(body, dict):
        rows = body.get("output", {}).get("embeddings", [])
        vectors = [row.get("embedding", []) for row in rows]
        valid = len(vectors) == len(texts) and len(vectors[0]) > 0 and all(
            len(vector) == len(vectors[0]) and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            for vector in vectors
        )
        embedding.update(passed=valid, dimensions=len(vectors[0]) if vectors else 0, count=len(vectors))
    body, rerank = post(opener, key, base + "/services/rerank/text-rerank/text-rerank", {
        "model": args.rerank_model, "input": {"query": texts[1], "documents": [texts[2], texts[0]]},
        "parameters": {"top_n": 2},
    })
    rerank.update(model=args.rerank_model, passed=False)
    if isinstance(body, dict):
        rows = body.get("output", {}).get("results", [])
        rerank.update(passed=len(rows) == 2 and {row.get("index") for row in rows} == {0, 1}
                      and all(type(row.get("relevance_score")) in (int, float) and math.isfinite(row["relevance_score"]) for row in rows),
                      count=len(rows), preferred_document_ranked_first=bool(rows and rows[0].get("index") == 1))
    report = {"base_url": base, "embedding": embedding, "rerank": rerank}
    encoded = json.dumps(report, indent=2)
    if key in encoded:
        raise RuntimeError("Credential found in report")
    Path(args.output).write_text(encoded + "\n")
    print(encoded)
    return 0 if embedding["passed"] and rerank["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
