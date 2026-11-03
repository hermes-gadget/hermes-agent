"""Profile-local hook timeout diagnostics; execution and cancellation stay with the dispatcher."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("hermes_cli.plugins")


class PluginHookHealth:
    def __init__(self, home: str) -> None:
        self.home = home
        self._lock = threading.RLock()
        self._owners: dict[tuple, list[str]] = {}
        self._timeouts: dict[tuple, dict[str, Any]] = {}
        self._workers: dict[tuple, set[object]] = {}

    def register(self, hook: str, callback: Callable, plugin: str) -> None:
        with self._lock:
            self._owners.setdefault((hook, id(callback)), []).append(plugin)

    def unregister(self, hook: str, callback: Callable, plugin: str) -> None:
        key = (hook, id(callback))
        with self._lock:
            owners = self._owners.get(key, [])
            if plugin in owners:
                owners.remove(plugin)
            if not owners:
                self._owners.pop(key, None)
                if not self._workers.get(key):
                    self._timeouts.pop(key, None)

    def owner(self, hook: str, callback: Callable) -> str | None:
        with self._lock:
            owners = self._owners.get((hook, id(callback)), ())
            return owners[-1] if owners else None

    def timed_out(self, hook: str, callback: Callable, timeout: float, *,
                  plugin: str | None, worker: object | None = None) -> None:
        key = (hook, id(callback))
        row = {
            "plugin": plugin,
            "hook": hook,
            "callback": f"{getattr(callback, '__module__', type(callback).__module__)}."
                        f"{getattr(callback, '__qualname__', type(callback).__qualname__)}",
            "home": self.home,
            "timeout_seconds": timeout,
            "timed_out_at": time.time(),
        }
        with self._lock:
            self._timeouts[key] = row
            if worker is not None:
                self._workers.setdefault(key, set()).add(worker)
        # JSON in the message survives plain-text formatters; extras serve structured collectors.
        logger.warning("plugin_hook_timeout %s", json.dumps(row, sort_keys=True),
                       extra={"event": "plugin_hook_timeout", "plugin_hook": row})

    def finished(self, hook: str, callback: Callable, worker: object) -> None:
        key = (hook, id(callback))
        with self._lock:
            workers = self._workers.get(key)
            if workers is not None:
                workers.discard(worker)
                if not workers:
                    self._workers.pop(key, None)
                    if key not in self._owners:
                        self._timeouts.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [{**row, "running_workers": len(self._workers.get(key, ())),
                     "registered": key in self._owners}
                    for key, row in self._timeouts.items()]
        return {"stuck_hooks": [r for r in rows if r["running_workers"]], "timed_out_hooks": rows}


def register_hook(context, hook: str, callback: Callable):
    """Attach attribution to the same disposable registration that owns the callback."""
    from hermes_cli.plugins import VALID_HOOKS
    from hermes_cli.plugins_manifest import manifest_key

    handle = context._track_callback("hook", hook, callback, context._manager._hooks, VALID_HOOKS)
    health = context._manager.hook_health
    plugin = manifest_key(context.manifest)
    health.register(hook, callback, plugin)
    remove = handle.release

    def release() -> None:
        remove()
        health.unregister(hook, callback, plugin)

    handle.release = release
    return handle
