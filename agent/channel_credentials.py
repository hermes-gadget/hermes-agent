"""Opt-in channel key reservations, separate from the provider's shared rotation pool."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from hermes_constants import hermes_home_key
from utils import is_truthy_value


logger = logging.getLogger(__name__)
_FREE_MODELS = frozenset({"longcat-2.5-preview-free"})
_coordinators = {}
_coordinators_lock = threading.RLock()


class ChannelCredentialUnavailable(ValueError):
    """A pinned channel must surface its unavailable key instead of selecting a fallback."""


def coordinator_for(channel_key, provider, model, policy):
    if provider != "opencode-go" or model not in _FREE_MODELS:
        raise ValueError("Channel credential reservations require OpenCode Go LongCat 2.5 Preview Free.")
    main = str(policy.get("main") or "").strip()
    workers, fallbacks = policy.get("subagents") or [], policy.get("fallbacks") or []
    if (not main or not isinstance(workers, list) or not isinstance(fallbacks, list)
            or not (workers or fallbacks) or (workers and fallbacks)
            or any(not isinstance(v, str) or not v.strip() for v in (*workers, *fallbacks))):
        raise ValueError("Channel credentials require a main key and either subagent keys or fallback keys.")
    workers, fallbacks = tuple(v.strip() for v in workers), tuple(v.strip() for v in fallbacks)
    signature = (main, workers, is_truthy_value(policy.get("allow_exhausted_free_models")), model, fallbacks)
    key = (hermes_home_key(), channel_key, provider)
    with _coordinators_lock:
        current = _coordinators.get(key)
        if current is None or current.signature != signature:
            if current is not None and current.busy():
                raise ValueError("Channel key assignments changed while workers are active; stop them before changing assignments.")
            current = _Coordinator(provider, signature)
            _coordinators[key] = current
        return current


class _Coordinator:
    def __init__(self, provider, signature):
        self.provider, self.signature = provider, signature
        self.main, self.workers, self.allow_exhausted, self.model, self.fallbacks = signature
        self.main_label = self.main
        self.condition = threading.Condition(threading.RLock())
        self.leases = set()

    def busy(self):
        with self.condition:
            return bool(self.leases)

    def entries(self):
        from agent.credential_pool import load_pool

        entries = load_pool(self.provider).entries()
        selected = {}
        for label in (self.main, *self.workers, *self.fallbacks):
            matches = [e for e in entries if e.id == label or e.label == label]
            if (len(matches) != 1 or not matches[0].runtime_api_key
                    or matches[0].auth_type != "api_key"):
                raise ValueError(f"Channel credential '{label}' is missing, ambiguous, or empty.")
            selected[label] = matches[0]
        values = list(selected.values())
        if len(selected) != 1 + len(self.workers) + len(self.fallbacks) or len({e.runtime_api_key for e in values}) != len(values):
            raise ValueError("Channel credentials must all be distinct keys.")
        return selected

    def blocked_until(self, entry):
        from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until
        from agent.credential_pool_model_cooldowns import model_cooldown_until

        if entry.last_status == STATUS_DEAD:
            return float("inf")
        until = model_cooldown_until(entry, self.model) or 0
        # Only the documented paid-usage wall is exempt. Auth errors, actual
        # LongCat throttles and model cooldowns remain binding.
        known_usage_wall = (entry.last_error_reason == "GoUsageLimitError"
                            or "GoUsageLimitError" in (entry.last_error_message or ""))
        exempt = self.allow_exhausted and entry.last_error_code == 429 and known_usage_wall
        if entry.last_status == STATUS_EXHAUSTED and not exempt:
            until = max(until, _exhausted_until(entry, sole_credential=True) or 0)
        return until

    def main_pool(self):
        if self.fallbacks:
            return RotatingChannelCredentialPool(self, self.main_label)
        return ChannelCredentialPool(self, self.main)

    def worker_pool(self):
        return ChannelCredentialPool(self, None)

    def reserve(self, child):
        with self.condition:
            while True:
                if getattr(child, "_interrupt_requested", False):
                    raise ValueError("Subagent stopped while waiting for its channel credential.")
                entries = self.entries()
                if all(self.blocked_until(entries[label]) == float("inf") for label in self.workers):
                    raise ValueError("All channel worker credentials are invalid; re-authenticate a worker key.")
                for label, entry in entries.items():
                    if label != self.main and entry.id not in self.leases and self.blocked_until(entry) <= time.time():
                        self.leases.add(entry.id)
                        return label, entry
                # A queued child is alive; keep async-dispatch liveness from
                # treating time spent behind its siblings as a provider stall.
                child._last_activity_ts = time.time()
                self.condition.wait(timeout=1)

    def release(self, credential_id):
        with self.condition:
            self.leases.discard(credential_id)
            self.condition.notify_all()


class ChannelCredentialPool:
    """A single agent's view: it can never rotate into the main key or a sibling's key."""

    channel_pinned = True

    def __init__(self, coordinator, label):
        self.coordinator, self.label = coordinator, label
        self.provider, self.model = coordinator.provider, coordinator.model
        self.lease_id = None

    def entries(self):
        return [self.coordinator.entries()[self.label]] if self.label else []

    def current(self):
        return next(iter(self.entries()), None)

    def entry_id_for_api_key(self, api_key):
        return next((e.id for e in self.entries() if e.runtime_api_key == api_key), None)

    def has_credentials(self):
        return bool(self.entries())

    def select(self, *, model=None):
        if model and model != self.model:
            return None
        entry = self.current()
        return entry if entry and self.coordinator.blocked_until(entry) <= time.time() else None

    def has_available(self, *, model=None):
        return self.select(model=model) is not None

    def peek(self, *, model=None):
        return self.select(model=model)

    def next_available_at(self, *, model=None):
        entry = self.current()
        until = self.coordinator.blocked_until(entry) if entry else None
        return until if until and until != float("inf") and until > time.time() else None

    def reclaim(self, credential_id, *, model=None):
        entry = self.select(model=model)
        return entry if entry and entry.id == credential_id else None

    def try_refresh_matching(self, **kwargs):
        return None  # This policy accepts API keys, not refreshable OAuth grants.

    def try_refresh_current(self):
        return None

    def accepts(self, entry):
        current = self.current()
        return current is not None and current.id == getattr(entry, "id", None)

    def mark_exhausted_and_rotate(self, **kwargs):
        from agent.credential_pool import _exhausted_ttl, _normalize_error_context, load_pool

        entry = self.current()
        if entry is None:
            return None
        pool = load_pool(self.provider)
        if kwargs.get("status_code") in (401, 403):
            pool.mark_exhausted_and_rotate(**{**kwargs, "credential_id": entry.id})
        else:
            # A LongCat failure must not erase paid-usage exhaustion or bench
            # DeepSeek. Persist the existing model-scoped cooldown instead.
            with pool._lock:
                context = _normalize_error_context(kwargs.get("error_context"))
                # This agent has exactly one key. The shared pool's four-key
                # one-hour bench does not describe its retry options.
                context.setdefault("reset_at", time.time() + _exhausted_ttl(429, sole_credential=True))
                pool._cool_down_model(entry, self.model, context, kwargs.get("failure_reason"))
        return None

    def record_success(self, *, api_key):
        """A successful retry retires only this key's observed LongCat cooldown."""
        from dataclasses import replace
        from agent.credential_pool import load_pool
        from hermes_cli import auth

        pool = load_pool(self.provider)
        with pool._lock:
            entry = next((e for e in pool.entries() if e.runtime_api_key == api_key and self.accepts(e)), None)
            observed = (entry.model_cooldowns or {}).get(self.model) if entry else None
            if observed is None:
                return
            # Read the latest row under the store lock: preserve paid quota,
            # sibling model cooldowns and any newer failure from another writer.
            with auth._auth_store_lock():
                store = auth._load_auth_store()
                rows = store.get("credential_pool", {}).get(self.provider, [])
                for row in rows:
                    if row.get("id") != entry.id or row.get("access_token") != entry.access_token:
                        continue
                    cooldowns = dict(row.get("model_cooldowns") or {})
                    if cooldowns.get(self.model, 0) > observed:
                        return
                    cooldowns.pop(self.model, None)
                    row["model_cooldowns"] = cooldowns
                    auth._save_auth_store(store)
                    pool._replace_entry(entry, replace(entry, model_cooldowns=cooldowns))
                    logger.info("Channel credential recovered: label=%s model=%s", self.label, self.model)
                    return

    def runtime(self):
        from hermes_cli.runtime_provider import resolve_runtime_provider

        entry = self.select(model=self.model)
        if entry is None:
            raise ChannelCredentialUnavailable(
                "This channel's LongCat credentials are currently unavailable. "
                "No fallback model is configured; retry later."
            )
        runtime = resolve_runtime_provider(
            requested=self.provider, target_model=self.model, explicit_api_key=entry.runtime_api_key,
            explicit_base_url=entry.runtime_base_url,
        )
        runtime["credential_pool"] = self
        logger.info("Channel main credential selected: label=%s model=%s", self.label, self.model)
        return runtime

    def release(self):
        if self.lease_id is not None:
            self.coordinator.release(self.lease_id)
            self.lease_id = None


