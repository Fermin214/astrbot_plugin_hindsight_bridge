"""Installation ownership and removal boundaries beyond the live service check."""

import base64
import gzip
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
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


def free_ports():
    with socket.socket() as first, socket.socket() as second, socket.socket() as third:
        sockets = (first, second, third)
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return dict(zip(("api_port", "management_port", "control_plane_port"),
                        (listener.getsockname()[1] for listener in sockets)))


def install_environment(root):
    tools = root / "tools"
    tools.mkdir()
    executable = tools / "docker"
    executable.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$*" in
  "ps -q") printf '%s\\n' astrbot ;;
  "ps -q --filter"*) [ -z "$TEST_OWNED_PORT" ] || printf '%s\\n' owned ;;
  "inspect --format {{.Config.Hostname}}"*) printf '%s\\n' astrbot-host ;;
  'inspect --format {{range $k,$v := .NetworkSettings.Networks}}'*) printf '%s\\n' first second ;;
  "inspect --format {{range .NetworkSettings.Ports}}"*) printf '127.0.0.1 %s\\n' "$TEST_OWNED_PORT" ;;
  "network inspect"*) printf '%s\\n' 'bridge false' ;;
esac
exit 0
''')
    executable.chmod(0o755)
    return {
        **os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "HINDSIGHT_INSTALL_ROOT": str(root), "TEST_LOG": str(root / "calls"),
        "TEST_OWNED_PORT": "", "HINDSIGHT_ASTRBOT_NETWORK": "",
    }


class InstallerTests(unittest.TestCase):
    def test_installation_has_no_model_credentials_or_model_selection(self):
        result = installation(ROOT, STATE, container_hostname="astrbot-host")
        files = assets(result)
        self.assertNotIn("HINDSIGHT_API_LLM_", files[".env"])
        self.assertNotIn("HINDSIGHT_API_EMBEDDINGS_", files[".env"])
        self.assertNotIn("${ASTRBOT_MEMORY_API_PORT", files["compose.yaml"])
        self.assertNotIn("${ASTRBOT_MEMORY_MANAGEMENT_PORT", files["compose.yaml"])
        self.assertIn('127.0.0.1:${ASTRBOT_MEMORY_CP_PORT:-9999}:9999', files["compose.yaml"])
        self.assertIn('HINDSIGHT_ENABLE_CP: "true"', files["compose.yaml"])
        self.assertIn("HINDSIGHT_CP_DATAPLANE_API_URL: http://127.0.0.1:8888", files["compose.yaml"])
        self.assertIn("HINDSIGHT_CP_DATAPLANE_API_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}", files["compose.yaml"])
        cp_key = re.search(r"HINDSIGHT_CP_ACCESS_KEY='([0-9a-f]{64})'", files[".env"])[1]
        self.assertNotIn(cp_key, STATE.values())
        self.assertNotIn(cp_key, {key: value for key, value in result.items() if key != "command"}.values())
        self.assertNotIn("docker.sock", files["compose.yaml"])
        self.assertIn("service/managed_service.py", files)
        self.assertEqual(result["management_url"], "http://memory-012345abcdef:8766")
        self.assertEqual(result["api_url"], "http://memory-012345abcdef:8888")
        self.assertEqual(result["browser_url"], "http://127.0.0.1:9999")
        self.assertEqual(result["control_plane_url"], result["browser_url"])
        self.assertEqual(result["control_plane_key_env"], "HINDSIGHT_CP_ACCESS_KEY")
        self.assertTrue(result["control_plane_key_file"].endswith("/astrbot-memory-012345abcdef/.env"))
        subprocess.run(["bash", "-n"], input=script(result), text=True, check=True)

    def test_native_installation_binds_management_only_to_loopback(self):
        files = assets(installation(ROOT, STATE))
        self.assertIn('127.0.0.1:${ASTRBOT_MEMORY_MANAGEMENT_PORT:-8889}:8766', files["compose.yaml"])
        self.assertIn('127.0.0.1:${ASTRBOT_MEMORY_API_PORT:-8888}:8888', files["compose.yaml"])
        result = installation(ROOT, {**STATE, "control_plane_port": 19999})
        self.assertEqual(result["browser_url"], "http://127.0.0.1:19999")
        result = installation(ROOT, STATE, container_hostname="astrbot-host", control_plane_url="https://memory.example.com")
        self.assertEqual(result["control_plane_url"], "https://memory.example.com")
        self.assertEqual(result["api_url"], "http://memory-012345abcdef:8888")
        result = installation(ROOT, {**STATE, "control_plane_url": "http://127.0.0.1:29999"})
        self.assertEqual(result["browser_url"], "http://127.0.0.1:29999")

    def test_invalid_or_overlapping_ports_are_rejected_before_generating_commands(self):
        for value in (0, 65536, True, "9999:9999", "9999\n", 8888):
            with self.subTest(value=value), self.assertRaises(ValueError):
                installation(ROOT, {**STATE, "control_plane_port": value})
        for address in ("http://key@localhost:9999", "http://localhost:9999?key=secret", "memory:9999"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                installation(ROOT, STATE, control_plane_url=address)

    def test_rerun_keeps_credentials_settings_and_one_astrbot_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = install_environment(root)
            state = {**STATE, **free_ports()}
            generated = installation(ROOT, state, container_hostname="astrbot-host")
            run = lambda result, environment=env: subprocess.run(
                ["bash"], input=script(result), env=environment, text=True, capture_output=True
            )
            completed = run(generated)
            self.assertEqual(completed.returncode, 0, completed.stderr + (root / "calls").read_text())
            target = root / ("astrbot-memory-" + STATE["id"])
            self.assertEqual((target / "service").stat().st_mode & 0o777, 0o755)
            for name in ("managed_service.py", "model_probe.py", "model_transport.py"):
                self.assertEqual((target / "service" / name).stat().st_mode & 0o777, 0o644)
            override = (target / "compose.override.yaml").read_text()
            self.assertIn('name: "first"', override)
            self.assertEqual(override.count("external: true"), 1)
            self.assertNotIn("second", override)
            configuration = target / ".env"
            configuration.write_text(configuration.read_text() + "CUSTOM_SETTING='kept'\n")
            original = configuration.read_bytes()
            (target / "keep-data").write_text("keep")
            regenerated = installation(ROOT, state, container_hostname="astrbot-host")
            self.assertNotEqual(assets(generated)[".env"], assets(regenerated)[".env"])
            completed = run(regenerated, {**env, "HINDSIGHT_ASTRBOT_NETWORK": "second"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(configuration.read_bytes(), original)
            self.assertEqual(configuration.stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / "keep-data").read_text(), "keep")
            self.assertIn('name: "second"', (target / "compose.override.yaml").read_text())
            cp_key = re.search(r"HINDSIGHT_CP_ACCESS_KEY='([0-9a-f]{64})'", configuration.read_text())[1]
            self.assertNotIn(cp_key, completed.stdout)
            lookup = subprocess.run(
                ["bash", "-c", completed.stdout.splitlines()[-1]],
                env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual(lookup.stdout.strip(), cp_key)
            # Older installs acquire CP fields without rotating their existing credentials.
            configuration.write_text("\n".join(
                row for row in original.decode().splitlines()
                if not row.startswith(("HINDSIGHT_CP_ACCESS_KEY=", "ASTRBOT_MEMORY_CP_PORT="))
            ) + "\n")
            completed = run(regenerated)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("HINDSIGHT_CP_ACCESS_KEY=", configuration.read_text())
            self.assertIn("CUSTOM_SETTING='kept'", configuration.read_text())
            saved = configuration.read_bytes()
            for change in ({"api_key": "other-secret"}, {"control_plane_port": state["control_plane_port"] + 1}):
                completed = run(installation(ROOT, {**state, **change}, container_hostname="astrbot-host"))
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(configuration.read_bytes(), saved)
            calls = (root / "calls").read_text()
            self.assertNotIn("volume rm", calls)
            self.assertNotIn("network connect", calls)
            self.assertNotIn("down", calls)

    def test_occupied_port_fails_before_copy_but_owned_listener_allows_rerun(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as listener:
            root = Path(temporary)
            env = install_environment(root)
            state = {**STATE, **free_ports()}
            listener.bind(("127.0.0.1", state["control_plane_port"]))
            listener.listen()
            generated = installation(ROOT, state)
            completed = subprocess.run(["bash"], input=script(generated), env=env, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(str(state["control_plane_port"]), completed.stderr)
            self.assertNotIn(" up ", (root / "calls").read_text())
            target = root / ("astrbot-memory-" + STATE["id"])
            self.assertFalse((target / ".env").exists())
            completed = subprocess.run(
                ["bash"], input=script(generated), env={**env, "TEST_OWNED_PORT": str(state["control_plane_port"])},
                text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manual_environment_generation_is_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "init-env.py").write_bytes((ROOT / "deploy/init-env.py").read_bytes())
            (root / ".env.example").write_bytes((ROOT / "deploy/.env.example").read_bytes())
            subprocess.run([sys.executable, str(root / "init-env.py")], check=True, capture_output=True)
            configuration = root / ".env"
            original = configuration.read_bytes()
            keys = re.findall(rb"^(?:POSTGRES_PASSWORD|HINDSIGHT_API_TENANT_API_KEY|HINDSIGHT_CP_ACCESS_KEY)=([0-9a-f]{64})$", original, re.M)
            self.assertEqual(len(set(keys)), 3)
            self.assertEqual(configuration.stat().st_mode & 0o777, 0o600)
            subprocess.run([sys.executable, str(root / "init-env.py")], check=True, capture_output=True)
            self.assertEqual(configuration.read_bytes(), original)
            compose = (ROOT / "deploy/compose.yaml").read_text()
            self.assertIn('HINDSIGHT_API_PORT: "8888"', compose)
            self.assertIn('HINDSIGHT_CP_PORT: "9999"', compose)
            self.assertIn('HINDSIGHT_ENABLE_CP: "true"', compose)

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
