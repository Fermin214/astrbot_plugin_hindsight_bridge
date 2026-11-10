"""Give the host a disposable upload while preserving the locked candidate."""

import hashlib
import json
import os
from pathlib import Path
import shutil


NAME = "astrbot_plugin_hindsight_bridge"
DELIVERY = Path(os.environ.get("DELIVERY_ROOT", "/delivery"))


def candidate_manifest():
    return json.loads((DELIVERY / "package-inventory.json").read_text())


async def install_candidate(manager, archive_name):
    manifest = candidate_manifest()
    source = DELIVERY / archive_name
    digest = manifest["archives"][archive_name]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    upload = Path(os.environ["ASTRBOT_ROOT"]) / archive_name
    shutil.copyfile(source, upload)
    await manager.install_plugin_from_file(str(upload))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def verify_installed_candidate(manager):
    manifest = candidate_manifest()
    for name, digest in manifest["files"].items():
        installed = Path(manager.plugin_store_path) / NAME / name
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest, name
    return manifest["archives"]["candidate-install.zip"]
