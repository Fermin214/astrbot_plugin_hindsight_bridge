"""Execute the unmodified wizard commands in an owned Docker bridge installation."""

import argparse
import base64
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location("live_harness", Path(__file__).with_name("run-live-e2e.py"))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class Managed(live.Environment):
    def __init__(self, args):
        super().__init__(args)
        self.network = self.owner + "-astrbot-network"
        self.network_mode = args.network_mode
        if self.network_mode != "custom":
            self.network = self.network_mode
        self.project = None
        self.network_created = False
        self.driver = "managed_e2e.py"
        self.install_root = self.output / "managed-installations"
        self.settings.update(listen_host="0.0.0.0", chat_tokens=8192, control_plane_port=live.free_port())
        self.settings["upgrade_from_1_1"] = args.upgrade_from_1_1
        self.settings["verify_panel"] = args.verify_panel
        self.settings.update(network_mode=self.network_mode, api_port=live.free_port(), management_port=live.free_port())
        if self.network_mode == "host":
            self.settings["listen_host"] = "127.0.0.1"

    def prepare_network(self):
        if self.network_mode == "custom":
            self.docker("network", "create", "--label", "hindsight-bridge.test=" + self.owner, self.network, stdout=subprocess.DEVNULL)
            self.network_created = True
        gateway_network = "bridge" if self.network_mode == "host" else self.network
        self.gateway_host = subprocess.check_output(["docker", "network", "inspect", "--format", "{{(index .IPAM.Config 0).Gateway}}", gateway_network], text=True).strip()
        self.gateway.server_close()
        self.gateway = live.Gateway(self)
        base = f"http://{self.gateway_host}:{self.gateway.server_port}"
        self.settings.update(gateway_url=base, chat_url=base + "/v1", memory_url=base + "/memory")
        live.private_json(self.output / "live-settings.json", self.settings)

    def start(self):
        live.run(sys.executable, str(live.ROOT / "tools/build-test-package.py"), "--output", str(self.output))
        if self.settings["upgrade_from_1_1"]:
            with (self.output / "baseline-install.zip").open("wb") as file:
                live.run("git", "archive", "--format=zip", "--prefix=astrbot_plugin_hindsight_bridge/",
                         "c2dec0e25ae8292e3dd01ecb6e326a87965d41ef", cwd=live.ROOT, stdout=file)
        environment = json.loads(self.model_environment.read_text())
        models = json.loads(environment["ASTRBOT_MEMORY_MODEL_CONFIG"])
        live.private_json(self.output / "native-models.json", self.native_models(models))
        token_cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or os.environ.get("DATA_GYM_CACHE_DIR")
                           or str(Path(tempfile.gettempdir()) / "data-gym-cache")).resolve()
        live.run(sys.executable, "-c", "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')")
        mounts = ["-v", str(live.ROOT) + ":/candidate-source:ro", "-v", str(self.output) + ":/delivery",
                  "-e", "ASTRBOT_ROOT=/delivery/verification", "-e", "ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + self.settings["initial_password"],
                  "-e", "TIKTOKEN_CACHE_DIR=/token-cache", "-v", str(token_cache) + ":/token-cache:ro"]
        self.docker("run", "--rm", "--network", "none", *mounts, self.astrbot_image, "python", "/candidate-source/tests/integration/live_astrbot_setup.py")
        name = self.owner + "-astrbot"
        self.containers.append(name)
        ports = [] if self.network_mode == "host" else [
            "-p", f'127.0.0.1:{self.settings["dashboard_port"]}:{self.settings["dashboard_port"]}',
            "-p", f'127.0.0.1:{self.settings["onebot_port"]}:{self.settings["onebot_port"]}',
        ]
        self.docker("run", "-d", "--name", name, "--hostname", name, "--label", "hindsight-bridge.test=" + self.owner,
                    "--network", self.network, "--memory", "1536m", *ports, *mounts, self.astrbot_image, stdout=subprocess.DEVNULL)
        self.astrbot_networks = subprocess.check_output([
            "docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name,
        ], text=True)
        self.wait(lambda: self.ready_http(f'http://127.0.0.1:{self.settings["dashboard_port"]}/api/v1/auth/setup-status'))
        print("Owned " + self.network_mode + " AstrBot ready", flush=True)

    def native_models(self, models):
        return [
            {"id": "delivery-embedding", "type": "dashscope_embedding", "provider_type": "embedding", "enable": True,
             "embedding_model": models["embedding"]["model"], "embedding_api_base": models["embedding"]["base_url"],
             "embedding_api_key": models["embedding"]["api_key"], "embedding_dimensions": 1024},
            {"id": "delivery-rerank", "type": "bailian_rerank", "provider_type": "rerank", "enable": True,
             "rerank_model": models["rerank"]["model"], "rerank_api_base": models["rerank"]["base_url"],
             "rerank_api_key": models["rerank"]["api_key"]},
        ]

    def managed_control(self, action):
        self.owned(self.owner + "-astrbot")
        state = json.loads(subprocess.check_output([
            "docker", "exec", self.owner + "-astrbot", "python", "-c",
            "from pathlib import Path; print(Path('/delivery/verification/data/plugin_data/astrbot_plugin_hindsight_bridge/setup.json').read_text())",
        ], text=True))
        identifier = state["id"]
        if not re.fullmatch("[0-9a-f]{12}", identifier):
            raise RuntimeError("Invalid owned installation identifier")
        project = "astrbot-memory-" + identifier
        if self.project not in {None, project}:
            raise RuntimeError("Managed installation identity changed")
        self.project = project
        if action in {"managed_pause", "managed_unpause"}:
            names = subprocess.check_output([
                "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                "--filter", "label=com.docker.compose.service=memory-api",
            ], text=True).split()
            if len(names) != 1:
                raise RuntimeError("Expected one owned managed Hindsight API")
            self.docker("pause" if action == "managed_pause" else "unpause", names[0], stdout=subprocess.DEVNULL)
            return
        path = self.output / (action + "-command.txt")
        command = path.read_text().strip()
        match = re.fullmatch(r"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d \| gzip -d \| bash", command)
        if not match:
            raise RuntimeError("Unexpected wizard command format")
        script = gzip.decompress(base64.b64decode(match[1])).decode()
        if not script.startswith("#!/bin/bash\nset -euo pipefail\n") or "project=" + project + "\n" not in script:
            raise RuntimeError("Wizard command belongs to another installation")
        if action == "managed_uninstall":
            for name in subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project], text=True).split():
                result = subprocess.run(["docker", "logs", name], capture_output=True)
                (self.output / ("managed-before-uninstall-" + name + ".log")).write_bytes(result.stdout + result.stderr)
        with (self.output / (action + ".log")).open("w") as log:
            live.run("bash", "-c", command, env={**os.environ, "HINDSIGHT_INSTALL_ROOT": str(self.install_root),
                                                "HINDSIGHT_ASTRBOT_NETWORK": "" if self.network_mode == "host" else self.network}, timeout=660,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if action != "managed_uninstall":
            name = subprocess.check_output(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project,
                                            "--filter", "label=com.docker.compose.service=memory-api"], text=True).strip()
            api_network = self.network if self.network_mode == "custom" else project + "-clients"
            address = subprocess.check_output(["docker", "inspect", "--format", '{{(index .NetworkSettings.Networks "' + api_network + '").IPAddress}}', name], text=True).strip()
            bindings = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", name], text=True))
            expected_ports = {"9999/tcp"} | ({"8888/tcp", "8766/tcp"} if self.network_mode != "custom" else set())
            published = {port for port, entries in bindings.items() if entries}
            if published != expected_ports or any(
                entry["HostIp"] != (self.gateway_host if self.network_mode == "bridge" and port != "9999/tcp" else "127.0.0.1")
                for port, entries in bindings.items() if entries for entry in entries
            ):
                raise RuntimeError("Unexpected managed host port exposure")
            self.settings["verified_host_bindings"] = bindings
            self.settings.update(hindsight_url="http://" + address + ":8888", memory_key=state["api_key"], managed_project=project)
            actual = subprocess.check_output([
                "docker", "exec", name, "python", "-c", "from hindsight_api import __version__; print(__version__)",
            ], text=True).strip()
            expected = "1.1.0" if self.settings["upgrade_from_1_1"] and action == "managed_install" else "1.2.0"
            if actual != expected:
                raise RuntimeError("Managed Hindsight version differs from the test target")
            self.settings["hindsight_version"] = actual
            self.settings["hindsight_image_id"] = subprocess.check_output([
                "docker", "inspect", "--format", "{{.Image}}", name,
            ], text=True).strip()
            live.private_json(self.output / "live-settings.json", self.settings)
        else:
            for kind, flags in (("ps", ["-aq"]), ("volume", ["ls", "-q"]), ("network", ["ls", "-q"])):
                remaining = subprocess.check_output(["docker", kind, *flags, "--filter", "label=com.docker.compose.project=" + project], text=True).strip()
                if remaining:
                    raise RuntimeError("Managed uninstall left owned resources")
            if (self.install_root / project).exists():
                raise RuntimeError("Managed uninstall left installation files")
            self.owned(self.owner + "-astrbot")
            if not subprocess.check_output(["docker", "network", "inspect", "--format", "{{.Name}}", self.network], text=True).strip() == self.network:
                raise RuntimeError("External AstrBot network changed")
        networks = subprocess.check_output([
            "docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", self.owner + "-astrbot",
        ], text=True)
        if networks != self.astrbot_networks:
            raise RuntimeError("AstrBot network configuration changed")

    def cleanup(self):
        if self.project:
            for name in subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project], text=True).split():
                service = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.service"}}', name], text=True).strip()
                if service not in {"memory-api", "memory-db", "memory-settings-init"}:
                    raise RuntimeError("Unexpected managed project resource")
                result = subprocess.run(["docker", "logs", name], capture_output=True)
                (self.output / ("managed-" + service + ".log")).write_bytes(result.stdout + result.stderr)
                self.docker("stop", "-t", "90", name, stdout=subprocess.DEVNULL)
                self.docker("rm", name, stdout=subprocess.DEVNULL)
            for kind, names in (("network", {self.project + "_memory_database", self.project + "-clients"}),
                                ("volume", {self.project + "_memory_postgres", self.project + "_memory_settings"})):
                for name in subprocess.check_output(["docker", kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project], text=True).split():
                    actual = subprocess.check_output(["docker", kind, "inspect", "--format", "{{.Name}}", name], text=True).strip()
                    if actual not in names:
                        raise RuntimeError("Unexpected managed project resource")
                    self.docker(kind, "rm", name, stdout=subprocess.DEVNULL)
        super().cleanup()
        if self.network_created:
            owner = subprocess.check_output(["docker", "network", "inspect", "--format", '{{index .Labels "hindsight-bridge.test"}}', self.network], text=True).strip()
            if owner != self.owner:
                raise RuntimeError("External test network ownership mismatch")
            self.docker("network", "rm", self.network, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-environment", type=Path, default=Path("/tmp/hindsight-bridge-qwen-acceptance/model-environment.json"))
    parser.add_argument("--magpie-url", default="http://127.0.0.1:3425/v1")
    parser.add_argument("--chat-model", default="remote-magpie/workbuddy-ai/deepseek-v4.1-flash-sg")
    parser.add_argument("--astrbot-version", choices=("4.29.1", "4.29.4"), default="4.29.1")
    parser.add_argument("--upgrade-from-1.1", dest="upgrade_from_1_1", action="store_true",
                        help="install the previous native plugin/service, then use the current browser upgrade flow")
    parser.add_argument("--verify-panel", action="store_true",
                        help="verify panel layout, user lifecycle and pause recovery in the owned browser environment")
    parser.add_argument("--network-mode", choices=("custom", "bridge", "host"), default="custom")
    parser.add_argument("--scenario", choices=("full", "connection", "mode", "network"), default="full",
                        help="check active model edits and mode switching, or mode switching alone")
    args = parser.parse_args()
    state = Managed(args)
    if args.scenario == "network":
        state.driver = "network_e2e.py"
        state.settings["connection_check_mode"] = "managed"
        live.private_json(state.output / "live-settings.json", state.settings)
    if args.scenario in {"connection", "mode"}:
        state.driver = "connection_e2e.py"
        state.settings["connection_check_mode"] = "managed"
        state.settings["connection_check_step"] = args.scenario
        live.private_json(state.output / "live-settings.json", state.settings)
    state.model_environment = args.model_environment
    try:
        state.prepare_network()
        with live.serving(state.gateway):
            state.start()
            state.verify()
    finally:
        state.gateway.extraction_gate.set()
        state.gateway.cleanup_gate.set()
        state.cleanup()
    print("Managed acceptance evidence: " + str(state.output))


if __name__ == "__main__":
    main()
