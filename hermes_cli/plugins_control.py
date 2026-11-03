"""Plugin handlers for the gateway's existing local control socket.

The gateway composes these with its other verb handlers. No listener, process, or transport is added.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def plugin_health_status() -> dict:
    """Read existing managers without discovery, imports of plugin code, or profile switching."""
    from hermes_cli.plugins import _known_plugin_managers

    return {manager.scope_key: manager.hook_health.snapshot() for manager in _known_plugin_managers()}


def plugin_control_handlers(runner, loop: asyncio.AbstractEventLoop, home: Path) -> dict:
    """Add ``reload-plugin`` and enrich ``status`` with ``plugin_health``.

    ``home`` is the socket owner's home; the runner authorizes any secondary profile before the
    operation binds its complete runtime scope. Call from a control worker, never the gateway loop.
    """
    from hermes_constants import hermes_home_key

    owner_key = hermes_home_key(home)

    def status() -> dict:
        from gateway.control_socket import build_status_payload
        return {**build_status_payload(), "plugin_health": plugin_health_status()}

    def reload_one(params: dict) -> dict:
        from gateway.run import _profile_runtime_scope
        from hermes_cli.plugins import get_plugin_manager

        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            return {"reloaded": False, "error": "name must identify one plugin"}
        requested = Path(str(params.get("home") or home)).expanduser()
        key = hermes_home_key(requested)
        served = getattr(runner, "_served_profile_homes", None) or {}
        if key != owner_key and key not in {hermes_home_key(h) for h in served.values()}:
            return {"reloaded": False, "error": "home is not served by this gateway", "home": str(requested)}
        with _profile_runtime_scope(requested, hydrate_secrets=False):
            result = get_plugin_manager().reload_plugin(name)
        if result["reloaded"]:
            # The existing loaded-event subscriber queues adapter rewiring on this loop. A barrier
            # queued afterwards lets the caller distinguish registered hooks from completed rewiring.
            async def barrier():
                return True

            pending = asyncio.run_coroutine_threadsafe(barrier(), loop)
            try:
                result["handlers_rewired"] = pending.result(timeout=5.0)
            except TimeoutError:
                pending.cancel()
                result["handlers_rewired"] = False
        return result

    return {"reload-plugin": reload_one, "status": status}
