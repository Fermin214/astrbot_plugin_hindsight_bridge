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
    tests = root / "tests"
    if sys.platform == "win32":
        linux_tests = {"test_installer", "test_managed_service"}
        print("Linux deployment checks run in the Linux job: " + ", ".join(sorted(linux_tests)), flush=True)
        sys.path.insert(0, str(tests))
        names = [path.stem for path in sorted(tests.glob("test_*.py")) if path.stem not in linux_tests]
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    else:
        suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    frontend = subprocess.run([node, str(root / "tests" / "test_memory_view.mjs")], check=False)
    return 0 if result.wasSuccessful() and frontend.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
