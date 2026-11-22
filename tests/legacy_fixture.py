from contextlib import contextmanager
import importlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types


BASELINE = "d6a4ff56617fa2a98cc79df16905da2a41265ac3"


@contextmanager
def legacy_code():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        archive = subprocess.check_output(["git", "archive", BASELINE, "core"], cwd=root)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(temporary, filter="data")
        name = "hindsight_v050_fixture"
        package = types.ModuleType(name)
        package.__path__ = [temporary]
        sys.modules[name] = package
        try:
            yield types.SimpleNamespace(
                Outbox=importlib.import_module(name + ".core.outbox").Outbox,
                Incoming=importlib.import_module(name + ".core.identity").Incoming,
            )
        finally:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]
