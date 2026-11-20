"""Configure only this container's Hindsight runtime and private settings volume."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from model_probe import STAGES
from model_transport import TransportServer, transport_environment


MAX_BODY = 256 * 1024
STOP_TIMEOUT = 75
PROBE_TIMEOUT = 120
PROBE_LIMIT = 4096
OPENER = build_opener(ProxyHandler({}))

PROBE_ERRORS = {
    "embedding_init": {
        "embedding_init_failed": "嵌入模型初始化失败，请检查模型配置。",
    },
    "embedding_encode": {
        "embedding_encode_failed": "嵌入模型请求失败，请检查地址、密钥与网络。",
        "embedding_empty": "嵌入模型返回了空向量。",
        "embedding_invalid_count": "嵌入模型返回的向量数量不正确。",
    },
    "embedding_dim_check": {
        "embedding_dim_check_failed": "无法校验嵌入模型维数，请检查模型配置。",
        "embedding_dim_mismatch": "嵌入模型返回的维数与配置不符。",
        "embedding_non_finite": "嵌入模型返回的向量含无效数值。",
    },
    "llm_verify": {
        "llm_verify_failed": "对话模型连接失败，请检查地址、密钥与网络。",
    },
    "rerank_init": {
        "rerank_init_failed": "重排模型初始化失败，请检查模型配置。",
    },
    "rerank_predict": {
        "rerank_predict_failed": "重排模型请求失败，请检查地址、密钥与网络。",
        "rerank_invalid_score": "重排模型返回了无效分数。",
    },
}

SERVICE_ERRORS = {
    "probe_failed": "模型连接或应用失败，请检查所选模型的地址、密钥与网络。",
    "probe_timeout": "模型探测超时，请检查模型服务与网络。",
    "service_start_timeout": "记忆服务启动超时，请检查服务日志与模型配置。",
    "service_exited": "记忆服务在启动时退出，请检查服务日志与模型配置。",
}


class ConfigurationError(ValueError):
    pass


class ApplyError(ConfigurationError):
    def __init__(self, code, stage=None, last_ok_stage=None):
        self.diagnostic = {"code": code, "stage": stage, "last_ok_stage": last_ok_stage}
        super().__init__(PROBE_ERRORS[stage][code] if stage else SERVICE_ERRORS[code])


def probe_error(output, returncode):
    try:
        report = json.loads(output)
        if not isinstance(report, dict) or set(report) != {"ok", "stage", "last_ok_stage", "code"}:
            return ApplyError("probe_failed")
        ok, stage, last_ok_stage, code = (
            report["ok"], report["stage"], report["last_ok_stage"], report["code"],
        )
        if not isinstance(stage, str) or not isinstance(code, str) or len(output.splitlines()) != 1:
            return ApplyError("probe_failed")
        if (ok is True and returncode == 0 and stage == "complete" and code == "ok"
                and last_ok_stage in ("llm_verify", "rerank_predict")):
            return None
        if ok is False and returncode > 0 and code in PROBE_ERRORS.get(stage, {}):
            position = STAGES.index(stage)
            if last_ok_stage == (STAGES[position - 1] if position else None):
                return ApplyError(code, stage, last_ok_stage)
    except (ValueError, RecursionError):
        pass
    return ApplyError("probe_failed")


def run_probe(environment):
    command = [sys.executable, str(Path(__file__).with_name("model_probe.py"))]
    deadline = time.monotonic() + PROBE_TIMEOUT
    with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while len(output) < PROBE_LIMIT:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ApplyError("probe_timeout")
                    chunk = os.read(process.stdout.fileno(), PROBE_LIMIT - len(output))
                    if not chunk:
                        break
                    output.extend(chunk)
                else:
                    raise ApplyError("probe_failed")
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise ApplyError("probe_timeout") from None
        finally:
            if process.poll() is None:
                process.kill()
    error = probe_error(output, returncode)
    if error:
        raise error


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
        self.applying_fingerprint = ""
        self.last_error = ""
        self.diagnostic = None
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
            "ready": not self.applying_fingerprint and self.process is not None
            and self.process.poll() is None and self.healthy(),
            "embedding_fingerprint": self.active["embedding_fingerprint"] if self.active else "",
            "configuration_fingerprint": self.fingerprint(self.active["environment"]) if self.active else "",
            "applying": bool(self.applying_fingerprint),
            "applying_fingerprint": self.applying_fingerprint,
            "error": self.last_error,
            "diagnostic": self.diagnostic,
        }

    def fingerprint(self, environment):
        encoded = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.token.encode(), encoded, hashlib.sha256).hexdigest()

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
        values = transport_environment(values, descriptors, token=self.token)
        return {**os.environ, **values}, descriptors

    def save(self, candidate):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.directory / "models.json.new"
        temporary.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.directory / "models.json")
        self.active = candidate

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
                    self.process.wait(timeout=STOP_TIMEOUT)
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
                raise ApplyError("service_exited")
            if self.healthy():
                return
            time.sleep(1)
        raise ApplyError("service_start_timeout")

    def apply(self, environment):
        candidate = configuration(environment)
        if not self.lock.acquire(blocking=False):
            raise ConfigurationError("模型配置正在应用，请稍后刷新。")
        try:
            expected = self.active["embedding_fingerprint"] if self.active else self.initial_fingerprint
            if expected and expected != candidate["embedding_fingerprint"]:
                raise ConfigurationError("嵌入模型或维数变化需要迁移已有向量数据。")
            if self.active and self.environment(self.active) == self.environment(candidate) and self.status()["ready"]:
                if self.active != candidate:
                    self.save(candidate)
                return self.status()
            previous = self.active
            self.applying_fingerprint = self.fingerprint(candidate["environment"])
            self.stop()
            try:
                values, descriptors = self.environment(candidate)
                self.start_transport(descriptors)
                run_probe(values)
                if self.transport:
                    self.transport.shutdown()
                    self.transport.server_close()
                    self.transport_thread.join(timeout=3)
                    self.transport = self.transport_thread = None
                self.start(candidate)
                self.wait_ready()
                self.save(candidate)
                self.last_error = ""
                self.diagnostic = None
            except Exception as exc:
                error = exc if isinstance(exc, ApplyError) else ApplyError("probe_failed")
                self.last_error = str(error)
                self.diagnostic = error.diagnostic
                self.stop()
                if previous:
                    self.start(previous)
                raise error from None
            self.applying_fingerprint = ""
            return self.status()
        finally:
            self.applying_fingerprint = ""
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