class RotatingChannelCredentialPool(ChannelCredentialPool):
    """Same-model key failover for a channel without native children."""

    rotate_on_rate_limit = True

    def __init__(self, coordinator, label):
        super().__init__(coordinator, label)
        self._tried = set()
        self._turn_id = None

    def begin_turn(self, turn_id):
        if turn_id != self._turn_id:
            self._turn_id = turn_id
            self._tried.clear()

    def entries(self):
        return list(self.coordinator.entries().values())

    def current(self):
        return self.coordinator.entries().get(self.label)

    def accepts(self, entry):
        return any(e.id == getattr(entry, "id", None) for e in self.entries())

    def _choose(self, *, advance=False, exclude=()):
        with self.coordinator.condition:
            entries = self.coordinator.entries()
            labels = list(entries)
            start = labels.index(self.label) + int(advance)
            for offset in range(len(labels)):
                label = labels[(start + offset) % len(labels)]
                entry = entries[label]
                if entry.id not in exclude and self.coordinator.blocked_until(entry) <= time.time():
                    self.label = self.coordinator.main_label = label
                    return entry
        return None

    def select(self, *, model=None):
        return None if model and model != self.model else self._choose()

    def next_available_at(self, *, model=None):
        if model and model != self.model:
            return None
        deadlines = [self.coordinator.blocked_until(e) for e in self.entries()]
        if any(v <= time.time() for v in deadlines):
            return None
        finite = [v for v in deadlines if time.time() < v < float("inf")]
        return min(finite, default=None)

    def reclaim(self, credential_id, *, model=None):
        if model and model != self.model:
            return None
        with self.coordinator.condition:
            for label, entry in self.coordinator.entries().items():
                if entry.id == credential_id and self.coordinator.blocked_until(entry) <= time.time():
                    self.label = self.coordinator.main_label = label
                    return entry
        return None

    def mark_exhausted_and_rotate(self, **kwargs):
        # Attribute failures to the dispatched key, even if another availability
        # check changed the selection pointer while its request was in flight.
        entries = self.coordinator.entries()
        credential_id, api_key = kwargs.get("credential_id"), kwargs.get("api_key_hint")
        failed = next(((label, entry) for label, entry in entries.items()
                       if (credential_id and entry.id == credential_id)
                       or (api_key and entry.runtime_api_key == api_key)), None)
        if (credential_id or api_key) and failed is None:
            return None
        if failed is not None:
            self.label = failed[0]
        entry = self.current()
        self._tried.add(entry.id)
        previous = self.label
        super().mark_exhausted_and_rotate(**kwargs)
        next_entry = self._choose(advance=True, exclude=self._tried)
        if next_entry is not None:
            logger.info("Channel LongCat credential rotated: from=%s to=%s model=%s", previous, self.label, self.model)
        return next_entry

    def record_success(self, *, api_key):
        self._tried.clear()
        with self.coordinator.condition:
            for label, entry in self.coordinator.entries().items():
                if entry.runtime_api_key == api_key:
                    self.label = self.coordinator.main_label = label
                    break
        super().record_success(api_key=api_key)


@contextmanager
def worker_credentials(child):
    """Hold a strict reservation through the last real worker, including deferred exit."""
    pool = getattr(child, "_credential_pool", None)
    if not isinstance(pool, ChannelCredentialPool):
        yield
        return
    try:
        pool.label, entry = pool.coordinator.reserve(child)
        pool.lease_id = entry.id
        if child._swap_credential(entry) is False:
            raise ValueError("Could not bind the subagent's reserved channel credential.")
        if hasattr(child, "_primary_runtime"):
            from agent.agent_init import _snapshot_primary_runtime
            compressor = child.context_compressor
            compressor.update_model(
                model=child.model, context_length=compressor.context_length, base_url=child.base_url,
                api_key=child.api_key, provider=child.provider, api_mode=child.api_mode,
            )
            # Transport recovery restores this snapshot; it must never restore
            # the construction-time placeholder instead of the reserved key.
            _snapshot_primary_runtime(child)
        logger.info("Channel subagent credential assigned: label=%s session=%s", pool.label, getattr(child, "session_id", ""))
        yield
    finally:
        future = getattr(child, "_channel_credential_future", None)
        if future is not None and not future.done():
            future.add_done_callback(lambda _done: pool.release())
        else:
            pool.release()
