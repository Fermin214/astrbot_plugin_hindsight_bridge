"""Model replacement and secret isolation failures beyond live setup acceptance."""

import importlib.util
import json
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
                    patch.object(service.subprocess, "run") as probe:
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
                    patch.object(service.subprocess, "run", side_effect=probe):
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
            with patch.object(runtime, "start", side_effect=start) as launch, patch.object(runtime, "stop"), patch.object(runtime, "wait_ready"), patch.object(runtime, "healthy", return_value=True), patch.object(service.subprocess, "run") as probe:
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
