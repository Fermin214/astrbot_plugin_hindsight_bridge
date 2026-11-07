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
from unittest.mock import patch

from plugins.astrbot_plugin_hindsight_bridge.core.installer import POSTGRES_IMAGE, installation, uninstallation

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
  *"同版本修复仅支持1.2.0"*) case "$TEST_FAIL_STAGE" in repair_version|initial_retry) exit 50 ;; esac; cat >/dev/null ;;
  *"不能续接旧版本或已配置的服务"*) [ "$TEST_FAIL_STAGE" != repair_version ] || exit 50; cat >/dev/null ;;
  *"exec -T memory-db psql"*) [ "$TEST_FAIL_STAGE" != extensions ] || exit 54; printf '%s\\n' "${TEST_EXTENSIONS:-2}" ;;
  *"up -d --no-build --wait --wait-timeout 180 memory-db"*) [ "$TEST_FAIL_STAGE" != database_start ] || exit 55 ;;
  "image inspect "*) [ "$TEST_IMAGE_CACHE" != old ] || exit 1 ;;
  "pull ghcr.io/vectorize-io/hindsight:1.2.0-slim") [ "$TEST_FAIL_STAGE" != pull ] || exit 51 ;;
  *"exec -T memory-api python -c"*"models.json"*) printf '%s\n' '{"environment":{}}' ;;
  *"exec -T memory-api python -c"*) cat >/dev/null ;;
  *"exec -T memory-db pg_dump"*) [ "$TEST_FAIL_STAGE" != pg_dump ] || exit 52; printf '%s\n' fixture-dump ;;
  *"exec -T memory-db pg_restore --list"*) [ "$TEST_FAIL_STAGE" != pg_restore ] || exit 53; printf '%s\n' fixture-inventory ;;
  "ps -q") printf '%s\\n' astrbot ;;
  "ps -q --filter"*) [ -z "$TEST_OWNED_PORT" ] || printf '%s\\n' owned ;;
  "inspect --format {{.Config.Hostname}}"*) printf '%s\\n' astrbot-host ;;
  'inspect --format {{range $k,$v := .NetworkSettings.Networks}}'*) printf '%s\\n' "${TEST_NETWORKS:-first second}" ;;
  "inspect --format {{.HostConfig.NetworkMode}}"*) printf '%s\\n' "${TEST_NETWORK_MODE:-default}" ;;
  "inspect --format {{range .NetworkSettings.Ports}}"*) printf '127.0.0.1 %s\\n' "$TEST_OWNED_PORT" ;;
  "network inspect --format {{.Driver}} {{.Internal}} host") printf '%s\\n' 'host false' ;;
  "network inspect --format {{(index .IPAM.Config 0).Gateway}} bridge") printf '%s\\n' '172.17.0.1' ;;
  "network inspect"*) printf '%s\\n' 'bridge false' ;;
