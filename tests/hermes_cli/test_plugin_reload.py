"""Regression for HermesMind #707(c): named hook health and targeted live recovery."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


def _fixture_plugin(home: Path, name: str, marker: str) -> Path:
    folder = home / "plugins" / name
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(f"name: {name}\nversion: '1.0'\n")
    (folder / "version.py").write_text(f"MARKER = {marker!r}\n")
    (folder / "__init__.py").write_text(
        "from threading import Event\n"
        "from .version import MARKER\n"
        "STARTED, RELEASE, FINISHED = Event(), Event(), Event()\n"
        "BLOCK = False\n"
        "BLOCK_UNLOAD = False\n"
        "UNLOAD_ENTERED, UNLOAD_RELEASE = Event(), Event()\n"
        "UNLOADED = []\n"
        "def unload():\n"
        "    UNLOAD_ENTERED.set()\n"
        "    if BLOCK_UNLOAD:\n"
        "        UNLOAD_RELEASE.wait()\n"
        "    UNLOADED.append(MARKER)\n"
        "def hook(**kwargs):\n"
        "    STARTED.set()\n"
        "    if BLOCK:\n"
        "        RELEASE.wait()\n"
        "    FINISHED.set()\n"
        "    return MARKER\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_llm_call', hook)\n"
        f"    ctx.register_tool(name='{name}_tool', toolset='{name}',\n"
        f"        schema={{'name': '{name}_tool', 'description': MARKER,\n"
        "        'parameters': {'type': 'object', 'properties': {}}},\n"
        "        handler=lambda args, **kwargs: MARKER)\n"
        f"    ctx.register_system_prompt_section('{name}', MARKER)\n"
        "    ctx.on_unload(unload)\n"
    )
    return folder


def _load(home, monkeypatch):
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: home / "no-bundled")
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home, hydrate_secrets=False):
        manager = plugins.get_plugin_manager()
        manager.discover_and_load()
    return manager


def _home(path):
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(
        "plugins:\n  enabled: [fixture, sibling]\n  hook_callback_timeout: 0.02\n")
    _fixture_plugin(path, "fixture", "first")
    _fixture_plugin(path, "sibling", "sibling")
    return path


def test_timeout_health_names_plugin_and_retains_abandoned_generation(tmp_path, monkeypatch, caplog):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.plugins_control import plugin_health_status
    from tools.registry import registry

    home = _home(tmp_path / "a")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = _load(home, monkeypatch)
    old = manager._plugins["fixture"].module
    sibling = manager._plugins["sibling"]
    old.BLOCK = True
    try:
        with _profile_runtime_scope(home, hydrate_secrets=False), caplog.at_level(logging.WARNING):
            assert manager.invoke_hook("pre_llm_call") == ["sibling"]
            assert old.STARTED.is_set()
            row, = plugin_health_status()[manager.scope_key]["stuck_hooks"]
            assert (row["plugin"], row["hook"]) == ("fixture", "pre_llm_call")
            assert row["callback"].endswith(".hook") and row["registered"]
            record, = [r for r in caplog.records if getattr(r, "event", None) == "plugin_hook_timeout"]
            assert record.plugin_hook["plugin"] == row["plugin"]
            assert json.loads(record.getMessage().split(" ", 1)[1])["hook"] == row["hook"]

            # Cached conversation inputs remain the same objects/bytes across registration replacement.
            cached_schema = registry.get_entry("fixture_tool").schema
            cached_prompt = manager.render_system_prompt_sections({})
            schema_bytes = json.dumps(cached_schema, sort_keys=True)
            prompt_bytes = repr(cached_prompt)
            (home / "plugins/fixture/version.py").write_text("MARKER = 'recovered-generation'\n")
            old.BLOCK_UNLOAD = True
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(contextvars.copy_context().run, manager.reload_plugin, "fixture")
                try:
                    assert old.UNLOAD_ENTERED.wait(5)
                    assert old.hook in manager.iter_hook_callbacks("pre_llm_call")
                    assert manager.invoke_hook("pre_llm_call") == ["sibling"]
                finally:
                    old.UNLOAD_RELEASE.set()
                result = pending.result(5)
            assert result["reloaded"], result
            assert old.UNLOADED == ["first"]
            assert manager._plugins["sibling"] is sibling
            assert sorted(manager.invoke_hook("pre_llm_call")) == ["recovered-generation", "sibling"]
            assert registry.get_entry("fixture_tool").handler({}) == "recovered-generation"
            assert result["activation"]["deferred"]["tools"] == ["fixture_tool"]
            assert json.dumps(cached_schema, sort_keys=True) == schema_bytes
            assert repr(cached_prompt) == prompt_bytes
            assert manager.render_system_prompt_sections({}) != cached_prompt
            retired, = manager.hook_health.snapshot()["stuck_hooks"]
            assert retired["plugin"] == "fixture" and not retired["registered"]
    finally:
        old.RELEASE.set()
        assert old.FINISHED.wait(5)
        # The worker sets FINISHED before its dispatch finally; use its actual thread completion.
        for thread in threading.enumerate():
            if thread.name == "hermes-hook-hook":
                thread.join(5)
        manager.unload()
    assert manager.hook_health.snapshot()["stuck_hooks"] == []


@pytest.mark.platforms("posix")
def test_cli_reload_over_control_socket_is_single_plugin_and_profile_scoped(tmp_path, monkeypatch, capsys):
    from agent.secret_scope import set_multiplex_active
    from gateway.control_socket import GatewayControlServer, query_gateway_control
    from gateway.run import _profile_runtime_scope
    from hermes_cli.plugins_cmd import plugins_command
    from hermes_cli.plugins_control import plugin_control_handlers
    from hermes_cli.subcommands.plugins import build_plugins_parser
    from tools.registry import registry

    homes = [_home(tmp_path / name) for name in ("a", "b")]
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    managers = [_load(home, monkeypatch) for home in homes]
    runner = SimpleNamespace(_served_profile_homes={"b": homes[1]})
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = GatewayControlServer(homes[0], verb_handlers=plugin_control_handlers(runner, loop, homes[0]))
    # Keep Unix paths short even when a CI checkout or pytest basetemp has a long name.
    monkeypatch.setattr("gateway.control_socket._fallback_socket_path",
                        lambda home: tmp_path.parent / "reload.sock")
    parser = argparse.ArgumentParser(prog="hermes")
    build_plugins_parser(parser.add_subparsers(), cmd_plugins=plugins_command)
    rewired = []
    unsubscribers = [m.on_plugin_loaded(lambda rows: rewired.extend(r["key"] for r in rows)) for m in managers]
    try:
        assert asyncio.run_coroutine_threadsafe(server.start(), loop).result(5)
        set_multiplex_active(True)
        siblings = [m._plugins["sibling"] for m in managers]
        for index in (0, 1, 0):
            home, manager = homes[index], managers[index]
            other = managers[1 - index]._plugins["fixture"]
            if index:
                # Exercise the same request shape the CLI sends after finding a multiplexer.
                answer = query_gateway_control(homes[0], "reload-plugin", params={"home": str(home), "name": "fixture"}, timeout=10)
            else:
                with _profile_runtime_scope(home, hydrate_secrets=False):
                    args = parser.parse_args(["plugins", "reload", "fixture", "--json"])
                    args.func(args)
                answer = json.loads(capsys.readouterr().out)
            assert answer["reloaded"] and answer["home"] == manager.scope_key
            assert answer["plugin"] == "fixture"
            assert managers[1 - index]._plugins["fixture"] is other
            assert all(m._plugins["sibling"] is sibling for m, sibling in zip(managers, siblings))
            with _profile_runtime_scope(home, hydrate_secrets=False):
                assert registry.get_entry("fixture_tool").handler({}) == "first"
        assert rewired == ["fixture"] * 3
        status = query_gateway_control(homes[0], "status")
        assert set(status["plugin_health"]) >= {m.scope_key for m in managers}
        before = managers[0]._plugins["fixture"]
        with _profile_runtime_scope(homes[0], hydrate_secrets=False), pytest.raises(SystemExit) as exc:
            args = parser.parse_args(["plugins", "reload", "missing", "--json"])
            args.func(args)
        assert exc.value.code == 1
        assert "Unknown plugin" in json.loads(capsys.readouterr().out)["error"]
        assert managers[0]._plugins["fixture"] is before
        denied = query_gateway_control(homes[0], "reload-plugin", params={"home": str(tmp_path / "unserved"), "name": "fixture"})
        assert not denied["reloaded"] and "not served" in denied["error"]
        (homes[0] / "plugins/fixture/__init__.py").write_text(
            "def register(ctx):\n"
            "    ctx.register_hook('pre_llm_call', lambda **kwargs: 'partial')\n"
            "    raise RuntimeError('fixture registration failed')\n")
        failed = query_gateway_control(homes[0], "reload-plugin", params={"name": "fixture"}, timeout=10)
        assert not failed["reloaded"] and failed["unloaded"]
        assert "fixture registration failed" in failed["error"]
        with _profile_runtime_scope(homes[0], hydrate_secrets=False):
            assert managers[0].invoke_hook("pre_llm_call") == ["sibling"]
            assert registry.get_entry("fixture_tool") is None
        assert managers[0]._plugins["sibling"] is siblings[0]
    finally:
        set_multiplex_active(False)
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
        for unsubscribe, manager in zip(unsubscribers, managers):
            unsubscribe()
            manager.unload()
