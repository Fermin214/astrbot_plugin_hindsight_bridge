"""Run the core regressions from a clone with any directory name."""

from pathlib import Path
import shutil
import subprocess
import sys
import types
import unittest


def main():
    root = Path(__file__).resolve().parent.parent
    node = shutil.which("node")
    if not node:
        print("Node.js is required for the memory-view regressions; checks did not pass.", file=sys.stderr)
        return 1
    for name, path in (
        ("plugins", root.parent),
        ("plugins.astrbot_plugin_hindsight_bridge", root),
    ):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    suite = unittest.defaultTestLoader.discover(
        str(root / "tests"), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    frontend = subprocess.run([node, str(root / "tests" / "test_memory_view.js")], check=False)
    return 0 if result.wasSuccessful() and frontend.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
