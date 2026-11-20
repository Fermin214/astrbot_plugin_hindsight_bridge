from pathlib import Path
import subprocess


def working_tree_files(root, *, distribution=False):
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    files = sorted({name for name in names if name and (root / name).is_file()})
    if not distribution:
        return files
    paths = set(files)
    parents = {}
    for name in files:
        parents[name] = [parent.as_posix() + "/" for parent in Path(name).parents if parent != Path(".")]
        paths.update(parents[name])
    attributes = subprocess.check_output(
        ["git", "check-attr", "-z", "--stdin", "export-ignore"], cwd=root,
        input=("\0".join(sorted(paths)) + "\0").encode(),
    ).decode().split("\0")
    ignored = {
        attributes[index] for index in range(0, len(attributes) - 1, 3)
        if attributes[index + 2] == "set"
    }
    return [name for name in files if not ignored.intersection([name, *parents[name]])]
