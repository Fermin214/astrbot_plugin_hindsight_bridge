"""Build unreleased test or distribution candidates outside the repository."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from distribution import working_tree_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--distribution", action="store_true", help="apply the repository's export-ignore rules")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--revision", help="archive a specific commit for distribution verification")
    source.add_argument("--working-tree", action="store_true", help="include local changes for development verification")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    destination = args.output.expanduser().resolve()
    if destination.is_relative_to(root):
        parser.error("test artifacts must be outside the repository")
    if args.revision and not args.distribution:
        parser.error("--revision requires --distribution")
    destination.mkdir(parents=True, exist_ok=True)
    candidate = destination / "candidate-install.zip"
    prefix = "astrbot_plugin_hindsight_bridge/"
    commit = None
    if args.distribution and not args.working_tree:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", (args.revision or "HEAD") + "^{commit}"],
            cwd=root, text=True,
        ).strip()
        subprocess.run(
            ["git", "archive", "--format=zip", "--prefix=" + prefix, "--output=" + str(candidate), commit],
            cwd=root, check=True,
        )
    else:
        files = working_tree_files(root, distribution=args.distribution)
        for name in files:
            if (root / name).is_symlink():
                parser.error("candidate packages must not contain symlinks: " + name)
        with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                entry = zipfile.ZipInfo(prefix + name)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = (0o100755 if (root / name).stat().st_mode & 0o111 else 0o100644) << 16
                archive.writestr(entry, (root / name).read_bytes())
    with zipfile.ZipFile(candidate) as archive:
        inventory = {}
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                parser.error("candidate packages must not contain symlinks: " + entry.filename)
            inventory[entry.filename.removeprefix(prefix)] = hashlib.sha256(archive.read(entry)).hexdigest()
    shutil.copyfile(candidate, destination / "candidate-upgrade.zip")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    (destination / "package-inventory.json").write_text(
        json.dumps({
            "release_status": "unreleased_candidate",
            "package_kind": "distribution" if args.distribution else "test",
            "commit": commit,
            "archives": {name: digest for name in ("candidate-install.zip", "candidate-upgrade.zip")},
            "tree_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            "files": inventory,
        }, indent=2), encoding="utf-8",
    )
    print(f"Built {len(inventory)}-file candidate archives in {destination}")


if __name__ == "__main__":
    main()
