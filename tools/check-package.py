"""Check the public plugin's metadata, syntax, and distribution boundaries."""

import ast
import json
from pathlib import Path
import re

import yaml


def main():
    root = Path(__file__).resolve().parent.parent
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    for name in ("name", "display_name", "desc", "version", "author", "repo"):
        assert isinstance(metadata.get(name), str) and metadata[name].strip(), name
    assert metadata["name"] == "astrbot_plugin_hindsight_bridge"
    assert metadata["author"] == "Fermin214"
    assert metadata["repo"] == (
        "https://github.com/Fermin214/astrbot_plugin_hindsight_bridge"
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", metadata["version"])
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4.29.1,<4.30"
    for name in ("LICENSE", "NOTICE.md", "main.py", "requirements.txt"):
        assert (root / name).is_file(), name
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert schema["enabled"]["default"] is False and example["enabled"] is False
    assert not schema["api_key"]["default"] and not example["api_key"]
    for path in root.rglob("*.py"):
        if ".git" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in root.rglob("*.md"):
        if ".git" in path.parts:
            continue
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("#"):
                continue
            target = link.split("#", 1)[0]
            assert (path.parent / target).exists(), (str(path.relative_to(root)), link)
    for line in (root / "deploy/.env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.endswith(("_PASSWORD", "_API_KEY", "_ACCESS_KEY")):
            assert not value.strip(), key
    print("Plugin metadata, syntax, configuration, and document links passed.")


if __name__ == "__main__":
    main()
