"""Protect existing embedding identities while introducing protocol transport."""

import base64
import gzip
import io
from pathlib import Path
import re
import tarfile
import unittest

from plugins.astrbot_plugin_hindsight_bridge.core.installer import installation
import test_providers as fixtures


ROOT = Path(__file__).resolve().parent.parent
STATE = {
    "id": "012345abcdef", "database_password": "database-secret",
    "api_key": "service-secret", "panel_key": "panel-secret",
}


def assets(result):
    script = gzip.decompress(base64.b64decode(result["command"].split("'")[3])).decode()
    encoded = re.search(r"printf '%s' '([^']+)' \| base64 -d \| tar", script)[1]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as archive:
        return {item.name: archive.extractfile(item).read().decode() for item in archive}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ProviderTests()
        fixture.setUp()
        self.fixture = fixture

    def build(self):
        env = self.fixture.catalog.resolve(self.fixture.selection)
        return installation(ROOT, STATE, env, container_hostname="astrbot-host")

    def test_relay_and_key_rotation_preserve_existing_openai_identity(self):
        original = self.build()
        self.fixture.embedding.provider_config.update(
            embedding_dimensions_mode="never", embedding_api_key="rotated-key",
            custom_headers={"X-Note": "It's $literal\\value"},
        )
        relayed = self.build()
        self.assertEqual(original["embedding_fingerprint"], relayed["embedding_fingerprint"])
        files = assets(relayed)
        self.assertIn("memory-models:", files["compose.yaml"])
        self.assertNotIn("    ports:", files["compose.yaml"])
        self.assertNotIn("__ASTRBOT_MEMORY_TRANSPORT_KEY__", files[".env"])
        self.assertIn("ASTRBOT_MEMORY_MODEL_CONFIG=", files[".env"])
        self.assertIn("http://memory-models-012345abcdef:8765", files[".env"])
        self.assertIn("aliases: [memory-models-012345abcdef]", files["compose.yaml"])
        self.assertIn("model_transport.py", files)

    def test_model_dimensions_and_native_protocol_changes_are_not_hidden_by_relay_url(self):
        first = self.build()["embedding_fingerprint"]
        self.fixture.embedding.adapter = "dashscope_embedding"
        native = self.build()["embedding_fingerprint"]
        self.assertNotEqual(first, native)
        self.fixture.embedding.provider_config["embedding_dimensions"] = 512
        self.assertNotEqual(native, self.build()["embedding_fingerprint"])

    def test_native_addresses_are_checked_before_internal_url_replacement(self):
        self.fixture.embedding.adapter = "ollama_embedding"
        self.fixture.embedding.provider_config["embedding_api_base"] = "http://127.0.0.1:11434"
        with self.assertRaisesRegex(ValueError, "回环"):
            self.build()


if __name__ == "__main__":
    unittest.main()
