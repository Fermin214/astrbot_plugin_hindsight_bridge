"""Installation ownership and removal boundaries beyond the live service check."""

import base64
import gzip
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest

from plugins.astrbot_plugin_hindsight_bridge.core.installer import installation, uninstallation

ROOT = Path(__file__).resolve().parent.parent
STATE = {
    "id": "012345abcdef", "database_password": "database-secret",
    "api_key": "service-secret", "management_key": "management-secret",
}


def script(result):
    return gzip.decompress(base64.b64decode(result["command"].split("'")[3])).decode()


def assets(result):
    encoded = re.search(r"printf '%s' '([^']+)' \| base64 -d \| tar", script(result))[1]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as archive:
        return {item.name: archive.extractfile(item).read().decode() for item in archive}


class InstallerTests(unittest.TestCase):
    def test_installation_has_no_model_credentials_or_model_selection(self):
        result = installation(ROOT, STATE, container_hostname="astrbot-host")
        files = assets(result)
        self.assertNotIn("HINDSIGHT_API_LLM_", files[".env"])
        self.assertNotIn("HINDSIGHT_API_EMBEDDINGS_", files[".env"])
        self.assertNotIn("    ports:", files["compose.yaml"])
        self.assertNotIn("docker.sock", files["compose.yaml"])
        self.assertIn("service/managed_service.py", files)
        self.assertEqual(result["management_url"], "http://memory-012345abcdef:8766")
        subprocess.run(["bash", "-n"], input=script(result), text=True, check=True)

    def test_native_installation_binds_management_only_to_loopback(self):
        files = assets(installation(ROOT, STATE))
        self.assertIn('127.0.0.1:${ASTRBOT_MEMORY_MANAGEMENT_PORT:-8889}:8766', files["compose.yaml"])

    def test_uninstall_removes_only_the_owned_directory_and_volume_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / ("astrbot-memory-" + STATE["id"])
            target.mkdir()
            (target / "identity").write_text(STATE["id"])
            unrelated = root / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep").write_text("keep")
            tools = root / "tools"
            tools.mkdir()
            executable = tools / "docker"
            executable.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$*" in
  "ps -aq"*) printf '%s\\n' owned-container ;;
  "inspect"*) printf '%s\\n' "${TEST_SERVICE:-memory-api}" ;;
  "volume ls"*) printf '%s\\n' astrbot-memory-012345abcdef_memory_postgres ;;
esac
''')
            executable.chmod(0o755)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "HINDSIGHT_INSTALL_ROOT": str(root), "TEST_LOG": str(root / "calls")}
            removal = script(uninstallation(STATE))
            # Unknown services and symlinked paths must fail before deleting data.
            rejected = subprocess.run(["bash"], input=removal, env={**env, "TEST_SERVICE": "other-app"}, text=True, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertTrue(target.exists())
            self.assertNotIn("stop --time", (root / "calls").read_text())
            subprocess.run(["bash"], input=removal, env=env, text=True, capture_output=True, check=True)
            self.assertFalse(target.exists())
            self.assertEqual((unrelated / "keep").read_text(), "keep")
            self.assertIn("volume rm astrbot-memory-012345abcdef_memory_postgres", (root / "calls").read_text())
            self.assertIn("label=com.docker.compose.project=astrbot-memory-012345abcdef", (root / "calls").read_text())
            target.symlink_to(unrelated, target_is_directory=True)
            rejected = subprocess.run(["bash"], input=removal, env=env, text=True, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertTrue((unrelated / "keep").exists())


if __name__ == "__main__":
    unittest.main()