esac
exit 0
''')
    executable.chmod(0o755)
    return {
        **os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "HINDSIGHT_INSTALL_ROOT": str(root), "TEST_LOG": str(root / "calls"),
        "TEST_OWNED_PORT": "", "HINDSIGHT_ASTRBOT_NETWORK": "", "TEST_IMAGE_CACHE": "",
    }


class InstallerTests(unittest.TestCase):
    def test_host_and_default_bridge_bind_stable_addresses_without_changing_astrbot_network(self):
        for mode in ("host", "bridge"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = {**install_environment(root), "TEST_NETWORKS": mode, "TEST_NETWORK_MODE": mode}
                state = {**STATE, **free_ports()}
                generated = installation(ROOT, state, container_hostname="astrbot-host", setup_path="/plugin/setup.json")
                completed = subprocess.run(["bash"], input=script(generated), env=env, text=True, capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                target = root / ("astrbot-memory-" + STATE["id"])
                compose = (target / "compose.yaml").read_text()
                calls = (root / "calls").read_text()
                self.assertNotIn("0.0.0.0", compose)
                self.assertFalse((target / "compose.override.yaml").exists())
                if mode == "host":
                    self.assertIn(f"127.0.0.1:{state['api_port']}:8888", compose)
                    self.assertIn(f"127.0.0.1:{state['management_port']}:8766", compose)
                    self.assertNotIn("network connect", calls)
                else:
                    self.assertIn(f"172.17.0.1:{state['api_port']}:8888", compose)
                    self.assertIn(f"172.17.0.1:{state['management_port']}:8766", compose)
                    self.assertIn(f"http://172.17.0.1:{state['api_port']}", calls)
                self.assertIn("managed-network.json", calls)
                self.assertNotIn("network connect", calls)
                self.assertNotIn("network disconnect", calls)

    def test_first_start_failure_resumes_same_identity_and_checks_extensions_before_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = install_environment(root)
            state = {**STATE, **free_ports()}
            generated = script(installation(ROOT, state))
            failed = subprocess.run(["bash"], input=generated, env={**env, "TEST_FAIL_STAGE": "database_start"},
                                    text=True, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("数据库启动失败", failed.stderr)
            self.assertNotIn("安装完成", failed.stdout)
            target = root / ("astrbot-memory-" + STATE["id"])
            original = (target / ".env").read_bytes()
            (root / "calls").write_text("")
            resumed = subprocess.run(["bash"], input=generated, env={**env, "TEST_FAIL_STAGE": "initial_retry"},
                                     text=True, capture_output=True)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual((target / ".env").read_bytes(), original)
            self.assertEqual((target / "postgres/extensions.sql").stat().st_mode & 0o777, 0o644)
            calls = (root / "calls").read_text()
            self.assertIn("run --rm --no-deps --entrypoint python memory-api", calls)
            self.assertLess(calls.index("exec -T memory-db psql"), calls.index("--wait-timeout 600"))
            self.assertNotIn("down", calls)

    def test_old_cache_does_not_skip_revised_build_and_bad_extensions_cannot_report_success(self):
        for failure in ("", "extensions", "missing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = {**install_environment(root), "TEST_IMAGE_CACHE": "old", "TEST_FAIL_STAGE": failure,
                       "TEST_EXTENSIONS": "1" if failure == "missing" else "2"}
                result = subprocess.run(["bash"], input=script(installation(ROOT, {**STATE, **free_ports()})),
                                        env=env, text=True, capture_output=True)
                calls = (root / "calls").read_text()
                self.assertIn("image inspect " + POSTGRES_IMAGE, calls)
                self.assertIn("build memory-db", calls)
                self.assertNotIn("build memory-api", calls)
                if failure:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("安装完成", result.stdout)
                    self.assertNotIn("--wait-timeout 600", calls)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgrade_waits_for_api_and_rejects_wrong_version_or_startup_timeout(self):
        from plugins.astrbot_plugin_hindsight_bridge.core.installer import UPGRADE_AFTER

        code = re.search(r'python -c "(.*?)" < /dev/null', UPGRADE_AFTER, re.S)[1]
        with patch("urllib.request.urlopen", side_effect=[OSError("not ready"), io.BytesIO(b'{"api_version":"1.2.0"}')]) as request, patch("time.sleep"):
            exec(code, {})
            self.assertEqual(request.call_count, 2)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"api_version":"1.1.0"}')):
            with self.assertRaises(RuntimeError):
                exec(code, {})
        with patch("urllib.request.urlopen", side_effect=OSError("not ready")), patch("time.monotonic", side_effect=[0, 181]):
            with self.assertRaises(RuntimeError):
                exec(code, {})

    def test_encoded_pipeline_survives_docker_exec_reading_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = install_environment(root)
            state = {**STATE, **free_ports()}
            subprocess.run(["bash", "-c", installation(ROOT, state)["command"]], env=env, check=True, capture_output=True)
            result = subprocess.run(["bash", "-c", installation(ROOT, state, upgrade=True)["command"]],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("服务已升级到 1.2.0", result.stdout)
            self.assertTrue(list(root.glob("*-backups/upgrade-*/hindsight.dump")))
            result = subprocess.run(["bash", "-c", installation(ROOT, state)["command"]],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("安装完成", result.stdout)
    def test_same_version_repair_cannot_replace_old_or_unknown_existing_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = install_environment(root)
            state = {**STATE, **free_ports()}
            generated = script(installation(ROOT, state))
            result = subprocess.run(["bash"], input=generated, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = root / ("astrbot-memory-" + STATE["id"])
            before = (target / "compose.yaml").read_bytes()
            (root / "calls").write_text("")
            result = subprocess.run(["bash"], input=generated, env={**env, "TEST_FAIL_STAGE": "repair_version"}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target / "compose.yaml").read_bytes(), before)
            self.assertNotIn(" up ", (root / "calls").read_text())
    def test_upgrade_backup_failures_do_not_replace_original_service_files(self):
        for failure in ("pull", "pg_dump", "pg_restore"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = install_environment(root)
                state = {**STATE, **free_ports()}
                original = installation(ROOT, state)
                result = subprocess.run(["bash"], input=script(original), env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                target = root / ("astrbot-memory-" + STATE["id"])
                before = (target / "compose.yaml").read_bytes()
                (root / "calls").write_text("")
                result = subprocess.run(["bash"], input=script(installation(ROOT, state, upgrade=True)),
                                        env={**env, "TEST_FAIL_STAGE": failure}, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((target / "compose.yaml").read_bytes(), before)
                calls = (root / "calls").read_text()
                self.assertNotIn(" up ", calls)
                if failure == "pull":
                    self.assertNotIn(" stop ", calls)
                else:
                    self.assertTrue(list(root.glob("*-backups/upgrade-*")))

    def test_upgrade_script_backs_up_before_replacement_and_keeps_backup_on_failure(self):
        generated = script(installation(ROOT, STATE, upgrade=True))
        subprocess.run(["bash", "-n"], input=generated, text=True, check=True)
        before = generated.index('pg_dump -U hindsight')
        replacement = generated.index('cp -R "$stage/." "$target/"')
        self.assertLess(generated.index('stop --timeout 90 memory-api'), before)
        self.assertLess(before, replacement)
        self.assertIn('pg_restore --list', generated)
        self.assertIn('models.json', generated)
        self.assertIn('upgrade-1.1.0-', generated)
        self.assertNotIn('down -v', generated)
        self.assertNotIn('docker.sock', generated)

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

    def test_fixed_image_and_shutdown_grace_match_both_installation_paths(self):
        compose = assets(installation(ROOT, STATE))["compose.yaml"]
        self.assertEqual(compose.count("image: ghcr.io/vectorize-io/hindsight:1.2.0-slim"), 2)
        for material in (compose, (ROOT / "deploy/compose.yaml").read_text()):
            self.assertIn("image: " + POSTGRES_IMAGE, material)
            grace = int(re.search(r"stop_grace_period: (\d+)s", material)[1])
            self.assertGreater(grace, 75)
        self.assertIn("docker stop --time 90", script(uninstallation(STATE)))
        self.assertIn("COPY --chmod=0644 extensions.sql", (ROOT / "deploy/postgres/Dockerfile").read_text())

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
