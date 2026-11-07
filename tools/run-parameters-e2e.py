"""Real native-panel settings acceptance using owned services and synthetic models."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/integration"))
from backend_model import Model

spec = importlib.util.spec_from_file_location("managed_harness", ROOT / "tools/run-managed-e2e.py")
managed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(managed)
live = managed.live


class FixtureManaged(managed.Managed):
    def native_models(self, models):
        return [{
            "id": "delivery-embedding", "type": "openai_embedding", "provider_type": "embedding", "enable": True,
            "embedding_model": "compatibility-fixture", "embedding_api_base": self.magpie,
            "embedding_api_key": self.settings["chat_token"], "embedding_dimensions": 64,
        }]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("existing", "managed"), required=True)
    parser.add_argument("--astrbot-version", choices=("4.29.1", "4.29.4"), default="4.29.4")
    parser.add_argument("--hindsight-version", choices=("1.1.0", "1.2.0"), default="1.2.0")
    args = parser.parse_args()
    if args.mode == "existing":
        subprocess.run([sys.executable, str(ROOT / "tools/run-feedback-e2e.py"), "--output", str(args.output),
                        "--astrbot-version", args.astrbot_version, "--hindsight-version", args.hindsight_version,
                        "--scope", "single-page"], check=True)
        return
    if args.hindsight_version != "1.2.0":
        parser.error("new managed installations use Hindsight 1.2.0")
    args.magpie_url, args.chat_model = "http://127.0.0.1:3425/v1", "compatibility-fixture"
    args.upgrade_from_1_1 = args.verify_panel = False
    with tempfile.TemporaryDirectory(prefix="hb-parameter-models-") as directory:
        args.model_environment = Path(directory) / "environment.json"
        args.model_environment.write_text(json.dumps({"ASTRBOT_MEMORY_MODEL_CONFIG": "{}"}))
        state = FixtureManaged(args)
        state.model_environment = args.model_environment
        state.driver = "parameters_e2e.py"
        state.settings.update(parameter_managed=True, stop_timeout=10, chat_tokens=512)
        try:
            state.prepare_network()
            model = Model((state.gateway_host, 0), state.settings["chat_token"])
            model.local_chat = True
            state.magpie = f"http://{state.gateway_host}:{model.server_port}/v1"
            live.private_json(state.output / "live-settings.json", state.settings)
            with live.serving(model), live.serving(state.gateway):
                state.start()
                live.run(sys.executable, str(ROOT / "tools/build-test-package.py"), "--distribution", "--output", str(state.output))
                state.verify()
        finally:
            state.transport.server_close()
            state.cleanup()


if __name__ == "__main__":
    main()
