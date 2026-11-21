"""Model replacement and secret isolation failures beyond live setup acceptance."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location("managed_service", DEPLOY / "managed_service.py")
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)
sys.path.remove(str(DEPLOY))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        package = Path(directory.name, "hindsight_api")
        package.mkdir()
        (package / "__init__.py").write_text(
            '''import os
import sys
from types import ModuleType, SimpleNamespace

secret = os.environ["HINDSIGHT_API_LLM_API_KEY"]
failure = os.environ["PROBE_TEST_FAILURE"]

def check(stage):
    if failure == stage:
        print(secret)
        os.write(2, secret.encode())
        raise RuntimeError(secret)

async def initialize_embedding():
    check("embedding_init")

async def encode(texts):
    check("embedding_encode")
    return {
        "embedding_empty": [],
        "embedding_invalid_count": [[1, 2], [1, 2]],
        "embedding_dim_mismatch": [[1]],
        "embedding_non_finite": [[1, float("nan")]],
    }.get(failure, [[1, 2]])

async def verify():
    check("llm_verify")

async def cleanup():
    pass

async def initialize_reranker():
    check("rerank_init")

async def predict(pairs):
    check("rerank_predict")
    return [] if failure == "rerank_invalid_score" else [1]

engine = sys.modules["hindsight_api.engine"] = ModuleType("hindsight_api.engine")
sys.modules["hindsight_api.engine.embeddings"] = SimpleNamespace(
    create_embeddings_from_env=lambda: SimpleNamespace(initialize=initialize_embedding, encode=encode),
)
sys.modules["hindsight_api.engine.llm_wrapper"] = SimpleNamespace(
    LLMProvider=SimpleNamespace(from_env=lambda: SimpleNamespace(verify_connection=verify, cleanup=cleanup)),
)
sys.modules["hindsight_api.engine.cross_encoder"] = SimpleNamespace(
    create_cross_encoder_from_env=lambda: SimpleNamespace(initialize=initialize_reranker, predict=predict),
)
''', encoding="utf-8",
        )
        self.environment = {
            **os.environ, "PYTHONPATH": directory.name, "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "2",
            "HINDSIGHT_API_LLM_API_KEY": "secret-from-provider", "HINDSIGHT_API_RERANKER_PROVIDER": "remote",
        }

    def test_subprocess_reports_each_stage_and_never_prints_provider_output(self):
        for failure, stage, previous, code in (
            ("embedding_init", "embedding_init", None, "embedding_init_failed"),
            ("embedding_encode", "embedding_encode", "embedding_init", "embedding_encode_failed"),
            ("embedding_empty", "embedding_encode", "embedding_init", "embedding_empty"),
            ("embedding_invalid_count", "embedding_encode", "embedding_init", "embedding_invalid_count"),
            ("embedding_dim_mismatch", "embedding_dim_check", "embedding_encode", "embedding_dim_mismatch"),
            ("embedding_non_finite", "embedding_dim_check", "embedding_encode", "embedding_non_finite"),
            ("llm_verify", "llm_verify", "embedding_dim_check", "llm_verify_failed"),
            ("rerank_init", "rerank_init", "llm_verify", "rerank_init_failed"),
            ("rerank_predict", "rerank_predict", "rerank_init", "rerank_predict_failed"),
            ("rerank_invalid_score", "rerank_predict", "rerank_init", "rerank_invalid_score"),
        ):
            with self.subTest(failure=failure):
                result = subprocess.run(
                    [sys.executable, str(DEPLOY / "model_probe.py")],
                    env={**self.environment, "PROBE_TEST_FAILURE": failure},
                    capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, b"")
                expected = {"ok": False, "stage": stage, "last_ok_stage": previous, "code": code}
                self.assertEqual(result.stdout.decode(), json.dumps(expected) + "\n")
                self.assertNotIn(b"secret-from-provider", result.stdout)
                self.assertEqual(service.probe_error(result.stdout, result.returncode).diagnostic, {
                    "stage": stage, "last_ok_stage": previous, "code": code,
                })
        for provider in ("remote", "rrf"):
            service.run_probe({
                **self.environment, "PROBE_TEST_FAILURE": "", "HINDSIGHT_API_RERANKER_PROVIDER": provider,
            })

    def test_timeout_and_excess_output_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "model_probe.py")
            script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
            with patch.object(service, "__file__", str(script)), patch.object(service, "PROBE_TIMEOUT", 0.1):
                with self.assertRaises(service.ApplyError) as raised:
                    service.run_probe(self.environment)
                self.assertEqual(raised.exception.diagnostic["code"], "probe_timeout")
            script.write_text("import os; os.write(1, b'secret' * 1024)\n", encoding="utf-8")
            with patch.object(service, "__file__", str(script)), self.assertRaises(service.ApplyError) as raised:
                service.run_probe(self.environment)
            self.assertEqual(raised.exception.diagnostic["code"], "probe_failed")
            self.assertNotIn("secret", str(raised.exception))

    def test_parser_drops_unknown_fields_and_codes(self):
        for output in (
            b"secret-from-provider",
            b'{"ok":false,"stage":"llm_verify","last_ok_stage":"embedding_dim_check","code":"secret"}',
            b'{"ok":false,"stage":"llm_verify","last_ok_stage":"secret","code":"llm_verify_failed"}',
            b'{"ok":false,"stage":"secret","last_ok_stage":null,"code":"llm_verify_failed"}',
            b'{"ok":false,"stage":"llm_verify","last_ok_stage":"embedding_dim_check","code":"llm_verify_failed","key":"secret"}',
        ):
            error = service.probe_error(output, 1)
            self.assertEqual(error.diagnostic, {"code": "probe_failed", "stage": None, "last_ok_stage": None})
            self.assertNotIn("secret", str(error))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "HINDSIGHT_API_LLM_MODEL": "chat",
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "embedding",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "2",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "secret",
        }

    def test_model_config_cannot_override_process_database_or_management_settings(self):
        for key in ("PATH", "HINDSIGHT_API_DATABASE_URL", "ASTRBOT_MEMORY_MANAGEMENT_KEY"):
            with self.subTest(key=key), self.assertRaises(service.ConfigurationError):
                service.configuration({**self.environment, key: "override"})
        first = service.configuration(self.environment)
        rotated = service.configuration({**self.environment, "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "new-secret"})
        self.assertEqual(first["embedding_fingerprint"], rotated["embedding_fingerprint"])

    def test_lost_response_reuses_ready_configuration_and_fingerprint_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")

            def start(candidate):
                runtime.process = Mock()
                runtime.process.poll.return_value = None

            with patch.object(runtime, "start", side_effect=start) as launch, patch.object(runtime, "stop") as stop, \
                    patch.object(runtime, "wait_ready"), patch.object(runtime, "healthy", return_value=True), \
                    patch.object(service, "run_probe") as probe:
                first = runtime.apply(self.environment)
                self.assertEqual(runtime.apply(dict(reversed(list(self.environment.items())))), first)
                self.assertEqual((launch.call_count, stop.call_count, probe.call_count), (1, 1, 1))
                self.assertFalse(first["applying"])
                self.assertEqual(first["configuration_fingerprint"], runtime.fingerprint(self.environment))
                changed = {**self.environment, "HINDSIGHT_API_LLM_MODEL": "other-chat"}
                self.assertNotEqual(runtime.fingerprint(changed), first["configuration_fingerprint"])
                rotated = {**self.environment, "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "new-secret"}
                self.assertNotEqual(runtime.fingerprint(rotated), first["configuration_fingerprint"])
            with patch.object(service.Runtime, "start"), patch.object(service.Runtime, "healthy", return_value=False):
                reloaded = service.Runtime(directory, "service-key", "012345abcdef")
                self.assertEqual(reloaded.status()["configuration_fingerprint"], first["configuration_fingerprint"])
                self.assertNotIn("secret", json.dumps(reloaded.status()))

    def test_existing_relay_configuration_reloads_and_reuses_equivalent_loopback_without_restarting(self):
        old = {
            **self.environment,
            "HINDSIGHT_API_LLM_MODEL": "chat-http://memory-models:8765",
            "HINDSIGHT_API_LLM_BASE_URL": "http://memory-models:8765/custom",
            "HINDSIGHT_API_LLM_API_KEY": "__ASTRBOT_MEMORY_TRANSPORT_KEY__",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "http://memory-models:8765/v1",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "__ASTRBOT_MEMORY_TRANSPORT_KEY__",
            "ASTRBOT_MEMORY_MODEL_CONFIG": json.dumps({"embedding": {"base_url": "https://upstream.invalid/v1"}}),
            "ASTRBOT_MEMORY_EMBEDDING_IDENTITY": json.dumps({"model": "embedding"}),
        }
        new = {**old, "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "http://127.0.0.1:8765/v1"}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "models.json").write_text(json.dumps(service.configuration(old)))
            with patch.object(service.Runtime, "start") as start:
                runtime = service.Runtime(directory, "service-key", "012345abcdef")
            start.assert_called_once_with(service.configuration(old))
            values, descriptors = runtime.environment(runtime.active)
            for field in ("HINDSIGHT_API_LLM_MODEL", "HINDSIGHT_API_LLM_BASE_URL", "HINDSIGHT_API_LLM_API_KEY"):
                self.assertEqual(values[field], old[field])
            self.assertEqual(values["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"], new["HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL"])
            self.assertEqual(values["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"], "service-key")
            self.assertEqual(descriptors, json.loads(old["ASTRBOT_MEMORY_MODEL_CONFIG"]))
            identity = runtime.active["embedding_fingerprint"]
            with patch.object(runtime, "status", wraps=runtime.status), patch.object(runtime, "stop") as stop:
                runtime.process = Mock()
                runtime.process.poll.return_value = None
                with patch.object(runtime, "healthy", return_value=True):
                    self.assertEqual(runtime.apply(new)["configuration_fingerprint"], runtime.fingerprint(new))
                    self.assertEqual(runtime.apply(old)["configuration_fingerprint"], runtime.fingerprint(old))
                stop.assert_not_called()
            self.assertEqual(runtime.active["embedding_fingerprint"], identity)

    def test_status_exposes_pending_configuration_without_claiming_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")
            entered, release = threading.Event(), threading.Event()
            results = []

            def probe(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("probe wait expired")

            def start(candidate):
                runtime.process = Mock()
                runtime.process.poll.return_value = None

            with patch.object(runtime, "start", side_effect=start), patch.object(runtime, "stop"), \
                    patch.object(runtime, "wait_ready"), patch.object(runtime, "healthy", return_value=True), \
                    patch.object(service, "run_probe", side_effect=probe):
                worker = threading.Thread(target=lambda: results.append(runtime.apply(self.environment)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    status = runtime.status()
                    self.assertTrue(status["applying"])
                    self.assertFalse(status["ready"])
                    self.assertEqual(status["applying_fingerprint"], runtime.fingerprint(self.environment))
                    with self.assertRaisesRegex(service.ConfigurationError, "正在应用"):
                        runtime.apply(self.environment)
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(results[0]["ready"])
                self.assertFalse(results[0]["applying"])

    def test_failed_application_restores_runtime_and_keeps_saved_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")
            def start(candidate):
                runtime.process = Mock()
                runtime.process.poll.return_value = None
            with patch.object(runtime, "start", side_effect=start) as launch, patch.object(runtime, "stop"), patch.object(runtime, "wait_ready"), patch.object(runtime, "healthy", return_value=True), patch.object(service, "run_probe") as probe:
                runtime.apply(self.environment)
                saved = (Path(directory) / "models.json").read_bytes()
                self.assertEqual(runtime.status()["configured"], True)
                self.assertNotIn("secret", json.dumps(runtime.status()))
                probe.side_effect = subprocess.CalledProcessError(1, "probe")
                with self.assertRaisesRegex(service.ConfigurationError, "模型连接或应用失败"):
                    runtime.apply({**self.environment, "HINDSIGHT_API_LLM_MODEL": "broken"})
                self.assertEqual((Path(directory) / "models.json").read_bytes(), saved)
                self.assertEqual(launch.call_args.args[0]["environment"]["HINDSIGHT_API_LLM_MODEL"], "chat")
                calls = launch.call_count
                with self.assertRaisesRegex(service.ConfigurationError, "需要迁移"):
                    runtime.apply({**self.environment, "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "3"})
                self.assertEqual(launch.call_count, calls)

    def test_apply_reports_safe_probing_and_startup_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")
            runtime.process = Mock()
            missing_vector = service.ApplyError("embedding_empty", "embedding_encode", "embedding_init")
            with patch.object(runtime, "start"), patch.object(runtime, "stop"), \
                    patch.object(service, "run_probe") as probe:
                probe.side_effect = missing_vector
                with self.assertRaisesRegex(service.ConfigurationError, "空向量"):
                    runtime.apply(self.environment)
                self.assertEqual(runtime.status()["diagnostic"], missing_vector.diagnostic)
                self.assertEqual(runtime.status()["error"], str(missing_vector))
                probe.side_effect = None
                for poll, clock, code in (
                    (1, [0, 1], "service_exited"),
                    (None, [0, 181], "service_start_timeout"),
                ):
                    with patch.object(runtime.process, "poll", return_value=poll), \
                            patch.object(service.time, "monotonic", side_effect=clock):
                        with self.assertRaises(service.ApplyError) as raised:
                            runtime.apply(self.environment)
                    self.assertEqual(raised.exception.diagnostic["code"], code)
                    self.assertEqual(runtime.status()["diagnostic"]["code"], code)
                runtime.process.poll.return_value = None
                with patch.object(runtime, "healthy", return_value=True):
                    result = runtime.apply(self.environment)
                self.assertEqual(result["error"], "")
                self.assertIsNone(result["diagnostic"])

    def test_existing_installation_fingerprint_is_checked_before_first_model_start(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef", "f" * 64)
            with patch.object(runtime, "stop") as stop, self.assertRaisesRegex(service.ConfigurationError, "需要迁移"):
                runtime.apply(self.environment)
            stop.assert_not_called()

    def test_shutdown_outlasts_upstream_drain_before_forced_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")
            process = runtime.process = Mock(pid=12345)
            process.poll.return_value = None
            with patch.object(service.os, "killpg") as kill:
                runtime.stop()
            kill.assert_called_once_with(12345, service.signal.SIGTERM)
            process.wait.assert_called_once_with(timeout=service.STOP_TIMEOUT)
            self.assertGreater(service.STOP_TIMEOUT, 60)
            self.assertIsNone(runtime.process)

    def test_shutdown_kills_only_its_process_group_after_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = service.Runtime(directory, "service-key", "012345abcdef")
            process = runtime.process = Mock(pid=12345)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("supervisor", service.STOP_TIMEOUT), None]
            with patch.object(service.os, "killpg") as kill:
                runtime.stop()
            self.assertEqual([call.args for call in kill.call_args_list], [
                (12345, service.signal.SIGTERM), (12345, service.signal.SIGKILL),
            ])
            self.assertIsNone(runtime.process)
