"""HermesMind #707: a timed-out hook must not poison later tool calls."""

import asyncio
import json
import threading
import textwrap
import time
from contextlib import contextmanager

import pytest
import hermes_yaml as yaml


def _write_plugin(home, name, source):
    directory = home / "plugins" / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(yaml.safe_dump({"name": name, "version": "0.1.0"}))
    (directory / "__init__.py").write_text(textwrap.dedent(source))


@contextmanager
def _profile_scope(home):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope({}, profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@pytest.mark.parametrize("callback_kind", ["sync", "async"])
def test_timed_out_plugin_does_not_poison_tool_calls_until_reload(tmp_path, monkeypatch, callback_kind):
    import hermes_cli.plugins as plugins
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from model_tools import handle_function_call

    home = tmp_path / "home"
    _write_plugin(home, "timeout_probe", '''
        import asyncio
        import threading

        release = threading.Event()
        finished = threading.Event()
        calls = []

        def sync_hook(**kwargs):
            calls.append(kwargs["tool_call_id"])
            try:
                release.wait(10)
                return {"action": "block", "message": "late result"}
            finally:
                finished.set()

        async def async_hook(**kwargs):
            calls.append(kwargs["tool_call_id"])
            try:
                if not release.is_set():
                    await asyncio.sleep(10)
                return {"action": "block", "message": "late result"}
            finally:
                finished.set()

        def register(ctx):
            ctx.register_hook("pre_tool_call", CALLBACK)
    '''.replace("CALLBACK", callback_kind + "_hook"))
    _write_plugin(home, "healthy_probe", '''
        import json

        calls = []

        def guard(tool_call_id, args, **kwargs):
            calls.append(tool_call_id)
            if args.get("deny"):
                return {"action": "block", "message": "healthy guard denied"}

        def register(ctx):
            ctx.register_hook("pre_tool_call", guard)
            for name in ("supervisor_probe_a", "supervisor_probe_b"):
                ctx.register_tool(
                    name=name, toolset="supervisor_probe",
                    schema={"name": name, "parameters": {"type": "object", "properties": {}}},
                    handler=lambda args, **kwargs: json.dumps({"ok": True}),
                )
    ''')
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["timeout_probe", "healthy_probe"], "hook_callback_timeout": 0.1,
    }}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: tmp_path / "no-bundled")
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    manager = plugins.get_plugin_manager()
    manager.discover_and_load()
    bad = manager._plugins["timeout_probe"].module
    good = manager._plugins["healthy_probe"].module

    def call(call_id, tool="supervisor_probe_a", **args):
        return json.loads(handle_function_call(tool, args, session_id=call_id, tool_call_id=call_id))

    try:
        started = time.monotonic()
        first = call("first")
        assert "timed out" in first["error"]
        assert time.monotonic() - started < 5
        second = call("second", "supervisor_probe_b")
        assert second == {"ok": True}, second
        assert bad.calls == ["first"]
        assert good.calls == ["first", "second"]
        # The same plugin in another served profile still runs: A -> B -> A under multiplex.
        home_b = tmp_path / "home-b"
        for name in ("timeout_probe", "healthy_probe"):
            _write_plugin(home_b, name, (home / "plugins" / name / "__init__.py").read_text())
        (home_b / "config.yaml").write_text((home / "config.yaml").read_text())
        was_multiplex = is_multiplex_active()
        set_multiplex_active(True)
        try:
            with _profile_scope(home):
                assert call("profile-a") == {"ok": True}
            with _profile_scope(home_b):
                manager_b = plugins.get_plugin_manager()
                manager_b.discover_and_load()
                try:
                    bad_b = manager_b._plugins["timeout_probe"].module
                    bad_b.release.set()
                    assert call("profile-b") == {"error": "late result"}
                    assert bad_b.calls == ["profile-b"]
                finally:
                    manager_b.unload()
            with _profile_scope(home):
                assert plugins.get_plugin_manager() is manager
                assert call("profile-a-return") == {"ok": True}
                assert bad.calls == ["first"]
        finally:
            set_multiplex_active(was_multiplex)
        if callback_kind == "async":
            assert bad.finished.wait(5), "timed-out coroutine was not cancelled"
        bad.release.set()
        assert bad.finished.wait(5)
        # Neither worker completion nor switching off the budget re-enables the hook.
        monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
        assert call("third") == {"ok": True}
        assert call("denied", deny=True) == {"error": "healthy guard denied"}
        assert bad.calls == ["first"]
        monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.1)
        manager.discover_and_load(force=True)
        reloaded = manager._plugins["timeout_probe"].module
        reloaded.release.set()
        # Explicit reload, using the existing discovery path, makes the callback live again.
        assert call("reloaded")["error"] == "late result"
        assert reloaded.calls == ["reloaded"]
        assert reloaded.finished.wait(5)
    finally:
        bad.release.set()
        manager.unload()


@pytest.mark.parametrize("hook_name", ["pre_tool_call", "post_tool_call", "pre_llm_call"])
@pytest.mark.parametrize("callback_kind", ["async", "sync", "ignores_cancellation"])
def test_async_timeout_disables_only_the_bad_callback(monkeypatch, hook_name, callback_kind):
    from hermes_cli.plugins import PluginManager

    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.1)
    manager = PluginManager()
    calls = []

    async def scenario():
        cancelled = asyncio.Event()
        release = asyncio.Event()
        sync_release = threading.Event()
        finished = threading.Event()

        async def bad(**kwargs):
            calls.append("bad")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                if callback_kind == "ignores_cancellation":
                    await release.wait()
                    return "late"
                raise
            finally:
                finished.set()

        def sync_bad(**kwargs):
            calls.append("bad")
            try:
                sync_release.wait(10)
                return "late"
            finally:
                finished.set()

        async def good(**kwargs):
            calls.append("good")
            return "healthy"

        manager._hooks[hook_name] = [sync_bad if callback_kind == "sync" else bad, good]
        try:
            started = time.monotonic()
            first = await manager.ainvoke_hook(hook_name)
            assert time.monotonic() - started < 5
            assert first[-1] == "healthy"
            if hook_name == "pre_tool_call":
                assert first[0]["action"] == "block"
            if callback_kind != "sync":
                await asyncio.wait_for(cancelled.wait(), 5)
            assert await manager.ainvoke_hook(hook_name) == ["healthy"]
            assert manager.invoke_hook(hook_name) == ["healthy"]
            assert calls == ["bad", "good", "good", "good"]
        finally:
            release.set()
            sync_release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            manager.unload()

    asyncio.run(scenario())
