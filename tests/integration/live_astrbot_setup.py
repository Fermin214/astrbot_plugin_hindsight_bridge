"""Seed a fresh, dedicated AstrBot root, never an existing installation."""

import copy
import json
import os
from pathlib import Path

root = Path(os.environ["ASTRBOT_ROOT"])
if not root.is_absolute() or (root.exists() and any(root.iterdir())):
    raise RuntimeError("ASTRBOT_ROOT must be a dedicated empty test directory")
(root / "data/config").mkdir(parents=True)
(root / "data/plugins").mkdir()
settings = json.loads(Path("/delivery/live-settings.json").read_text())

from astrbot.core.config.default import DEFAULT_CONFIG, VERSION

assert VERSION == "4.29.1", VERSION
config = copy.deepcopy(DEFAULT_CONFIG)
config["dashboard"].update(host=settings.get("listen_host", "127.0.0.1"), port=settings["dashboard_port"],
                           auth_rate_limit={"enable": False})
config["platform"] = [{
    "id": "delivery-onebot", "type": "aiocqhttp", "enable": True,
    "ws_reverse_host": settings.get("listen_host", "127.0.0.1"), "ws_reverse_port": settings["onebot_port"],
    "ws_reverse_token": settings["onebot_token"], "name": "Delivery OneBot simulator",
}]
config["platform_settings"]["enable_id_white_list"] = False
config["admins_id"] = ["123001", "123002"]
config["provider"] = [{
    "id": "delivery-chat", "type": "openai_chat_completion", "provider_type": "chat_completion",
    "enable": True, "key": [settings["chat_token"]], "model": settings["chat_model"],
    "api_base": settings["chat_url"], "timeout": 90,
    "custom_extra_body": {"max_tokens": settings.get("chat_tokens", 512)},
}]
native_models = Path("/delivery/native-models.json")
if native_models.exists():
    config["provider"].extend(json.loads(native_models.read_text()))
config["agent_runner"]["runner_type"] = "local"
config["agent_runner"]["config"]["model"]["provider_id"] = "delivery-chat"
config["provider_settings"]["streaming_response"] = False
config["provider_settings"]["proactive_capability"]["add_cron_tools"] = False
(root / "data/cmd_config.json").write_text(json.dumps(config, indent=2))
print("Fresh AstrBot test root configured")
