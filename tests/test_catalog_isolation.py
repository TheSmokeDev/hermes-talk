"""The catalog fixture must isolate and restore ambient import state."""

import inspect
import sys
import types

import pytest
from conftest import isolated_capability_catalog


@pytest.mark.parametrize("installed", [False, True])
def test_catalog_fixture_restores_modules_after_test_overrides(monkeypatch, installed):
    names = (
        "hermes_cli", "hermes_cli.config", "hermes_cli.tools_config",
        "tools", "tools.skills_tool", "toolsets", "model_tools",
    )
    ambient = {name: types.ModuleType(name) if installed else None for name in names}
    for name, value in ambient.items():
        monkeypatch.setitem(sys.modules, name, value)

    # Exercise the fixture generator and pytest's shared undo-stack ordering.
    patcher = pytest.MonkeyPatch()
    fixture = inspect.unwrap(isolated_capability_catalog)(patcher)
    next(fixture)
    try:
        assert all(sys.modules[name] is None for name in names)
        for name in names:
            patcher.setitem(sys.modules, name, types.ModuleType(name))
    finally:
        try:
            with pytest.raises(StopIteration):
                next(fixture)
        finally:
            patcher.undo()

    assert all(sys.modules[name] is value for name, value in ambient.items())
