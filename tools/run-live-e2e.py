"""Owned loopback-only real-service acceptance, with a OneBot protocol simulator.

Run outside the sandbox with explicit approval. Docker containers use host
networking solely to reach a loopback Magpie gateway; every test listener binds
127.0.0.1 and uses its own port. No existing service/configuration is modified.
"""

import argparse
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def run(*args, **options):
    return subprocess.run(list(args), check=True, **options)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file, indent=2)


def extraction_response(request, raw):
    schema = request.get("response_format", {}).get("json_schema", {}).get("schema", {})
    item = schema.get("properties", {}).get("facts", {}).get("items", {})
    if isinstance(item, dict) and item.get("$ref", "").startswith("#/$defs/"):
        item = schema.get("$defs", {}).get(item["$ref"].removeprefix("#/$defs/"), {})
    # Consolidation also returns `facts` in 1.2; only extraction has what/fact_type.
    if isinstance(item, dict) and {"what", "fact_type"} <= item.get("properties", {}).keys():
        return True
    if request.get("response_format", {}).get("type") != "json_object":
        return False
    try:
        value = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    facts = value.get("facts") if isinstance(value, dict) else None
    return isinstance(facts, list) and bool(facts) and all(
        isinstance(fact, dict) and isinstance(fact.get("what"), str)
        and fact.get("fact_type") in {"world", "assistant"}
        for fact in facts
    )


@contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state, port=0):
        super().__init__((getattr(state, "gateway_host", "127.0.0.1"), port), GatewayHandler)
        self.state = state
        self.recall_delay = 0
        self.background_delay = 0
        self.extraction_gate = threading.Event()
        self.extraction_gate.set()
        self.extraction_held = 0
        self.cleanup_gate = threading.Event()
        self.cleanup_gate.set()
        self.lock = threading.Lock()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def record(self, kind, value):
        with self.lock, (self.state.output / (kind + ".jsonl")).open("a") as file:
            file.write(json.dumps(value, ensure_ascii=True) + "\n")

    def handle_error(self, request, client_address):
        pass


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, raw, content_type="application/json"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

    def drop(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def relay_chat_stream(self, response, request, started):
        self.send_response(response.status)
        self.send_header("Content-Type", response.headers["Content-Type"])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        chunks = []
        size = 0
        while chunk := response.read1(16 * 1024):
            size += len(chunk)
            if size > 32 * 1024 * 1024:
                self.drop()
                return
            chunks.append(chunk)
            self.wfile.write(chunk)
            self.wfile.flush()
        self.server.record("chat-stream-trace", {
            "request": request, "response_sse": b"".join(chunks).decode("utf-8"),
            "seconds": round(time.monotonic() - started, 3),
        })

    def do_GET(self):
        self.dispatch()

    do_POST = do_PUT = do_DELETE = do_GET

    def dispatch(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8 * 1024 * 1024:
            self.reply(413, b"{}")
            return
        raw = self.rfile.read(length) if length else None
        if self.path.startswith("/control"):
            if not hmac.compare_digest(self.headers.get("X-Test-Token", ""), state.settings["control_token"]):
                self.reply(401, b"{}")
                return
            try:
                body = json.loads(raw or b"{}")
                if body["action"] in {"delay_recall", "delay_background"}:
                    delay = float(body["seconds"])
                    if not 0 <= delay <= 15:
                        raise ValueError
                    if body["action"] == "delay_background":
                        self.server.background_delay = delay
                    else:
                        self.server.recall_delay = delay
                elif body["action"] == "hold_extraction":
                    self.server.extraction_gate.clear()
                elif body["action"] == "release_extraction":
                    self.server.extraction_gate.set()
                elif body["action"] == "hold_cleanup":
                    self.server.cleanup_gate.clear()
                elif body["action"] == "release_cleanup":
                    self.server.cleanup_gate.set()
                elif body["action"] == "extraction_status":
                    self.reply(200, json.dumps({"held": self.server.extraction_held}).encode())
                    return
                elif body["action"] in {"managed_install", "managed_reinstall", "managed_upgrade", "managed_uninstall",
                                       "managed_pause", "managed_unpause"}:
                    state.managed_control(body["action"])
                elif body["action"] in {"stop_hindsight", "start_hindsight", "restart_hindsight", "rrf_hindsight", "restart_astrbot", "stop_astrbot", "start_astrbot"}:
                    state.control(body["action"])
                else:
                    raise ValueError
                self.reply(200, b'{"ok":true}')
            except Exception as error:
                self.server.record("control-failure", {
                    "type": type(error).__name__,
                    "trace": [{"file": Path(row.filename).name, "line": row.lineno, "function": row.name}
                              for row in traceback.extract_tb(error.__traceback__)],
                })
                self.reply(500, b'{"error":"test_control_failed"}')
            return
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        response_delay = 0
        if self.path.startswith("/memory/"):
            path = self.path[len("/memory"):]
            if self.headers.get("Authorization"):
                headers["Authorization"] = self.headers["Authorization"]
            url = state.settings["hindsight_url"] + path
            if path.endswith("/recall") and self.command == "POST":
                response_delay = self.server.recall_delay
                self.server.record("recall-trace", {"path": path, "body": json.loads(raw)})
            if "/mental-models/" in path and self.command == "GET":
                response_delay = self.server.background_delay
            if path.endswith("/memories") and self.command == "POST":
                self.server.record("retain-trace", {"path": path, "body": json.loads(raw)})
            if self.command == "DELETE" and not self.server.cleanup_gate.is_set():
                self.server.record("held-cleanup-trace", {"path": path, "at": time.time()})
                if not self.server.cleanup_gate.wait(120):
                    self.drop()
                    return
        elif self.path in {"/v1/chat/completions", "/v1/models"}:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + state.settings["chat_token"]):
                self.reply(401, b"{}")
                return
            url = state.magpie + self.path.removeprefix("/v1")
        else:
            self.reply(404, b"{}")
            return
        started = time.monotonic()
        try:
            with self.server.opener.open(Request(url, data=raw, headers=headers, method=self.command), timeout=120) as response:
                if (
                    self.path == "/v1/chat/completions"
                    and response.headers.get_content_type() == "text/event-stream"
                ):
                    self.relay_chat_stream(response, json.loads(raw), started)
                    return
                value = response.read(32 * 1024 * 1024 + 1)
                # Hold the genuine response until the requested minimum latency;
                # cloud processing time counts toward the same test deadline.
                time.sleep(max(0, response_delay - (time.monotonic() - started)))
                if self.path == "/v1/chat/completions":
                    request = json.loads(raw)
                    if extraction_response(request, value) and not self.server.extraction_gate.is_set():
                        self.server.extraction_held += 1
                        self.server.record("held-extraction-trace", {"event": "held", "at": time.time()})
                        # Keep the actual cloud response; release or fail within a bounded test window.
                        if not self.server.extraction_gate.wait(180):
                            self.drop()
                            return
                        self.server.record("held-extraction-trace", {"event": "released", "at": time.time()})
                    self.server.record("chat-trace", {"request": json.loads(raw), "response": json.loads(value),
                                                      "seconds": round(time.monotonic() - started, 3)})
                self.reply(response.status, value, response.headers.get("Content-Type", "application/json"))
        except HTTPError as error:
            with error:
                self.reply(error.code, error.read(32 * 1024 * 1024))
        except (OSError, URLError):
            # A real stopped backend becomes a connection failure at the client.
            self.drop()


class Environment:
    def __init__(self, args):
        self.output = args.output.resolve()
        if self.output.is_relative_to(ROOT) or (self.output.exists() and any(self.output.iterdir())):
            raise ValueError("Output must be a fresh directory outside the repository")
        self.output.mkdir(parents=True, mode=0o700)
        os.chmod(self.output, 0o700)
        self.owner = "hb-live-" + secrets.token_hex(4)
        self.containers = []
        self.volume = self.owner + "-postgres"
        astrbot_version = getattr(args, "astrbot_version", "4.29.1")
        self.astrbot_image = "soulter/astrbot:v" + astrbot_version
        hindsight_version = getattr(args, "hindsight_version", "1.2.0")
        if hindsight_version not in {"1.1.0", "1.2.0"}:
            raise ValueError("Unsupported Hindsight test version")
        self.hindsight_image = "ghcr.io/vectorize-io/hindsight:" + hindsight_version + "-slim"
        self.magpie = args.magpie_url.rstrip("/")
        if self.magpie not in {"http://127.0.0.1:3425/v1", "http://localhost:3425/v1"}:
            raise ValueError("This harness expects the explicitly selected loopback Magpie gateway")
        self.settings = {
            "owner": self.owner, "dashboard_port": free_port(), "onebot_port": free_port(),
            "db_port": free_port(), "hindsight_port": free_port(),
            "onebot_token": secrets.token_hex(20), "chat_token": secrets.token_hex(20),
            "control_token": secrets.token_hex(20), "memory_key": secrets.token_hex(20),
            "database_password": secrets.token_hex(20),
            "chat_model": args.chat_model, "astrbot_version": astrbot_version,
            "hindsight_version": hindsight_version, "hindsight_image": self.hindsight_image,
            "initial_password": "Delivery-Initial-2026!",
            "admin_password": "Delivery-Acceptance-2026!", "admin_name": "delivery-admin",
        }
        self.settings["hindsight_url"] = f'http://127.0.0.1:{self.settings["hindsight_port"]}'
        self.gateway = Gateway(self)
        gateway_url = f"http://127.0.0.1:{self.gateway.server_port}"
        self.settings.update(gateway_url=gateway_url, chat_url=gateway_url + "/v1", memory_url=gateway_url + "/memory")
        spec = importlib.util.spec_from_file_location("model_transport", ROOT / "deploy/model_transport.py")
        transport = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(transport)
        upstream_post = transport._post
        def measured_post(config, url, headers, body):
            started = time.monotonic()
            sample = {"protocol": config.get("protocol"), "model": config.get("model"), "started_at": time.time()}
            try:
                result = upstream_post(config, url, headers, body)
                sample["success"] = True
                return result
            except Exception as error:
                sample.update(success=False, error_type=type(error).__name__)
                raise
            finally:
                sample["seconds"] = round(time.monotonic() - started, 3)
                self.gateway.record("model-phase-trace", sample)
        transport._post = measured_post
        environment = json.loads(args.model_environment.read_text())
        descriptors = json.loads(environment.pop("ASTRBOT_MEMORY_MODEL_CONFIG"))
        self.transport_token = secrets.token_hex(20)
        self.transport = transport.TransportServer(("127.0.0.1", 0), descriptors, self.transport_token)
        transport_url = f"http://127.0.0.1:{self.transport.server_port}"
        self.models = {name: value.replace("http://memory-models:8765", transport_url).replace(
            "__ASTRBOT_MEMORY_TRANSPORT_KEY__", self.transport_token) for name, value in environment.items()}
        self.models.update(HINDSIGHT_API_LLM_PROVIDER="openai", HINDSIGHT_API_LLM_MODEL=args.chat_model,
                           HINDSIGHT_API_LLM_BASE_URL=self.settings["chat_url"], HINDSIGHT_API_LLM_API_KEY=self.settings["chat_token"])
        private_json(self.output / "live-settings.json", self.settings)

    def docker(self, *args, **options):
        return run("docker", *args, **options)

    def owned(self, name):
        label = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "hindsight-bridge.test"}}', name], text=True).strip()
        if label != self.owner:
            raise RuntimeError("Resource does not belong to this test")

    def launch(self, role, image, *options, command=()):
        name = self.owner + "-" + role
        self.containers.append(name)
        self.docker("run", "-d", "--name", name, "--label", "hindsight-bridge.test=" + self.owner,
                    "--network", "host", *options, image, *command, stdout=subprocess.DEVNULL)
        return name

    def start_hindsight(self, rrf=False):
        name = self.owner + "-hindsight"
        if name in self.containers:
            self.owned(name)
            self.docker("stop", "-t", "90", name, stdout=subprocess.DEVNULL)
            self.docker("rm", name, stdout=subprocess.DEVNULL)
            self.containers.remove(name)
        s = self.settings
        env = {**self.models,
               "HINDSIGHT_API_DATABASE_URL": f'postgresql://hindsight:{s["database_password"]}@127.0.0.1:{s["db_port"]}/hindsight',
               "HINDSIGHT_API_HOST": "127.0.0.1", "HINDSIGHT_API_PORT": str(s["hindsight_port"]),
               "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
               "HINDSIGHT_API_TENANT_API_KEY": s["memory_key"], "HINDSIGHT_API_WORKER_ID": self.owner,
               "HINDSIGHT_API_WORKER_MAX_SLOTS": "2", "HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS": "1",
               "HINDSIGHT_API_DB_POOL_MIN_SIZE": "2", "HINDSIGHT_API_DB_POOL_MAX_SIZE": "10",
               "HINDSIGHT_API_OPERATION_RETENTION_DAYS": "0", "HINDSIGHT_API_VECTOR_EXTENSION": "pgvector",
               "HINDSIGHT_API_TEXT_SEARCH_EXTENSION": "pgroonga", "HINDSIGHT_API_LOG_LEVEL": "warning",
               "HINDSIGHT_API_OTEL_ENABLED": "false", "HINDSIGHT_ENABLE_CP": "false",
               "HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS": "8192", "HINDSIGHT_API_CONSOLIDATION_MAX_COMPLETION_TOKENS": "8192"}
        if rrf:
            env["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"
        with os.fdopen(os.open(self.output / "hindsight.env", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as file:
            for key, value in env.items():
                file.write(key + "=" + value + "\n")
        self.launch("hindsight", self.hindsight_image, "--memory", "1536m", "--init", "--stop-timeout", "90",
                    "--env-file", str(self.output / "hindsight.env"))

    def control(self, action):
        role = "astrbot" if action.endswith("_astrbot") else "hindsight"
        name = self.owner + "-" + role
        self.owned(name)
        if action == "rrf_hindsight":
            self.start_hindsight(rrf=True)
        else:
            command = "restart" if action.startswith("restart") else "stop" if action.startswith("stop") else "start"
            self.docker(command, *( ["-t", str(self.settings.get("stop_timeout", 90))] if command in {"restart", "stop"} else []), name, stdout=subprocess.DEVNULL)

    def wait(self, predicate, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            for name in self.containers:
                status = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", name], text=True).strip()
                if status in {"exited", "dead"}:
                    raise RuntimeError("Test service exited during startup: " + name)
            time.sleep(1)
        raise TimeoutError("Test service did not become ready")

    def ready_http(self, url):
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=3) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def start(self):
        run(sys.executable, str(ROOT / "tools/build-test-package.py"), "--output", str(self.output))
        with (self.output / "released-install.zip").open("wb") as file:
            run("git", "archive", "--format=zip", "--prefix=astrbot_plugin_hindsight_bridge/", "v0.1.0", cwd=ROOT, stdout=file)
        self.docker("volume", "create", "--label", "hindsight-bridge.test=" + self.owner, self.volume, stdout=subprocess.DEVNULL)
        self.launch("db", "hindsight-bridge-delivery-postgres:17", "--memory", "512m", "--shm-size", "128m",
                    "-e", "POSTGRES_USER=hindsight", "-e", "POSTGRES_DB=hindsight", "-e", "POSTGRES_PASSWORD=" + self.settings["database_password"],
                    "-v", self.volume + ":/var/lib/postgresql/data",
                    command=("postgres", "-p", str(self.settings["db_port"]), "-c", "listen_addresses=127.0.0.1"))
        def db_ready():
            return subprocess.run(["docker", "exec", self.owner + "-db", "pg_isready", "-U", "hindsight", "-p", str(self.settings["db_port"])],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        self.wait(db_ready, 60)
        self.start_hindsight()
        self.wait(lambda: self.ready_http(self.settings["hindsight_url"] + "/health"))
        actual = subprocess.check_output([
            "docker", "exec", self.owner + "-hindsight", "python", "-c",
            "from hindsight_api import __version__; print(__version__)",
        ], text=True).strip()
        if actual != self.settings["hindsight_version"]:
            raise RuntimeError("Hindsight runtime version differs from the selected image")
        self.settings["hindsight_image_id"] = subprocess.check_output([
            "docker", "inspect", "--format", "{{.Image}}", self.owner + "-hindsight",
        ], text=True).strip()
        private_json(self.output / "live-settings.json", self.settings)
        print("Real Hindsight and PostgreSQL are ready", flush=True)
        token_cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or os.environ.get("DATA_GYM_CACHE_DIR")
                           or str(Path(tempfile.gettempdir()) / "data-gym-cache")).resolve()
        run(sys.executable, "-c", "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')")
        mounts = ["-v", str(ROOT) + ":/candidate-source:ro", "-v", str(self.output) + ":/delivery",
                  "-e", "ASTRBOT_ROOT=/delivery/verification", "-e", "ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + self.settings["initial_password"],
                  "-e", "TIKTOKEN_CACHE_DIR=/token-cache", "-v", str(token_cache) + ":/token-cache:ro"]
        self.docker("run", "--rm", "--network", "none", *mounts, self.astrbot_image, "python", "/candidate-source/tests/integration/live_astrbot_setup.py")
        self.launch("astrbot", self.astrbot_image, "--memory", "1536m", *mounts, *getattr(self, "astrbot_mounts", ()))
        dashboard = f'http://127.0.0.1:{self.settings["dashboard_port"]}'
        self.wait(lambda: self.ready_http(dashboard + "/api/v1/auth/setup-status"))
        print("Real AstrBot dashboard is ready", flush=True)

    def verify(self):
        self.docker("build", "-t", "hindsight-bridge-live-browser:1.56.0", "-f", str(ROOT / "tests/integration/Dockerfile.browser"), str(ROOT / "tests/integration"),
                    stdout=(self.output / "browser-build.log").open("w"), stderr=subprocess.STDOUT)
        name = self.owner + "-browser"
        self.containers.append(name)
        self.docker("run", "--name", name, "--label", "hindsight-bridge.test=" + self.owner, "--network", "host", "--memory", "2g", "--shm-size", "512m",
                    "-e", "HOME=/tmp", "-v", str(ROOT) + ":/candidate-source:ro", "-v", str(self.output) + ":/delivery",
                    "hindsight-bridge-live-browser:1.56.0", "python", "/candidate-source/tests/integration/" + self.driver)

    def cleanup(self):
        for name in reversed(self.containers):
            try:
                self.owned(name)
                result = subprocess.run(["docker", "logs", name], capture_output=True)
                (self.output / (name + ".log")).write_bytes(result.stdout + result.stderr)
                self.docker("stop", "-t", "90", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.docker("rm", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, RuntimeError):
                print("Cleanup needs inspection: " + name, file=sys.stderr)
        try:
            label = subprocess.check_output(["docker", "volume", "inspect", "--format", '{{index .Labels "hindsight-bridge.test"}}', self.volume], text=True, stderr=subprocess.DEVNULL).strip()
            if label == self.owner:
                self.docker("volume", "rm", self.volume, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-environment", type=Path, default=Path("/tmp/hindsight-bridge-qwen-acceptance/model-environment.json"))
    parser.add_argument("--magpie-url", default="http://127.0.0.1:3425/v1")
    parser.add_argument("--chat-model", default="remote-magpie/workbuddy-ai/deepseek-v4.1-flash-sg")
    parser.add_argument("--astrbot-version", choices=("4.29.1", "4.29.4"), default="4.29.1")
    parser.add_argument("--hindsight-version", choices=("1.1.0", "1.2.0"), default="1.2.0")
    parser.add_argument("--scenario", choices=("full", "accepted", "connection"), default="full")
    args = parser.parse_args()
    state = Environment(args)
    state.driver = {"accepted": "accepted_e2e.py", "connection": "connection_e2e.py", "full": "live_e2e.py"}[args.scenario]
    try:
        with serving(state.gateway), serving(state.transport):
            state.start()
            state.verify()
    finally:
        state.gateway.extraction_gate.set()
        state.gateway.cleanup_gate.set()
        state.cleanup()
    print("Acceptance evidence: " + str(state.output))


if __name__ == "__main__":
    main()
