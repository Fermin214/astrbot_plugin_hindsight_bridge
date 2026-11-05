"""Owned real-service old-library upgrade and host acceptance with synthetic peers."""

import argparse
import asyncio
import base64
from contextlib import closing
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tarfile
import threading
import time
import types
import uuid


ROOT = Path(__file__).resolve().parents[1]
for name, path in (("plugins", ROOT.parent), ("plugins.astrbot_plugin_hindsight_bridge", ROOT)):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
sys.path.insert(0, str(ROOT / "tests/integration"))
from backend_model import MARKER, Model
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.installer import installation, uninstallation
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox


def run(*args, **options):
    try:
        return subprocess.run(args, check=True, **options)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{args[0]} exited {error.returncode}; inspect owned test logs") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{args[0]} timed out; inspect owned test logs") from None


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def free_port():
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Acceptance:
    def __init__(self, output, baseline, scope="full"):
        if output.resolve().is_relative_to(ROOT) or output.exists():
            raise ValueError("Use a new output directory outside the repository")
        output.mkdir(mode=0o700)
        self.output, self.baseline, self.scope = output.resolve(), baseline, scope
        self.state = {"id": secrets.token_hex(6), "api_key": secrets.token_hex(24),
                      "management_key": secrets.token_hex(24), "database_password": secrets.token_hex(24),
                      **{name: free_port() for name in ("api_port", "management_port", "control_plane_port")}}
        self.project = "astrbot-memory-" + self.state["id"]
        self.directory = self.output / self.project
        self.model = None
        self.hosts = []
        self.report = {"complete": False, "baseline": baseline, "passed": [],
                       "project": self.project, "scope": scope,
                       "model_peer": "synthetic local OpenAI-compatible fixture",
                       "platform_peer": "synthetic actual OneBot SDK objects, no real account login",
                       "images": {}}

    def record(self, name):
        self.report["passed"].append(name)
        self.save()
        print("PASS " + name, flush=True)

    def save(self):
        (self.output / "report.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def command(self, result, label):
        script = gzip.decompress(base64.b64decode(result["command"].split("'")[3])).decode()
        with (self.output / (label + ".log")).open("w") as log:
            run("bash", input=script, text=True, env={**os.environ, "HINDSIGHT_INSTALL_ROOT": str(self.output)},
                stdout=log, stderr=subprocess.STDOUT, timeout=660)

    def service(self, kind):
        found = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project,
                       "--filter", "label=com.docker.compose.service=" + kind).split()
        if len(found) != 1:
            raise RuntimeError("Expected one owned " + kind)
        return found[0]

    async def ready(self, client, version):
        async with asyncio.timeout(200):
            while True:
                try:
                    await client.ensure_auth(force=True)
                    actual = (await client.request("GET", "/v1/version"))["api_version"]
                    assert actual == version, actual
                    self.report["images"][version] = docker("inspect", "--format", "{{.Image}}", self.service("memory-api"))
                    return
                except HindsightError:
                    await asyncio.sleep(0.5)

    async def completed(self, client, bank, identity):
        try:
            async with asyncio.timeout(120):
                while True:
                    op = await client.operation(bank, identity)
                    if op.status == "completed":
                        return
                    if op.status in {"failed", "cancelled"}:
                        raise RuntimeError("Native operation ended " + op.status + ": " + identity)
                    await asyncio.sleep(0.5)
        except Exception:
            summary = docker("exec", self.service("memory-db"), "psql", "-U", "hindsight", "-Atc",
                             "SELECT json_agg(t) FROM (SELECT operation_type,status,worker_id,retry_count,"
                             "next_retry_at,error_message,created_at FROM async_operations) t")
            (self.output / "failed-operation-diagnostics.json").write_text(summary)
            raise

    def old_install(self):
        snapshot = self.output / "baseline"
        snapshot.mkdir()
        archive = subprocess.check_output(["git", "archive", self.baseline, "core/installer.py", "deploy"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(snapshot, filter="data")
        spec = importlib.util.spec_from_file_location("baseline_installer", snapshot / "core/installer.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.command(module.installation(snapshot, self.state), "install-1.1.0")

    def start_model(self):
        network = self.project + "-clients"
        gateway = docker("network", "inspect", "--format", "{{(index .IPAM.Config 0).Gateway}}", network)
        token = secrets.token_hex(24)
        self.model = Model((gateway, 0), token)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        base = f"http://{gateway}:{self.model.server_port}/v1"
        return {
            "HINDSIGHT_API_LLM_PROVIDER": "openai", "HINDSIGHT_API_LLM_MODEL": "compatibility-fixture",
            "HINDSIGHT_API_LLM_BASE_URL": base, "HINDSIGHT_API_LLM_API_KEY": token,
            "HINDSIGHT_API_LLM_MAX_RETRIES": "0",
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
            "HINDSIGHT_API_EMBEDDINGS_MAX_RETRIES": "0",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "compatibility-fixture",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": base,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": token,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "64", "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
        }

    async def configure(self, environment):
        manager = HindsightClient(f'http://127.0.0.1:{self.state["management_port"]}',
                                  self.state["management_key"], timeout=200)
        try:
            return await manager.request("POST", "/models", body={"environment": environment})
        finally:
            await manager.close()

    async def check_api(self):
        client = HindsightClient(f'http://127.0.0.1:{self.state["api_port"]}', self.state["api_key"])
        bank = "abm-v1-compat-" + self.state["id"] + "-g0"
        queue = None
        try:
            if self.scope == "full":
                self.old_install()
            else:
                self.command(installation(ROOT, self.state), "install-1.2.0")
            environment = self.start_model()
            assert (await self.configure(environment))["ready"]
            await self.ready(client, "1.1.0" if self.scope == "full" else "1.2.0")
            if self.scope == "maintenance":
                queue = Outbox(self.output / "runtime", "compat", client.url,
                               credential_fingerprint=sha256(self.state["api_key"]),
                               control_directory=self.output / "control")
                scope = queue.scope(Incoming("aiocqhttp", "fixture", "bot", "user", "1", int(time.time()), MARKER))
                bank = scope["bank"]
            await client.create_bank(bank)
            await client.request("PATCH", client.bank_path(bank) + "/config", body={"updates": {
                "enable_observations": False,
            }})
            identity = str(uuid.uuid4())
            payload = {"items": [{"content": MARKER, "document_id": "compat-source"}],
                       "async": True, "operation_id": identity}
            await client.submit(bank, payload)
            await self.completed(client, bank, identity)
            created = await client.request("POST", client.bank_path(bank) + "/knowledge-base/pages", body={
                "name": "Background", "source_query": "Return a short background.", "max_tokens": 256,
                "trigger": {"refresh_after_consolidation": False, "fact_types": ["world"]},
            })
            await self.completed(client, bank, created["operation_id"])
            original = await client.background_page(bank, created["mental_model_id"])
            assert MARKER in original["content"]
            assert original["freshness"] == "unknown"
            if self.scope == "deletion":
                await self.delete_check(client, bank)
                return
            if self.scope == "maintenance":
                await self.maintenance_check(client, bank, queue, scope["scope_id"])
                await self.host_checks()
                await self.delete_check(client, bank)
                return
            self.record("baseline_1_1_real_retain_native_page_and_content")
            baseline_env = (self.directory / ".env").read_bytes()
            db = self.service("memory-db")
            with (self.output / "pre-upgrade.dump").open("wb") as file:
                run("docker", "exec", db, "pg_dump", "-U", "hindsight", "-Fc", "hindsight", stdout=file)
            self.record("old_database_backup_captured_before_upgrade")
            self.command(installation(ROOT, self.state, upgrade=True), "upgrade-1.2.0")
            await self.ready(client, "1.2.0")
            assert (self.directory / ".env").read_bytes() == baseline_env
            assert (await client.background_page(bank, created["mental_model_id"])) == original
            assert await client.document_exists(bank, "compat-source")
            assert (await client.operation(bank, identity)).status == "completed"
            await client.submit(bank, payload)
            await self.completed(client, bank, identity)
            assert len((await client.list_memories(bank))["items"]) == 1
            self.record("unmodified_installer_upgrade_preserves_credentials_models_bank_document_operation_page_and_idempotency")
            rows = await client.recall(bank, "Cedar-120", timestamp="2026-11-04T12:00:00Z")
            assert rows and MARKER in json.dumps(rows)
            self.record("upgraded_bank_real_recall_contract")
            await self.host_checks()
            await client.request("PATCH", client.bank_path(bank) + "/config", body={"updates": {"enable_observations": True}})
            retained = await client.request("POST", client.bank_path(bank) + "/memories", body={
                "items": [{"content": MARKER, "document_id": "new-1-2-source"}], "async": True,
            })
            await self.completed(client, bank, retained["operation_id"])
            async with asyncio.timeout(90):
                while (await client.stats(bank)).get("total_observations", 0) == 0:
                    await asyncio.sleep(0.5)
            self.record("new_1_2_retain_extraction_and_consolidation_with_schema_response")
            bad = {**environment, "HINDSIGHT_API_LLM_BASE_URL": "http://127.0.0.1:1"}
            try:
                await self.configure(bad)
                raise AssertionError("Broken model configuration was accepted")
            except HindsightError as error:
                assert error.code == "http_400"
            await self.ready(client, "1.2.0")
            assert (await client.background_page(bank, created["mental_model_id"])) == original
            self.record("failed_model_application_restores_previous_runtime_settings_and_data")
            self.model.extraction_started.clear()
            self.model.extraction_gate.clear()
            held = await client.request("POST", client.bank_path(bank) + "/memories", body={
                "items": [{"content": MARKER, "document_id": "restart-source"}], "async": True,
            })
            assert await asyncio.to_thread(self.model.extraction_started.wait, 15)
            restart = await asyncio.create_subprocess_exec("docker", "restart", "-t", "90", self.service("memory-api"),
                                                          stdout=asyncio.subprocess.DEVNULL)
            await asyncio.sleep(35)
            self.model.extraction_gate.set()
            assert await restart.wait() == 0
            await self.ready(client, "1.2.0")
            await self.completed(client, bank, held["operation_id"])
            self.record("inflight_native_retain_survives_managed_container_graceful_restart")
            await self.delete_check(client, bank)
            self.report["model_calls"] = self.model.counts
        finally:
            if self.model:
                self.model.extraction_gate.set()
                self.model.embedding_gate.set()
            if queue:
                queue.close()
            await client.close()

    async def maintenance_check(self, client, bank, queue, scope_id):
        await client.request("PATCH", client.bank_path(bank) + "/config", body={"updates": {"enable_observations": True}})
        self.model.embedding_started.clear()
        self.model.embedding_gate.clear()
        self.model.fail_embedding = True
        original = (await client.request("POST", client.bank_path(bank) + "/consolidate"))["operation_id"]
        assert await asyncio.to_thread(self.model.embedding_started.wait, 30)
        replacement = str(uuid.uuid4())
        # Clone a legitimate task into a deferred row in this synthetic bank. The
        # native worker must find it itself when the held model request fails.
        sql = (
            "INSERT INTO async_operations(operation_id,bank_id,operation_type,status,task_payload,next_retry_at,serialization_key) "
            f"SELECT '{replacement}',bank_id,operation_type,'pending',"
            f"jsonb_set(task_payload::jsonb,'{{operation_id}}',to_jsonb('{replacement}'::text)),"
            f"now()+interval '1 hour',serialization_key FROM async_operations WHERE operation_id='{original}';"
        )
        docker("exec", self.service("memory-db"), "psql", "-U", "hindsight", "-v", "ON_ERROR_STOP=1", "-Atc", sql)
        self.model.embedding_gate.set()
        try:
            async with asyncio.timeout(90):
                while (await client.operation(bank, original)).status != "cancelled":
                    await asyncio.sleep(0.5)
        except Exception:
            result = await client.request("GET", client.bank_path(bank) + "/operations/" + original)
            (self.output / "supersession-failure.json").write_text(json.dumps(result, indent=2))
            raise
        native = await client.request("GET", client.bank_path(bank) + "/operations/" + original)
        assert native["completed_at"] and native["result_metadata"]["superseded_by"] == replacement
        maintenance = Maintenance(queue, client)
        maintenance.begin(scope_id)
        pending = await maintenance.status(scope_id)
        assert not pending["ready"] and pending["remote_active"] >= 1
        self.model.fail_embedding = False
        await client.retry(bank, replacement)
        await self.completed(client, bank, replacement)
        state = await maintenance.status(scope_id)
        assert state["ready"] and state["remote_active"] == 0
        await maintenance.resume(scope_id)
        self.report["superseded_sample"] = {
            "operation": original, "replacement": replacement, "native_status": native["status"],
            "completed_at": native["completed_at"], "pending": pending, "completed": state,
        }
        self.record("native_transient_consolidation_supersession_blocks_pending_replacement_then_allows_maintenance_resume")

    async def host_checks(self):
        run(sys.executable, str(ROOT / "tools/build-test-package.py"), "--distribution", "--output", str(self.output))
        for version in ("4.29.1", "4.29.4"):
            name = "hb-compat-" + self.state["id"] + "-" + version.replace(".", "")
            self.hosts.append(name)
            with (self.output / ("host-" + version + ".log")).open("w") as log:
                run("docker", "run", "--name", name, "--label", "hindsight-bridge.test=" + self.state["id"],
                    "--network", "host", "--memory", "1536m", "-e", "ASTRBOT_ROOT=/verification",
                    "-e", "ASTRBOT_TEST_VERSION=" + version, "-e", "HINDSIGHT_TEST_VERSION=1.2.0",
                    "-e", "BACKGROUND_API_URL=" + f'http://127.0.0.1:{self.state["api_port"]}',
                    "-e", "BACKGROUND_API_KEY=" + self.state["api_key"], "-e", "BACKGROUND_OWNER=" + self.state["id"],
                    "-e", "TIKTOKEN_CACHE_DIR=/token-cache", "-v", "/tmp/hindsight-bridge-token-cache:/token-cache:ro",
                    "-v", str(ROOT) + ":/candidate-source:ro", "-v", str(self.output) + ":/delivery",
                    "soulter/astrbot:v" + version, "python", "/candidate-source/tests/integration/background_native_host.py",
                    stdout=log, stderr=subprocess.STDOUT, timeout=240)
            report = json.loads((self.output / ("background-native-host-report-" + version + ".json")).read_text())
            assert report["complete"] and report["hindsight"] == "1.2.0"
            self.record("real_astrbot_" + version + "_installation_upgrade_background_maintenance_forget_reload")

    async def delete_check(self, client, bank):
        async with asyncio.timeout(90):
            while any(op.status in {"pending", "processing"} for op in await client.list_operations(bank)):
                await asyncio.sleep(0.5)
        # The dedicated DB contains synthetic material only; inspect bank-scoped residue directly.
        db = self.service("memory-db")
        tables = ("llm_requests", "graph_maintenance_queue", "entity_maintenance_queue", "mental_models",
                  "mental_model_history", "documents", "async_operations")
        # Stop native queue draining so a successful cleanup cannot be attributed
        # to a background worker between the before/after observations.
        with (self.directory / ".env").open("a") as file:
            file.write("\nHINDSIGHT_API_WORKER_ENABLED='false'\n")
        run("docker", "compose", "-f", str(self.directory / "compose.yaml"), "--project-name", self.project,
            "up", "-d", "--no-deps", "memory-api", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await self.ready(client, "1.2.0")
        assert (await client.request("GET", "/v1/version"))["features"]["worker"] is False
        docker("exec", db, "psql", "-U", "hindsight", "-v", "ON_ERROR_STOP=1", "-Atc",
               f"INSERT INTO graph_maintenance_queue(bank_id,unit_id) VALUES ('{bank}','{uuid.uuid4()}');"
               f"INSERT INTO entity_maintenance_queue(bank_id,entity_id) VALUES ('{bank}','{uuid.uuid4()}');")
        before = {}
        for table in tables:
            before[table] = int(docker("exec", db, "psql", "-U", "hindsight", "-Atc",
                                      f"SELECT COUNT(*) FROM {table} WHERE bank_id='{bank}'"))
            assert before[table] > 0, table
        await client.delete_bank(bank)
        assert not await client.bank_exists(bank)
        after = {}
        for table in tables:
            after[table] = int(docker("exec", db, "psql", "-U", "hindsight", "-Atc",
                                     f"SELECT COUNT(*) FROM {table} WHERE bank_id='{bank}'"))
            assert after[table] == 0, table
        self.report["delete_counts"] = {"before": before, "after": after, "worker_disabled": True}
        self.record("bank_delete_removes_actual_model_request_history_pages_versions_documents_operations_and_maintenance_rows")

    def cleanup(self):
        if self.model:
            self.model.extraction_gate.set()
            self.model.embedding_gate.set()
            self.model.shutdown()
            self.model.server_close()
        for host in self.hosts:
            if docker("inspect", "--format", '{{index .Config.Labels "hindsight-bridge.test"}}', host) != self.state["id"]:
                raise RuntimeError("Host ownership mismatch")
            run("docker", "rm", "-f", host, stdout=subprocess.DEVNULL)
        for kind in ("memory-api", "memory-db"):
            found = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project,
                           "--filter", "label=com.docker.compose.service=" + kind)
            if found:
                with (self.output / (kind + ".log")).open("w") as file:
                    run("docker", "logs", found, stdout=file, stderr=subprocess.STDOUT)
        if self.directory.exists():
            self.command(uninstallation(self.state), "uninstall")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default="c2dec0e25ae8292e3dd01ecb6e326a87965d41ef")
    parser.add_argument("--scope", choices=("full", "deletion", "maintenance"), default="full")
    args = parser.parse_args()
    os.umask(0o077)
    acceptance = Acceptance(args.output, args.baseline, args.scope)
    succeeded = False
    try:
        await acceptance.check_api()
        succeeded = True
    except Exception as error:
        acceptance.report.update(failure_type=type(error).__name__)
        raise
    finally:
        if acceptance.model:
            acceptance.report["model_calls"] = acceptance.model.counts
        acceptance.save()
        acceptance.cleanup()
        acceptance.report.update(complete=succeeded, owned_resources_cleaned=True)
        acceptance.save()


if __name__ == "__main__":
    asyncio.run(main())
