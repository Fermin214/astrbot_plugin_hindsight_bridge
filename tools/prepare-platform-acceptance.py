"""Create a fresh, private AstrBot test root and opt-in platform field sampler."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROBE = "astrbot_plugin_bridge_platform_probe"


def private_file(path, text):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(text)


def prepare(destination, platform, version, port):
    if destination.exists() or destination.is_symlink():
        raise ValueError("output must be a new directory")
    if platform not in {"weixin_oc", "telegram"} or version not in {"4.29.1", "4.29.4"}:
        raise ValueError("unsupported platform or host version")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("dashboard port must be between 1024 and 65535")
    destination.mkdir(parents=True, mode=0o700)
    data = destination / "data"
    (data / "config").mkdir(parents=True, mode=0o700)
    (data / "plugins").mkdir(mode=0o700)
    shutil.copytree(ROOT / "tests/integration/platform_probe", data / "plugins" / PROBE,
                    ignore=shutil.ignore_patterns("__pycache__"))
    private_file(data / "cmd_config.json", json.dumps({
        "dashboard": {"enable": True, "host": "0.0.0.0", "port": 6185},
        "platform": [], "provider": [], "admins_id": [],
        "disable_metrics": True,
    }, indent=2) + "\n")
    password = "Bridge-" + secrets.token_hex(16) + "!"
    private_file(destination / ".env", "ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + password + "\n")
    compose = {
        "name": "bridge-" + platform.replace("_", "-") + "-" + secrets.token_hex(4),
        "services": {
            "astrbot": {
                "image": "soulter/astrbot:v" + version,
                "ports": ["127.0.0.1:" + str(port) + ":6185"],
                "environment": {
                    "ASTRBOT_DASHBOARD_INITIAL_PASSWORD": "${ASTRBOT_DASHBOARD_INITIAL_PASSWORD:?}",
                    "ASTRBOT_DISABLE_METRICS": "1",
                },
                "volumes": ["./data:/AstrBot/data"],
                "restart": "unless-stopped",
            },
        },
    }
    private_file(destination / "compose.yaml", json.dumps(compose, indent=2) + "\n")
    manifest = {
        "format": 1, "platform": platform, "astrbot": version,
        "dashboard_port": port, "probe_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (data / "plugins" / PROBE).iterdir() if path.is_file()
        },
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip(),
    }
    private_file(destination / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    private_file(destination / "start-native.sh", (
        "#!/bin/sh\nset -eu\n"
        'cd -- "$(dirname -- "$0")"\n'
        "set -a\n. ./.env\nset +a\n"
        'export ASTRBOT_ROOT="$PWD"\n'
        "export ASTRBOT_DISABLE_METRICS=1\n"
        "export DASHBOARD_HOST=127.0.0.1\n"
        "export DASHBOARD_PORT=" + str(port) + "\n"
        'exec "$@"\n'
    ))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=("weixin_oc", "telegram"), required=True)
    parser.add_argument("--astrbot-version", choices=("4.29.1", "4.29.4"), default="4.29.4")
    parser.add_argument("--dashboard-port", type=int, default=6188)
    args = parser.parse_args()
    try:
        prepare(args.output.expanduser().absolute(), args.platform, args.astrbot_version, args.dashboard_port)
    except ValueError as error:
        parser.error(str(error))
    print("Created private " + args.platform + " test root: " + str(args.output))
    print("Docker: docker compose -f " + str(args.output / "compose.yaml") + " up -d")
    print("Native: sh " + str(args.output / "start-native.sh") + " /path/to/venv/bin/python /path/to/AstrBot/main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
