from importlib.metadata import EntryPoint

import pytest

from sprint.cli.plugin_registry import (
    list_plugin_entry_points,
    resolve_plugin_import_path,
)


def test_resolve_plugin_import_path_passthrough_module_class():
    assert resolve_plugin_import_path("my_plugin:Plugin") == "my_plugin:Plugin"


def test_resolve_plugin_import_path_uses_entry_point(monkeypatch):
    entry_point = EntryPoint(
        name="langsmith",
        value="harbor_langsmith:LangSmithPlugin",
        group="sprint.plugins",
    )
    monkeypatch.setattr(
        "sprint.cli.plugin_registry.entry_points",
        lambda *, group: [entry_point] if group == "sprint.plugins" else [],
    )

    assert resolve_plugin_import_path("langsmith") == "harbor_langsmith:LangSmithPlugin"


def test_resolve_plugin_import_path_unknown_plugin(monkeypatch):
    monkeypatch.setattr(
        "sprint.cli.plugin_registry.entry_points",
        lambda *, group: [],
    )

    with pytest.raises(ValueError, match="Unknown plugin 'missing'"):
        resolve_plugin_import_path("missing")


def test_list_plugin_entry_points(monkeypatch):
    entry_points = [
        EntryPoint(
            name="b",
            value="pkg_b:PluginB",
            group="sprint.plugins",
        ),
        EntryPoint(
            name="a",
            value="pkg_a:PluginA",
            group="sprint.plugins",
        ),
    ]
    monkeypatch.setattr(
        "sprint.cli.plugin_registry.entry_points",
        lambda *, group: entry_points if group == "sprint.plugins" else [],
    )

    assert list_plugin_entry_points() == {
        "b": "pkg_b:PluginB",
        "a": "pkg_a:PluginA",
    }
