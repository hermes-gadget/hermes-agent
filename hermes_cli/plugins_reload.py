"""Targeted plugin replacement using the existing ownership ledger and activation event."""

from __future__ import annotations

import importlib
from typing import Any

from hermes_cli.plugins_loader import _plugin_home_scope


def reload_plugin(manager, name: str) -> dict[str, Any]:
    """Replace exactly one known plugin. Never rediscover/load siblings or refresh session prompts.

    Hooks already in flight retain their callback. While registration is being replaced, hook readers
    use the previous snapshot; publishing the new table does not wait for abandoned hook workers.
    Arbitrary plugin-owned resources remain the responsibility of its on_unload handlers.
    """
    from hermes_cli.plugins_activation import plugin_activation_summary
    from hermes_cli.plugins_discovery import (
        _get_disabled_plugins, _get_enabled_plugins, gate_manifest, resolve_manifest_winners,
    )
    from hermes_cli.plugins_manifest import manifest_key, requires_hermes_error
    from utils import env_var_enabled

    result: dict[str, Any] = {"reloaded": False, "plugin": name, "home": manager.scope_key}
    with manager._discovery_lock, _plugin_home_scope(manager.home_path):
        keys = manager._unload_target_keys(name)
        if len(keys) != 1:
            result["error"] = (f"Unknown plugin: {name}" if not keys else
                               f"Ambiguous plugin: {name}; use one of {', '.join(sorted(keys))}")
            return result
        key = keys.pop()
        result["plugin"] = key
        if env_var_enabled("HERMES_SAFE_MODE"):
            return {**result, "error": "Plugin reload is disabled in safe mode"}
        manifests = manager._collect_directory_manifests()
        directory_keys = {manifest_key(m) for m in manifests}
        manifests.extend(m for m in manager._scan_entry_points() if manifest_key(m) not in directory_keys)
        manifest = resolve_manifest_winners(manifests).get(key)
        if manifest is None:
            return {**result, "error": "Plugin manifest is no longer available"}
        verdict = gate_manifest(manifest, _get_disabled_plugins(), _get_enabled_plugins())
        if verdict.action not in {"load", "load_now"}:
            return {**result, "error": verdict.error or "Plugin lifecycle is owned by its provider/platform"}
        if reason := requires_hermes_error(manifest):
            return {**result, "error": reason}

        # Pip modules may be shared with other plugins/providers. Re-register their hooks through
        # ep.load(), as discovery does; a changed installed distribution needs a fresh process.
        loaded_before = frozenset(manager._plugins) - {key}
        manager._hook_reload_snapshot = {hook: tuple(callbacks) for hook, callbacks in manager._hooks.items()}
        try:
            manager.unload(key)
            importlib.invalidate_caches()
            manager._warn_python_dependencies(manifest)
            manager._validate_plugin_config_schema(manifest)
            manager._load_plugin(manifest)
            loaded = manager._plugins[key]
            if not loaded.enabled or loaded.error:
                # A partial registration can include persistent handles, which the loader's normal
                # failure sweep deliberately leaves alone. Targeted recovery owns their cleanup too.
                manager.unload(key)
                manager._plugins[key] = loaded
                return {**result, "error": loaded.error or "Plugin did not register", "unloaded": True}
            result.update(reloaded=True, activation=plugin_activation_summary(manager, key))
        finally:
            manager._hook_reload_snapshot = None
    manager._notify_plugin_loaded(loaded_before)
    return result
