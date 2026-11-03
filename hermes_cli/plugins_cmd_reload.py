"""CLI client for one-plugin reload; never falls back to all-plugin discovery or a restart."""

from __future__ import annotations

import json
from pathlib import Path


def request_plugin_reload(name: str) -> dict:
    from gateway.control_socket import identify_gateway, query_gateway_control
    from hermes_constants import get_default_hermes_root, get_hermes_home

    home = Path(get_hermes_home())
    endpoint = home
    identity = identify_gateway(endpoint)
    if identity is None:
        root = Path(get_default_hermes_root())
        if root.resolve() != home.resolve():
            endpoint, identity = root, identify_gateway(root)
    if identity is None:
        return {"reloaded": False, "plugin": name, "error": "No running gateway answered for this profile"}
    # Resolve the endpoint BEFORE the mutation. A lost response must never cause a second reload.
    result = query_gateway_control(endpoint, "reload-plugin", params={"name": name, "home": str(home)},
                                   timeout=30.0)
    if result is None:
        return {"reloaded": False, "plugin": name,
                "error": "Gateway did not confirm single-plugin reload (unsupported verb or timeout); "
                         "check gateway status/logs before retrying"}
    return result


def cmd_reload(args) -> None:
    result = request_plugin_reload(args.name)
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True))
    elif result.get("reloaded"):
        print(f"Reloaded plugin '{result['plugin']}' in the running gateway. "
              "Hooks are active; tool schemas and system prompt changes apply next session.")
        if result.get("handlers_rewired") is False:
            print("Adapter rewiring is still pending; check gateway logs.")
    else:
        print(f"Plugin reload failed: {result.get('error', 'unknown error')}")
    if not result.get("reloaded"):
        raise SystemExit(1)
