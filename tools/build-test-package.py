"""Build unreleased test or distribution candidates outside the repository."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def distribution_files(root, files):
    paths = set(files)
    parents = {}
    for name in files:
        parents[name] = [parent.as_posix() + "/" for parent in Path(name).parents if parent != Path(".")]
        paths.update(parents[name])
    attributes = subprocess.check_output(
        ["git", "check-attr", "-z", "--stdin", "export-ignore"], cwd=root,
        input=("\0".join(sorted(paths)) + "\0").encode(),
    ).decode().split("\0")
    ignored = {attributes[i] for i in range(0, len(attributes) - 1, 3) if attributes[i + 2] == "set"}
    return [name for name in files if not ignored.intersection([name, *parents[name]])]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--distribution", action="store_true", help="apply the repository's export-ignore rules")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    destination = args.output.expanduser().resolve()
    if destination.is_relative_to(root):
        parser.error("test artifacts must be outside the repository")
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    files = sorted({name for name in names if name and (root / name).is_file()})
    if args.distribution:
        files = distribution_files(root, files)
    for name in files:
        if (root / name).is_symlink():
            parser.error("candidate packages must not contain symlinks: " + name)
    destination.mkdir(parents=True, exist_ok=True)
    contents = {name: (root / name).read_bytes() for name in files}
    inventory = {name: hashlib.sha256(contents[name]).hexdigest() for name in files}
    archives = {}
    for archive_name in ("candidate-install.zip", "candidate-upgrade.zip"):
        with zipfile.ZipFile(destination / archive_name, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                entry = zipfile.ZipInfo("astrbot_plugin_hindsight_bridge/" + name)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = (0o100755 if (root / name).stat().st_mode & 0o111 else 0o100644) << 16
                archive.writestr(entry, contents[name])
        archives[archive_name] = hashlib.sha256((destination / archive_name).read_bytes()).hexdigest()
    (destination / "package-inventory.json").write_text(
        json.dumps({
            "release_status": "unreleased_candidate",
            "package_kind": "distribution" if args.distribution else "test",
            "archives": archives,
            "tree_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            "files": inventory,
        }, indent=2), encoding="utf-8",
    )
    print(f"Built {len(files)}-file candidate archives in {destination}")


if __name__ == "__main__":
    main()
