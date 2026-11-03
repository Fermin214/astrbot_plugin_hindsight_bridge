"""Build candidate archives and a content inventory outside the repository."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    destination = args.output.expanduser().resolve()
    if destination.is_relative_to(root):
        parser.error("test artifacts must be outside the repository")
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    files = sorted({name for name in names if name and (root / name).is_file()})
    for name in files:
        if (root / name).is_symlink():
            parser.error("candidate packages must not contain symlinks: " + name)
    destination.mkdir(parents=True, exist_ok=True)
    contents = {name: (root / name).read_bytes() for name in files}
    inventory = {name: hashlib.sha256(contents[name]).hexdigest() for name in files}
    for archive_name in ("candidate-install.zip", "candidate-upgrade.zip"):
        with zipfile.ZipFile(destination / archive_name, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.writestr("astrbot_plugin_hindsight_bridge/" + name, contents[name])
    (destination / "package-inventory.json").write_text(
        json.dumps({
            "tree_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            "files": inventory,
        }, indent=2), encoding="utf-8",
    )
    print(f"Built {len(files)}-file candidate archives in {destination}")


if __name__ == "__main__":
    main()
