"""Configure only this container's Hindsight runtime and private settings volume."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from model_transport import TransportServer


MAX_BODY = 256 * 1024
OPENER = build_opener(ProxyHandler({}))


class ConfigurationError(ValueError):
    pass


def configuration(environment):
    if not isinstance(environment, dict) or not environment:
        raise ConfigurationError("模型配置为空。")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        or len(value) > 65536 or "\x00" in value
        or not (
            re.fullmatch(r"HINDSIGHT_API_(?:LLM|EMBEDDINGS|RERANKER)_[A-Z0-9_]+", key)
            or key in {"ASTRBOT_MEMORY_MODEL_CONFIG", "ASTRBOT_MEMORY_EMBEDDING_IDENTITY"}
        )
        for key, value in environment.items()
    ):
        raise ConfigurationError("模型配置格式无效。")
    identity = environment.get("ASTRBOT_MEMORY_EMBEDDING_IDENTITY")
    identity = json.loads(identity) if identity else {
        key: value for key, value in environment.items()
        if key.startswith("HINDSIGHT_API_EMBEDDINGS_")
        and not key.endswith(("_API_KEY", "_BATCH_SIZE"))
    }
    if not isinstance(identity, dict) or not identity:
        raise ConfigurationError("缺少嵌入模型标识。")
    fingerprint = hashlib.sha256(repr(sorted(identity.items())).encode()).hexdigest()
    return {"environment": dict(environment), "embedding_fingerprint": fingerprint}


class Runtime:
    def __init__(self, directory, token, identity, embedding_fingerprint=""):
        self.directory = Path(directory)
        self.token, self.identity = token, identity
        self.lock = threading.Lock()
        self.process = self.transport = self.transport_thread = None
        self.active = None
        self.last_error = ""
        if embedding_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", embedding_fingerprint):
            raise ConfigurationError("嵌入模型标识无效。")
        self.initial_fingerprint = embedding_fingerprint
        path = self.directory / "models.json"
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            self.active = configuration(value["environment"])
            if self.active["embedding_fingerprint"] != value["embedding_fingerprint"]:
                raise ConfigurationError("模型配置标识损坏。")
            self.start(self.active)

    def status(self):
        return {
            "installation_id": self.identity, "protocol": 1,
            "configured": self.active is not None,
            "ready": self.process is not None and self.process.poll() is None and self.healthy(),
            "embedding_fingerprint": self.active["embedding_fingerprint"] if self.active else "",
            "error": self.last_error,
        }

    @staticmethod
    def healthy():
        try:
            with OPENER.open("http://127.0.0.1:8888/health", timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def environment(self, candidate):
        values = dict(candidate["environment"])
        descriptors = json.loads(values.pop("ASTRBOT_MEMORY_MODEL_CONFIG", "{}"))
        values.pop("ASTRBOT_MEMORY_EMBEDDING_IDENTITY", None)
        values = {
            key: value.replace("__ASTRBOT_MEMORY_TRANSPORT_KEY__", self.token)
            .replace("http://memory-models:8765", "http://127.0.0.1:8765")
            for key, value in values.items()
        }
        return {**os.environ, **values}, descriptors

    def start_transport(self, descriptors):
        if descriptors:
            self.transport = TransportServer(("127.0.0.1", 8765), descriptors, self.token)
            self.transport_thread = threading.Thread(target=self.transport.serve_forever, daemon=True)
            self.transport_thread.start()

    def start(self, candidate):
        environment, descriptors = self.environment(candidate)
        self.start_transport(descriptors)
        self.process = subprocess.Popen(
            ["/app/start-all.sh"], env=environment, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
            self.process = None
        if self.transport:
            self.transport.shutdown()
            self.transport.server_close()
            self.transport_thread.join(timeout=3)
            self.transport = self.transport_thread = None

    def wait_ready(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if self.healthy():
                return
            time.sleep(1)
        raise ConfigurationError("记忆服务启动失败，请检查服务日志与模型配置。")

    def apply(self, environment):
        candidate = configuration(environment)
        if not self.lock.acquire(blocking=False):
            raise ConfigurationError("模型配置正在应用，请稍后刷新。")
        try:
            expected = self.active["embedding_fingerprint"] if self.active else self.initial_fingerprint
            if expected and expected != candidate["embedding_fingerprint"]:
                raise ConfigurationError("嵌入模型或维数变化需要迁移已有向量数据。")
            previous = self.active
            self.stop()
            try:
                values, descriptors = self.environment(candidate)
                self.start_transport(descriptors)
                subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("model_probe.py"))],
                    env=values, check=True, timeout=120,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if self.transport:
                    self.transport.shutdown()
                    self.transport.server_close()
                    self.transport_thread.join(timeout=3)
                    self.transport = self.transport_thread = None
                self.start(candidate)
                self.wait_ready()
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = self.directory / "models.json.new"
                temporary.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
                temporary.chmod(0o600)
                temporary.replace(self.directory / "models.json")
                self.active = candidate
                self.last_error = ""
            except Exception:
                self.stop()
                if previous:
                    self.start(previous)
                self.last_error = "模型连接或应用失败，请检查所选模型的地址、密钥与网络。"
                raise ConfigurationError(self.last_error) from None
            return self.status()
        finally:
            self.lock.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def authorized(self):
        expected = "Bearer " + self.server.management_key
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode())

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"ok": True})
        if not self.authorized():
            return self.send(401, {"message": "连接凭据无效。"})
        if self.path != "/status":
            return self.send(404, {"message": "接口不存在。"})
        self.send(200, self.server.runtime.status())

    def do_POST(self):
        if not self.authorized():
            return self.send(401, {"message": "连接凭据无效。"})
        if self.path != "/models":
            return self.send(404, {"message": "接口不存在。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError()
            payload = json.loads(data)
            result = self.server.runtime.apply(payload["environment"])
        except ConfigurationError as exc:
            return self.send(400, {"message": str(exc)})
        except Exception:
            return self.send(400, {"message": "模型配置格式无效。"})
        self.send(200, result)


def main():
    os.umask(0o077)
    runtime = Runtime(
        "/settings", os.environ["HINDSIGHT_API_TENANT_API_KEY"],
        os.environ["HINDSIGHT_SETUP_ID"], os.environ.get("HINDSIGHT_SETUP_FINGERPRINT", ""),
    )
    server = ThreadingHTTPServer(("0.0.0.0", 8766), Handler)
    server.daemon_threads = True
    server.runtime = runtime
    server.management_key = os.environ["ASTRBOT_MEMORY_MANAGEMENT_KEY"]

    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    failed = threading.Event()
    finished = threading.Event()

    def monitor():
        while not finished.wait(2):
            if runtime.lock.acquire(blocking=False):
                try:
                    if runtime.active and runtime.process and runtime.process.poll() is not None:
                        failed.set()
                        server.shutdown()
                        return
                finally:
                    runtime.lock.release()

    threading.Thread(target=monitor, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        finished.set()
        with runtime.lock:
            runtime.stop()
        server.server_close()
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
