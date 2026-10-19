"""Conversation-scoped delegation settings carried through copied worker contexts."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

from utils import is_truthy_value


_CHANNEL_KEYS = ("enabled", "model", "provider", "max_concurrent_children", "max_spawn_depth")
_channel_delegation: ContextVar[dict[str, Any] | None] = ContextVar("channel_delegation", default=None)


@contextmanager
def scoped_channel_delegation(overrides: Mapping[str, Any] | None) -> Iterator[None]:
    """Bind a channel's settings without changing shared config or another conversation."""
    values = {k: overrides[k] for k in _CHANNEL_KEYS if overrides and overrides.get(k) is not None}
    token = _channel_delegation.set(values)
    try:
        yield
    finally:
        _channel_delegation.reset(token)


def delegation_scope_key() -> tuple:
    """Include channel settings in caches that freeze the delegate tool schema."""
    return tuple(sorted((k, str(v)) for k, v in (_channel_delegation.get() or {}).items()))


def channel_delegation_enabled() -> bool:
    """Channel policy applies to tool selection and to calls from existing history."""
    return is_truthy_value((_channel_delegation.get() or {}).get("enabled"), default=True)


def apply_channel_delegation(config: dict) -> dict:
    overrides = _channel_delegation.get()
    if not overrides:
        return config
    merged = dict(config)
    if overrides.get("provider"):
        # A channel provider owns its endpoint/credentials/transport as one bundle.
        # Global direct-endpoint or ACP settings must not redirect that route.
        for key in ("base_url", "api_key", "api_mode", "command", "args", "request_overrides"):
            merged.pop(key, None)
    if overrides.get("provider") or overrides.get("model"):
        # Fallbacks belong to the explicitly selected route, not the global one.
        merged.pop("fallback_providers", None)
    merged.update(overrides)
    return merged
