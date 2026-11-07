import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own HERMES_HOME and no GROUNDED_RECALL_* env leakage."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import os
    for k in list(os.environ):
        if k.startswith("GROUNDED_RECALL_"):
            monkeypatch.delenv(k, raising=False)
    return home


@pytest.fixture
def plugin_module():
    """Load the repo root as the plugin package, the way Hermes loads a directory plugin."""
    name = "hermes_plugins_test.grounded_recall_plugin"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    yield mod
    for k in [k for k in sys.modules if k.startswith(name)]:
        sys.modules.pop(k, None)


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.hooks = {}
        self.sections = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb, **_):
        self.hooks.setdefault(name, []).append(cb)

    def register_system_prompt_section(self, id, content, *, position="after_memory", max_chars=4000):
        assert max_chars <= 4000 and position == "after_memory"
        self.sections[id] = content


@pytest.fixture
def fake_ctx():
    return FakeCtx
