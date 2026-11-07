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


def arguments(name, content=MARKER):
    if name == "done":
        return {
            "answer": content, "markdown": content, "answer_found": bool(content),
            "memory_ids": [], "observation_ids": [], "mental_model_ids": [],
            "document": {"sections": [{"heading": "Background", "level": 2, "blocks": [content]}] if content else []},
        }
    return {"query": "Cedar-120", "reason": "synthetic fixture", "max_tokens": 256}


def tool_value(message):
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        value = json.loads(content)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def bridge_tool_reply(body):
    """Drive only synthetic chats; native Hindsight model requests use their own tools."""
    messages = body.get("messages", [])
    last_user = next((index for index in range(len(messages) - 1, -1, -1)
                      if messages[index].get("role") == "user"), -1)
    if last_user >= 0 and str(messages[last_user].get("content", "")).startswith("You have decided to call tool(s):"):
        last_user = next((index for index in range(last_user - 1, -1, -1)
                          if messages[index].get("role") == "user"), -1)
    query = json.dumps(messages[last_user].get("content", "")) if last_user >= 0 else ""
    current = [message for message in messages[last_user + 1:] if message.get("role") == "tool"]
    history = [tool_value(message) for message in messages if message.get("role") == "tool"]
    page = next((value for value in reversed(history) if isinstance(value, dict) and value.get("kind") == "page"), {})
    kind, params = "", {}
    if "HB-TOOLS-FLOW" in query or "HB-TOOLS-GUARDS" in query:
        if not current:
            kind, params = "search_pages", {"query": "HB-TOOLS-PAGE"}
        elif len(current) == 1:
            values = tool_value(current[0]).get("items", [])
            selected = next((value for value in values if "HB-TOOLS-PAGE" in value.get("name", "")), None)
            if selected:
                kind, params = "read_page", {"page_id": selected["id"]}
        elif len(current) == 2:
            kind, params = "recall", {"query": "Cedar-120"}
    elif "HB-TOOLS-CONTINUE" in query and len(current) < 2 and page.get("next_line"):
        kind, params = "read_page", {"page_id": page["page_id"], "start_line": page["next_line"], "version": page["version"]}
    elif "HB-TOOLS-SOURCES" in query and page and len(current) < 2:
        kind, params = ("page_sources" if not current else "page_history"), {"page_id": page["page_id"]}
    elif "HB-TOOLS-STALE" in query and not current and page.get("next_line"):
        kind, params = "read_page", {"page_id": page["page_id"], "start_line": page["next_line"], "version": page["version"]}
    elif "HB-TOOLS-QUOTA" in query and len(current) < 6:
        kind, params = "recall", {"query": "Cedar-120 quota " + str(len(current))}
    elif "HB-TOOLS-PROBE-" in query and not current:
        kind, params = "recall", {"query": "Cedar-120"}
    if not kind:
        return {"role": "assistant", "content": "HB-TOOLS-ANSWER"}, "stop"
    if not any(tool.get("function", {}).get("name") == "hindsight_bridge_" + kind for tool in body.get("tools", [])):
        return {"role": "assistant", "content": "HB-TOOLS-ANSWER"}, "stop"
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "bridge-fixture-" + str(len(current)), "type": "function",
        "function": {"name": "hindsight_bridge_" + kind, "arguments": json.dumps(params)},
    }]}, "tool_calls"


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
        local_chat = (getattr(self.server, "local_chat", False) and self.path == "/v1/chat/completions"
                      and self.headers.get("Authorization") is None)
        if self.headers.get("Authorization") != "Bearer " + self.server.token and not local_chat:
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
            request_text = json.dumps(body)
            page_content = MARKER
            if "HB-FEEDBACK-EMPTY" in request_text:
                time.sleep(20)
            elif "HB-FEEDBACK-LONG" in request_text:
                page_content = "HB-FEEDBACK-LONG complete paragraph. " * 120
            elif "HB-BUDGET-MEDIUM" in request_text:
                page_content = "neutral " * 210
            elif "HB-TOOLS-PAGE" in request_text:
                prefix = "HB-TOOLS-V2" if "HB-TOOLS-PAGE-V2" in request_text else "HB-TOOLS-V1"
                page_content = "".join(f"{prefix}-LINE-{index:03d} complete original paragraph; conditions preserved.\n"
                                       for index in range(200))
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
            content, finish, message = "ok", "stop", None
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
            elif (
                getattr(self.server, "local_chat", False) and body.get("max_tokens") == 512
                and "HB-TOOLS-" in str(body.get("messages", []))
            ):
                message, finish = bridge_tool_reply(body)
            elif body.get("tools") and not (getattr(self.server, "local_chat", False) and body.get("max_tokens") == 512):
                choice = body.get("tool_choice")
                name = choice.get("function", {}).get("name") if isinstance(choice, dict) else "done"
                searched = any(
                    call.get("function", {}).get("name") == "recall"
                    for message in body.get("messages", []) for call in message.get("tool_calls", [])
                )
                if not isinstance(choice, dict) and not searched and any(
                    tool.get("function", {}).get("name") == "recall" for tool in body["tools"]
                ):
                    name = "recall"
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments(name, page_content))},
                }]}
                finish = "tool_calls"
                self.server.counts["page"] += 1
            elif "call" in properties:
                content = json.dumps({"call": {"name": "done", "arguments": arguments("done", page_content)}})
                self.server.counts["page"] += 1
            elif "query" in properties:
                content = json.dumps(arguments("search"))
                self.server.counts["page"] += 1
            elif "answer_found" in properties or "markdown" in properties:
                content = json.dumps(arguments("done", page_content))
                self.server.counts["page"] += 1
            if finish != "tool_calls" and message is None:
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
