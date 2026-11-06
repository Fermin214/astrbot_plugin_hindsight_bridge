"""Reproduce the old SQL permission failure and repair its owned data in Docker."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "34b8b586f226e24834873bae3613c4946c7c45d3"
LEGACY_IMAGE = "astrbot-hindsight-postgres:17-pgroonga4.0.8-pgvector0.8.6"
SPEC = importlib.util.spec_from_file_location("installer", ROOT / "core/installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def run(*command, check=True, **options):
    try:
        result = subprocess.run(command, check=False, **options)
    except subprocess.TimeoutExpired:
        raise RuntimeError(command[0] + " timed out; inspect the stage log") from None
    if check and result.returncode:
        # Generated shell commands embed private installation credentials.
        raise RuntimeError(command[0] + " failed with exit code " + str(result.returncode) + "; inspect the stage log")
    return result


def output(*command):
    return run(*command, capture_output=True, text=True).stdout.strip()


def image_id(name):
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", name],
                            capture_output=True, text=True)
    return result.stdout.strip() if not result.returncode else ""


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def verify_fresh(directory, environment):
    identifier = secrets.token_hex(6)
    state = {"id": identifier, "database_password": secrets.token_hex(32), "api_key": secrets.token_hex(32),
             "management_key": secrets.token_hex(32), "api_port": free_port(), "management_port": free_port(),
             "control_plane_port": free_port()}
    project = "astrbot-memory-" + identifier
    installed = directory / "installations" / project

    def compose(*arguments):
        return output("docker", "compose", "--project-name", project, "--project-directory", str(installed), *arguments)

    try:
        with (directory / "fresh-install.log").open("w") as log:
            run("bash", "-c", installer.installation(ROOT, state)["command"], env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        database = compose("ps", "-a", "-q", "memory-db")
        logged = run("docker", "logs", database, capture_output=True, text=True)
        logs = logged.stdout + logged.stderr
        (directory / "fresh-database.log").write_text(logs)
        assert "Permission denied" not in logs, logs
        assert "10-hindsight.sql" in logs, logs
        assert "PostgreSQL init process complete" in logs, logs
        assert output("docker", "inspect", "--format", "{{.Image}}", database) == image_id(installer.POSTGRES_IMAGE)
        assert compose("exec", "-T", "memory-db", "psql", "-U", "hindsight", "-d", "hindsight", "-qAt", "-c",
                       "SELECT count(*) FROM pg_extension WHERE extname IN ('vector','pgroonga');") == "2"
        return {"fresh_volume_initialization_completed": True, "fresh_volume_permission_errors": False}
    finally:
        if (installed / "identity").exists():
            with (directory / "fresh-cleanup.log").open("w") as log:
                run("bash", "-c", installer.uninstallation(state)["command"], env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=180)


def verify(directory):
    if image_id(LEGACY_IMAGE) or image_id(installer.POSTGRES_IMAGE):
        raise RuntimeError("Cold-start verification requires an isolated Docker daemon without bridge database images")
    identifier = secrets.token_hex(6)
    state = {"id": identifier, "database_password": secrets.token_hex(32), "api_key": secrets.token_hex(32),
             "management_key": secrets.token_hex(32), "api_port": free_port(), "management_port": free_port(),
             "control_plane_port": free_port()}
    project = "astrbot-memory-" + identifier
    environment = {**os.environ, "HINDSIGHT_INSTALL_ROOT": str(directory / "installations")}
    installed = directory / "installations" / project
    created_images = {}

    def compose(*arguments):
        return output("docker", "compose", "--project-name", project, "--project-directory", str(installed), *arguments)

    def sql(statement):
        return compose("exec", "-T", "memory-db", "psql", "-U", "hindsight", "-d", "hindsight",
                       "-v", "ON_ERROR_STOP=1", "-qAt", "-c", statement)

    def install(command, name, *, allow_failure=False):
        with (directory / (name + ".log")).open("w") as log:
            return run("bash", "-c", command, check=not allow_failure, env=environment, stdin=subprocess.DEVNULL,
                       stdout=log, stderr=subprocess.STDOUT, timeout=900).returncode

    try:
        with tempfile.TemporaryDirectory(prefix="bridge-startup-") as temporary:
            legacy = Path(temporary)
            for name in ("Dockerfile", "extensions.sql"):
                path = legacy / "deploy/postgres" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(run("git", "show", BASELINE + ":deploy/postgres/" + name, capture_output=True).stdout)
                path.chmod(0o600)
            for name in ("managed_service.py", "model_probe.py", "model_transport.py", "HINDSIGHT-LICENSE"):
                (legacy / "deploy" / name).write_bytes(run("git", "show", BASELINE + ":deploy/" + name, capture_output=True).stdout)
            with (directory / "legacy-build.log").open("w") as log:
                run("docker", "build", "--label", "hindsight-bridge.test=" + project, "-t", LEGACY_IMAGE,
                    str(legacy / "deploy/postgres"), stdout=log, stderr=subprocess.STDOUT, timeout=900)
            created_images[LEGACY_IMAGE] = image_id(LEGACY_IMAGE)
            old = {}
            exec(compile(run("git", "show", BASELINE + ":core/installer.py", capture_output=True).stdout,
                         "baseline-installer.py", "exec"), old)
            legacy_exit = install(old["installation"](legacy, state)["command"], "legacy-install", allow_failure=True)

        database = compose("ps", "-a", "-q", "memory-db")
        logged = run("docker", "logs", database, capture_output=True, text=True)
        logs = logged.stdout + logged.stderr
        (directory / "legacy-database.log").write_text(logs)
        assert "/docker-entrypoint-initdb.d/10-hindsight.sql: Permission denied" in logs, logs
        deadline = time.monotonic() + 120
        while True:
            try:
                assert sql("SELECT 1;") == "1"
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        assert output("docker", "exec", database, "stat", "-c", "%a", "/docker-entrypoint-initdb.d/10-hindsight.sql") == "600"
        sql("CREATE TABLE bridge_install_sentinel (id integer PRIMARY KEY); INSERT INTO bridge_install_sentinel VALUES (44);")
        credential_digest = hashlib.sha256((installed / ".env").read_bytes()).hexdigest()
        command = installer.installation(ROOT, state)["command"]
        install(command, "repaired-install")
        created_images[installer.POSTGRES_IMAGE] = image_id(installer.POSTGRES_IMAGE)
        database = compose("ps", "-q", "memory-db")
        assert output("docker", "inspect", "--format", "{{.Image}}", database) == created_images[installer.POSTGRES_IMAGE]
        assert output("docker", "exec", database, "stat", "-c", "%a", "/docker-entrypoint-initdb.d/10-hindsight.sql") == "644"
        assert sql("SELECT count(*) FROM pg_extension WHERE extname IN ('vector','pgroonga');") == "2"
        assert sql("SELECT id FROM bridge_install_sentinel;") == "44"
        api = compose("ps", "-q", "memory-api")
        status = json.loads(compose("exec", "-T", "memory-api", "python", "-c",
            "import json,os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8766/status',headers={'Authorization':'Bearer '+os.environ['ASTRBOT_MEMORY_MANAGEMENT_KEY']}); print(urllib.request.urlopen(r,timeout=5).read().decode())"))
        assert status["installation_id"] == identifier and not status["configured"] and not status["ready"]
        assert status["configuration_fingerprint"] == "" and not status["applying"]
        install(command, "repeated-install")
        assert compose("ps", "-q", "memory-api") == api
        assert sql("SELECT id FROM bridge_install_sentinel;") == "44"
        assert hashlib.sha256((installed / ".env").read_bytes()).hexdigest() == credential_digest
        result = {"baseline": BASELINE, "hindsight": "1.2.0", "strict_umask": "0077",
                  "legacy_install_exit_code": legacy_exit,
                  "old_permission_failure_reproduced": True, "repaired_sql_mode": "0644",
                  "extensions": ["vector", "pgroonga"], "sentinel_preserved": True,
                  "credentials_preserved": True, "unconfigured_retry_reuses_api_container": True,
                  "management_identity_verified": True, "management_configuration_fingerprint_supported": True,
                  "legacy_image_id": created_images[LEGACY_IMAGE], "repaired_image_id": created_images[installer.POSTGRES_IMAGE]}
        result.update(verify_fresh(directory, environment))
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        if installed.exists() and (installed / "identity").exists():
            database = compose("ps", "-a", "-q", "memory-db")
            if database:
                logged = run("docker", "logs", database, capture_output=True, text=True, check=False)
                (directory / "final-database.log").write_text(logged.stdout + logged.stderr)
            install(installer.uninstallation(state)["command"], "cleanup")
        # Shared names are removed only if this pristine-daemon run created them.
        for name, identifier in created_images.items():
            if identifier and image_id(name) == identifier:
                run("docker", "image", "rm", name, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    verify(args.output.resolve())
    print("Owned installation startup checks passed.")


if __name__ == "__main__":
    main()
