"""HermesMind #707: normal gateway startup composes quarantine health and live recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import textwrap
from types import SimpleNamespace

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["sync", "async"])
async def test_gateway_startup_serves_quarantine_health_and_targeted_reload(
    tmp_path, monkeypatch, caplog, callback_kind,
):
    import hermes_cli.plugins as plugins
    from gateway.control_socket import query_gateway_control
    from gateway.run import _profile_runtime_scope, _start_gateway_start_control_socket
    from gateway.run_plugin_rewire import GatewayPluginRewireMixin

    home = tmp_path / "home"
    folder = home / "plugins" / "fixture"
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text("name: fixture\nversion: '1.0'\n")
    source = textwrap.dedent('''
        import asyncio
        from threading import Event
        release, finished = Event(), Event()
        calls = []

        def sync_hook(**kwargs):
            calls.append('sync')
            try:
                release.wait(10)
                return {'action': 'block', 'message': 'late'}
            finally:
                finished.set()

        async def async_hook(**kwargs):
            calls.append('async')
            try:
                await asyncio.sleep(10)
            finally:
                finished.set()

        def register(ctx):
            ctx.register_hook('pre_tool_call', CALLBACK)
    ''').replace("CALLBACK", callback_kind + "_hook")
    (folder / "__init__.py").write_text(source)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [fixture]\n  hook_callback_timeout: 0.1\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: home)
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: home / "no-bundled")
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    # Keep the real Unix transport inside this test's temp directory even on long CI paths.
    monkeypatch.setattr("gateway.control_socket._fallback_socket_path",
                        lambda owner: tmp_path.parent / f"compose-{callback_kind}.sock")

    rewired = []

    class Adapter:
        def rewire_plugin_handlers(self):
            rewired.append(plugins.get_plugin_manager()._plugins["fixture"].module)

    class Runner(GatewayPluginRewireMixin):
        config = SimpleNamespace(multiplex_profiles=False)
        adapters = {"fixture": Adapter()}

        def served_profile_names(self):
            return ["default"]

    with _profile_runtime_scope(home, hydrate_secrets=False):
        manager = plugins.get_plugin_manager()
        manager.discover_and_load()
        old = manager._plugins["fixture"].module
        runner = Runner()
        runner._subscribe_plugin_rewire(manager, profile_home=home)
        server = await _start_gateway_start_control_socket(runner)
        assert server is not None

        async def query(verb, **kwargs):
            return await asyncio.to_thread(query_gateway_control, home, verb, **kwargs)

        try:
            identity = await query("identify")
            assert identity["hermes_home"] == str(home)
            legacy = await query("rescan-profiles")
            assert legacy == {"multiplex": False, "served_profiles": ["default"]}
            with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
                if callback_kind == "async":
                    result = await manager.ainvoke_hook("pre_tool_call", tool_call_id="first")
                else:
                    result = await asyncio.to_thread(
                        manager.invoke_hook, "pre_tool_call", tool_call_id="first")
            assert result[0]["action"] == "block" and "timed out" in result[0]["message"]
            warning, = [r for r in caplog.records if getattr(r, "event", None) == "plugin_hook_timeout"]
            assert warning.plugin_hook["plugin"] == "fixture"
            assert json.loads(warning.getMessage().split(" ", 1)[1])["hook"] == "pre_tool_call"
            health = (await query("status"))["plugin_health"][manager.scope_key]
            disabled, = health["disabled_hooks"]
            assert disabled["plugin"] == "fixture" and disabled["quarantined"]
            assert disabled["registered"] and disabled["callback"].endswith(callback_kind + "_hook")

            old.release.set()
            assert await asyncio.to_thread(old.finished.wait, 5)
            # Completion must update diagnostics without clearing the dispatcher's quarantine.
            async with asyncio.timeout(5):
                while True:
                    health = (await query("status"))["plugin_health"][manager.scope_key]
                    if not health["stuck_hooks"]:
                        break
                    await asyncio.sleep(0.01)
            assert health["disabled_hooks"][0]["running_workers"] == 0
            assert await manager.ainvoke_hook("pre_tool_call", tool_call_id="later") == []
            assert old.calls == [callback_kind]

            (folder / "__init__.py").write_text(
                "def hook(**kwargs):\n    return {'recovered': True}\n"
                "def register(ctx):\n    ctx.register_hook('pre_tool_call', hook)\n")
            # Exercise the complete CLI entry point against the socket composed by gateway startup.
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "hermes_cli.main", "plugins", "reload", "fixture", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, (stdout, stderr)
            answer = json.loads(stdout)
            assert answer["reloaded"] and answer["plugin"] == "fixture"
            assert answer["home"] == manager.scope_key and answer["handlers_rewired"]
            replacement = manager._plugins["fixture"].module
            assert replacement is not old and rewired == [replacement]
            assert await manager.ainvoke_hook("pre_tool_call", tool_call_id="recovered") == [{"recovered": True}]
            after = await query("status")
            assert after["answering_pid"] == identity["pid"]
            assert after["plugin_health"][manager.scope_key]["disabled_hooks"] == []
            assert after["plugin_health"][manager.scope_key]["timed_out_hooks"] == []
            # The existing reload-all handler remains available alongside targeted reload.
            assert (await query("reload-plugins", timeout=10))["reloaded"]
        finally:
            old.release.set()
            await server.stop()
            for unsubscribe in runner._plugin_rewire_unsubscribe.values():
                unsubscribe()
            manager.unload()
