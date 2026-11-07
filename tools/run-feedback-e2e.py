"""Owned real AstrBot/Hindsight feedback and control migration with synthetic peers."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/integration"))
from backend_model import Model

SPEC = importlib.util.spec_from_file_location("live_harness", ROOT / "tools/run-live-e2e.py")
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--astrbot-version", choices=("4.29.1", "4.29.4"), default="4.29.4")
    parser.add_argument("--hindsight-version", choices=("1.1.0", "1.2.0"), default="1.2.0")
    parser.add_argument("--scope", choices=("full", "feedback", "view", "recovery", "single-page", "multi-page",
                                          "multi-page-recovery", "read-tools", "parameters"), default="full")
    args = parser.parse_args()
    args.magpie_url = "http://127.0.0.1:3425/v1"
    args.chat_model = "compatibility-fixture"
    with tempfile.TemporaryDirectory(prefix="hb-feedback-models-") as directory:
        args.model_environment = Path(directory) / "environment.json"
        args.model_environment.write_text(json.dumps({"ASTRBOT_MEMORY_MODEL_CONFIG": "{}"}))
        state = live.Environment(args)
        state.driver = "feedback_e2e.py"
        state.settings["stop_timeout"] = 10
        state.settings["feedback_only"] = args.scope != "full"
        state.settings["view_only"] = args.scope == "view"
        state.settings["recovery_only"] = args.scope == "recovery"
        state.settings["single_page"] = args.scope == "single-page"
        state.settings["multi_page"] = args.scope in {"multi-page", "multi-page-recovery"}
        state.settings["multi_page_recovery_only"] = args.scope == "multi-page-recovery"
        state.settings["parameters_only"] = args.scope == "parameters"
        state.settings["read_tools"] = args.scope == "read-tools"
        live.private_json(state.output / "live-settings.json", state.settings)
        control_volume = state.output / "control-volume"
        control_volume.mkdir()
        state.astrbot_mounts = ["-v", str(control_volume) + ":/delivery/verification/data/plugin_data/astrbot_plugin_hindsight_bridge_control"]
        model = Model(("127.0.0.1", 0), state.settings["chat_token"])
        model.local_chat = True
        state.magpie = f"http://127.0.0.1:{model.server_port}/v1"
        state.models.update(
            HINDSIGHT_API_EMBEDDINGS_PROVIDER="openai",
            HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=state.magpie,
            HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=model.token,
            HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=args.chat_model,
            HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS="64",
            HINDSIGHT_API_RERANKER_PROVIDER="rrf",
        )
        try:
            with live.serving(model), live.serving(state.gateway), live.serving(state.transport):
                state.start()
                live.run(sys.executable, str(ROOT / "tools/build-test-package.py"), "--distribution",
                         "--output", str(state.output))
                with (state.output / "baseline-install.zip").open("wb") as archive:
                    live.run("git", "archive", "--format=zip", "--prefix=astrbot_plugin_hindsight_bridge/",
                             ("7513bf7385eb692bc621568d45697bc9e3ff9a79" if state.settings["read_tools"] else
                              "v0.2.8" if state.settings["multi_page"] else
                              "66c0e1118a61a721a23b4bed220c6922f6a5b887" if args.scope == "single-page" else "v0.2.6"),
                             cwd=ROOT, stdout=archive)
                state.verify()
        finally:
            state.cleanup()
    print("Acceptance evidence: " + str(state.output))


if __name__ == "__main__":
    main()
