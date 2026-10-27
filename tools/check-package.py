"""Check the public plugin's metadata, syntax, and distribution boundaries."""

import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata

import yaml


class ViewAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value:
                if not value.startswith(("#", "/", "//")) and ":" not in value:
                    self.paths.append(value.split("#", 1)[0].split("?", 1)[0])


def heading_anchors(path):
    anchors, counts = set(), {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or not re.match(r"^#{1,6} ", line):
            continue
        title = re.sub(r"^#{1,6} +", "", line).strip().lower()
        slug = "".join(
            char
            for char in title
            if not unicodedata.category(char).startswith(("P", "S"))
            or char in "-_"
        ).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


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
    assert metadata["astrbot_version"] == ">=4.29.1"
    for name in (
        "LICENSE", "NOTICE.md", "main.py", "requirements.txt",
        "core/__init__.py", "scripts/backup.py",
        "core/panel.py", "core/providers.py", "core/installer.py",
        "views/memory/index.html", "views/memory/app.js", "views/memory/style.css",
        ".astrbot-plugin/i18n/zh-CN.json", ".astrbot-plugin/i18n/en-US.json",
        "deploy/compose.yaml", "deploy/.env.example", "deploy/init-env.py",
        "deploy/postgres/Dockerfile", "deploy/postgres/extensions.sql",
        "deploy/HINDSIGHT-LICENSE",
        "deploy/model_transport.py", "tools/check-model-protocols.py",
        "deploy/managed_service.py", "deploy/model_probe.py",
        "docs/INSTALL.md", "docs/USAGE.md", "docs/SUPPORT.md",
        "docs/DATA-MAINTENANCE.md", "docs/ARCHITECTURE.md", "docs/SETUP-DESIGN.md",
    ):
        assert (root / name).is_file(), name
    for language in ("zh-CN", "en-US"):
        translation = json.loads(
            (root / ".astrbot-plugin/i18n" / f"{language}.json").read_text(
                encoding="utf-8"
            )
        )
        assert translation["views"]["memory"]["title"].strip(), language
    for view in (root / "views").rglob("*.html"):
        assets = ViewAssets()
        assets.feed(view.read_text(encoding="utf-8"))
        for name in assets.paths:
            destination = (view.parent / name).resolve()
            assert destination.is_relative_to(root / "views"), (str(view), name)
            assert destination.is_file(), (str(view), name)
    assert {path.name for path in root.glob("*.py")} == {"main.py"}
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    example = json.loads(
        (root / "examples/config.example.json").read_text(encoding="utf-8")
    )
    for obsolete in ("enabled", "permission_config_version", "allowed_user_ids", "allowed_platform_ids"):
        assert obsolete not in schema and obsolete not in example, obsolete
    assert not schema["api_key"]["default"] and not example["api_key"]
    for path in root.rglob("*.py"):
        if ".git" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in root.rglob("*.md"):
        if ".git" in path.parts:
            continue
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in link:
                continue
            target, _, fragment = link.partition("#")
            destination = path.parent / target if target else path
            assert destination.exists(), (str(path.relative_to(root)), link)
            if fragment and destination.suffix == ".md":
                assert fragment in heading_anchors(destination), (
                    str(path.relative_to(root)), link
                )
    for line in (root / "deploy/.env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.endswith(("_PASSWORD", "_API_KEY", "_ACCESS_KEY")):
            assert not value.strip(), key
    print("Plugin metadata, syntax, configuration, view assets, and document links passed.")


if __name__ == "__main__":
    main()
