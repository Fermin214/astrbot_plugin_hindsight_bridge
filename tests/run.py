"""Run the core regressions from a clone with any directory name."""

from pathlib import Path
import sys
import types
import unittest


def main():
    root = Path(__file__).resolve().parent.parent
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
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
