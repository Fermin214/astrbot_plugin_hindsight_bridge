"""Synthetic model peer for real backend upgrades; no production model calls."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


MARKER = "Native generated background marker. Compatibility marker Cedar-120."


def definition(schema, value):
    if isinstance(value, dict) and value.get("$ref", "").startswith("#/$defs/"):
        return schema.get("$defs", {}).get(value["$ref"].removeprefix("#/$defs/"), {})
    return value


def arguments(name):
    if name == "done":
        return {
            "answer": MARKER, "markdown": MARKER, "answer_found": True,
            "memory_ids": [], "observation_ids": [], "mental_model_ids": [],
            "document": {"sections": [{"heading": "Background", "level": 2, "blocks": [MARKER]}]},
        }
    return {"query": "Cedar-120", "reason": "synthetic fixture", "max_tokens": 256}


class Model(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token):
        super().__init__(address, Handler)
        self.token = token
        self.extraction_gate = threading.Event()
        self.extraction_gate.set()
        self.extraction_started = threading.Event()
        self.embedding_started = threading.Event()
        self.embedding_gate = threading.Event()
        self.embedding_gate.set()
        self.fail_embedding = False
        self.counts = {"extraction": 0, "consolidation": 0, "page": 0, "embedding": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + self.server.token:
            self.send_error(401)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/embeddings"):
            self.server.counts["embedding"] += 1
            self.server.embedding_started.set()
            self.server.embedding_gate.wait(80)
            if self.server.fail_embedding:
                self.send_error(503, "Synthetic transient embedding outage")
                return
            items = body["input"] if isinstance(body["input"], list) else [body["input"]]
            result = {"data": [{"index": i, "embedding": [0.01] * 64} for i in range(len(items))],
                      "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        else:
            schema = body.get("response_format", {}).get("json_schema", {}).get("schema", {})
            if not schema:
                for message in body.get("messages", []):
                    content = message.get("content")
                    marker = "You must respond with valid JSON matching this schema:\n"
                    if isinstance(content, str) and marker in content:
                        schema, _ = json.JSONDecoder().raw_decode(content.rsplit(marker, 1)[1].lstrip())
            properties = schema.get("properties", {})
            fact = definition(schema, properties.get("facts", {}).get("items", {}))
            fields = fact.get("properties", {}) if isinstance(fact, dict) else {}
            content, finish = "ok", "stop"
            if "what" in fields:
                self.server.counts["extraction"] += 1
                self.server.extraction_started.set()
                self.server.extraction_gate.wait(80)
                content = json.dumps({"facts": [{
                    "what": MARKER, "when": "N/A", "where": "N/A", "who": "user", "why": "fixture",
                    "fact_type": "world", "fact_kind": "conversation", "entities": [],
                }]})
            elif "fact_id" in fields:
                self.server.counts["consolidation"] += 1
                identities = fields["fact_id"].get("enum", ["F1"])
                content = json.dumps({"facts": [{
                    "fact_id": identity, "about": "fixture", "kind": "lasting",
                    "existing": [], "adds": "yes", "new": [MARKER],
                } for identity in identities], "deletes": []})
            elif "creates" in properties:
                self.server.counts["consolidation"] += 1
                content = '{"creates":[],"updates":[],"deletes":[]}'
            elif body.get("tools"):
                choice = body.get("tool_choice")
                name = choice.get("function", {}).get("name") if isinstance(choice, dict) else "done"
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments(name))},
                }]}
                finish = "tool_calls"
                self.server.counts["page"] += 1
            elif "call" in properties:
                content = json.dumps({"call": {"name": "done", "arguments": arguments("done")}})
                self.server.counts["page"] += 1
            elif "query" in properties:
                content = json.dumps(arguments("search"))
                self.server.counts["page"] += 1
            elif "answer_found" in properties or "markdown" in properties:
                content = json.dumps(arguments("done"))
                self.server.counts["page"] += 1
            if finish != "tool_calls":
                message = {"role": "assistant", "content": content}
            result = {
                "id": "fixture", "object": "chat.completion", "created": int(time.time()),
                "model": "compatibility-fixture",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        raw = json.dumps(result).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass
