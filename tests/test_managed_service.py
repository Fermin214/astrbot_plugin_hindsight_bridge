"""Model replacement and secret isolation failures beyond live setup acceptance."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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
