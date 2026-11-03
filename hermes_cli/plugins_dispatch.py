"""Plugin hook / middleware / event-bus / system-prompt-section dispatch.

Mixed into :class:`hermes_cli.plugins.PluginManager`. ``_resolve_hook_callback_timeout`` stays on
the origin (tests patch it there) and is looked up lazily.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import logging
import queue
import re
import threading
import types
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Union

from hermes_cli.middleware import OBSERVER_SCHEMA_VERSION

logger = logging.getLogger("hermes_cli.plugins")

# Allowlist of agent-turn hot-path hooks bounded by plugins.hook_callback_timeout (fail-open:
# abandon without join — joining reintroduced a shutdown hang). Unlisted hooks run synchronously.
# Intentionally unbounded: on_session_finalize/reset (last-chance flush — abandon can lose state);
# subagent_start (observer); pre_gateway_dispatch (policy gate — neither fail mode is acceptable);
# pre/post_approval_* (approval UX has its own timeout); kanban_* (own heartbeat/stale reclaim).
# The goal is to stop a hung Python plugin callback from wedging the conversation loop (#76821) without
# joining the worker (avoids the #6622 ThreadPoolExecutor shutdown hang). Hooks not listed below run
# synchronously to completion. (on_session_start/end stay bounded — they sit on the common session-boundary
# path.) - subagent_start — observer only; blocking delegation belongs in pre_tool_call. Lower frequency
# than tool/LLM hooks. Abandoning is unsafe either way (fail-open skips auth-like checks; fail-closed can
# drop legitimate messages). Prefer finish-or-exception fallthrough. - pre_approval_request /
# post_approval_response — observers only (cannot veto); the approval UX already has its own timeout; not on
# the tool loop hot path. - kanban_task_* — fire after the board DB commit, observers only, in
# dispatcher/worker processes; kanban has its own heartbeat/stale reclaim. Abandon-without-join also leaves
# a daemon thread that may still mutate shared state — safer for value-returning observers than for
# gates/flushes.
_HOOK_TIMEOUT_BOUNDED_HOOKS: Set[str] = {
    "post_tool_call", "transform_terminal_output", "transform_tool_result", "transform_llm_output",
    "pre_llm_call", "post_llm_call", "pre_api_request", "post_api_request", "api_request_error",
    "pre_auxiliary_call", "post_auxiliary_call", "pre_verify", "on_session_start", "on_session_end",
}

# Policy hooks fail closed for the invocation that times out. Later calls skip the disabled hook.
_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS: Set[str] = {"pre_tool_call"}
# Documented parent-thread serialization contract — never run on a timeout worker (hooks.md).
_HOOK_CALLER_THREAD_HOOKS: Set[str] = {"subagent_stop"}
_PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE = "pre_tool_call plugin callback timed out or is still running"


def _policy_error_block_directive(hook_name: str, cb: Callable, exc: BaseException) -> Dict[str, str]:
    """Block directive for a fail-closed hook whose callback raised: names the callback and the
    error (truncated — a hook that embeds tool args in its exception must not grow the tool
    result) so the operator can tell a crashing guard from a slow one."""
    callback_name = getattr(cb, "__name__", repr(cb))
    return {"action": "block",
            "message": f"{hook_name} plugin callback {callback_name} raised {type(exc).__name__}: {str(exc)[:200]}"}

# System-prompt sections are tightly bounded: they become high-trust prompt bytes charged every turn.
SYSTEM_PROMPT_SECTION_POSITIONS = frozenset({"after_memory"})
DEFAULT_SYSTEM_PROMPT_SECTION_MAX_CHARS = 4_000
MAX_SYSTEM_PROMPT_SECTION_CHARS = 4_000
MAX_SYSTEM_PROMPT_SECTIONS = 32
MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS = 8_000
_SYSTEM_PROMPT_SECTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SYSTEM_PROMPT_SECTION_HEADING_PREFIX = "## Plugin Context: "
PLUGIN_SECTIONS_START = "<!-- hermes-plugin-sections:start -->"
PLUGIN_SECTIONS_END = "<!-- hermes-plugin-sections:end -->"


def is_valid_system_prompt_section_id(value: Any) -> bool:
    """Return whether *value* is a stable, heading-safe section identifier."""
    return isinstance(value, str) and bool(_SYSTEM_PROMPT_SECTION_ID_RE.fullmatch(value))


def format_system_prompt_section(section_id: str, content: str) -> str:
    """Render an auditable, length-framed block recoverable from the full prompt."""
    return (
        f"{_SYSTEM_PROMPT_SECTION_HEADING_PREFIX}{section_id}\n"
        f"<!-- hermes-plugin-section-chars:{len(content)} -->\n\n{content}")


def format_system_prompt_sections(sections: list) -> str:
    """Render the canonical container used for persistence recovery."""
    if not sections:
        return ""
    blocks = [format_system_prompt_section(item.id, item.content) for item in sections]
    return f"{PLUGIN_SECTIONS_START}\n" + "\n\n".join(blocks) + f"\n{PLUGIN_SECTIONS_END}"


# Reserved event namespace prefix — only core may publish ``hermes:<event>``.
HERMES_EVENT_NAMESPACE = "hermes"
# Event recursion depth cap (subscribers may emit); over-deep emits are dropped with a warning.
_EVENT_EMIT_DEPTH_CAP = 8
# Max queued + running events per manager generation; emit never waits — a full budget drops.
_EVENT_PENDING_CAP = 64
_EVENT_WORKER_STOP = object()


@dataclass(frozen=True)
class PluginSystemPromptSection:
    """A plugin-owned section rendered once for each new session."""

    id: str
    content: Union[str, Callable[[Mapping[str, Any]], str]]
    position: str
    max_chars: int
    plugin: str


@dataclass(frozen=True)
class RenderedPluginSystemPromptSection:
    """Validated prompt bytes frozen on the owning AIAgent."""

    id: str
    content: str
    position: str
    plugin: str


@dataclass(frozen=True)
class _EventSubscription:
    """Host-owned subscription ledger entry."""

    owner: str
    callback: Callable


@dataclass(frozen=True)
class _QueuedPluginEvent:
    """Immutable dispatch envelope consumed by the event worker."""

    event: str
    payload: Dict[str, Any]
    subscriptions: tuple[_EventSubscription, ...]
    depth: int
    generation: int
    # The emitter's contextvars: the single worker thread serves every profile, so each delivery
    # runs under the profile scope the emit happened in (#118538).
    context: contextvars.Context


# Hook callback timeout (non-blocking abandon). Default cap per Python hook callback; overridden by
# ``plugins.hook_callback_timeout``. Shell hooks enforce their own subprocess timeout.
_HOOK_CALLBACK_TIMEOUT_SECS = 30.0
_MAX_HOOK_CALLBACK_TIMEOUT_SECS = 600.0
_HOOK_SKIPPED = object()  # returned by _run_hook_callback_bounded on skip/timeout
_HOOK_DISABLED = object()  # already quarantined: skip this callback, not the tool call


class _HookRun:
    """Cancellation shared by a deadline caller and its callback's coroutine, if any.

    Python cannot interrupt a blocked synchronous thread. It is detached on timeout;
    an awaitable is cancelled on its owning loop, without waiting for plugin cleanup.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.finished = False
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._loop = None
        self._task = None

    def cancel(self) -> None:
        with self._lock:
            self.cancelled.set()
            if self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        self.done.set()

    async def await_result(self, result: Any) -> Any:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self.cancelled.is_set():
                self._task.cancel()
        try:
            return await result
        finally:
            # Unbind before the worker closes its loop; cancel() cannot race loop.close().
            with self._lock:
                self._task = self._loop = None


def _consume_hook_task(task: asyncio.Task) -> None:
    """Retrieve late exceptions from cancelled callbacks that suppress CancelledError."""
    if not task.cancelled():
        task.exception()


def _hook_call_identity(kwargs: Dict[str, Any]) -> Optional[str]:
    """Identity of the call this callback fires for, or ``None`` when the event has none.

    Concurrent invocations of the same tool in one session must not collapse into one
    gate key: they are different work, and treating the second as a duplicate drops the
    hook as if a callback had timed out (upstream #98382). The identity is already in the
    payload; nothing new is plumbed. Deliberately not ``api_request_id`` — one API request
    carries many tool calls, which would re-collapse the keys.
    """
    for field in ("tool_call_id", "turn_id"):
        value = kwargs.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _hook_uses_callback_timeout(hook_name: str, timeout: float) -> bool:
    """Whether *hook_name* should run under the non-blocking timeout path."""
    if timeout <= 0 or hook_name in _HOOK_CALLER_THREAD_HOOKS:
        return False
    return hook_name in _HOOK_TIMEOUT_BOUNDED_HOOKS or hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS


class PluginDispatchMixin:
    @staticmethod
    def _hook_callback_kwargs(callback: Callable, payload: Dict[str, Any]) -> Dict[str, Any]:
        """The slice of *payload* a callback accepts: everything for ``**kwargs`` (or
        un-introspectable) callbacks, only declared names for narrow legacy signatures."""
        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            return dict(payload)  # no introspectable signature
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            return dict(payload)
        keyword_kinds = {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        return {
            name: value for name, value in payload.items()
            if name in parameters and parameters[name].kind in keyword_kinds
        }

    @classmethod
    def _invoke_hook_callback(cls, callback: Callable, payload: Dict[str, Any]) -> Any:
        """Invoke a hook while withholding additive fields from narrow legacy callbacks.

        An ``async def`` callback returns a coroutine; resolve it the way plugin slash commands
        are (loop-safe), otherwise the bare coroutine object is appended to the results and the
        plugin's body never runs (#12449).
        """
        from hermes_cli.plugins import resolve_plugin_command_result
        return resolve_plugin_command_result(callback(**cls._hook_callback_kwargs(callback, payload)))

    def invoke_hook(self, hook_name: str, **kwargs: Any) -> List[Any]:
        """Call all callbacks for *hook_name*; return their non-``None`` results.

        Payloads evolve additively: ``**kwargs`` callbacks get everything, narrow signatures only
        what they declare. Each callback is isolated. Bounded hooks and ``pre_tool_call`` run under
        ``plugins.hook_callback_timeout``; a timeout disables only that callback until reload.
        The timing-out ``pre_tool_call`` fails closed; later calls skip the disabled callback.
        ``_HOOK_CALLER_THREAD_HOOKS`` always run on the caller thread. ``pre_llm_call`` may return
        ``{"context": "..."}`` (or a str) to inject.
        """
        from hermes_cli.plugins import _resolve_hook_callback_timeout
        # Gateway platform events define event-local envelopes; a bus-wide version here would turn
        # unrelated adapter payloads into one monolithic compatibility contract.
        if hook_name != "gateway_platform_event":
            kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
        results: List[Any] = []
        timeout = _resolve_hook_callback_timeout()
        use_timeout = _hook_uses_callback_timeout(hook_name, timeout)
        fail_closed = hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS
        for cb in self.iter_hook_callbacks(hook_name):
            if self._hook_callback_disabled(hook_name, cb):
                continue
            try:
                if use_timeout:
                    ret = self._run_hook_callback_bounded(hook_name, cb, kwargs, timeout)
                    if ret is _HOOK_DISABLED:
                        continue
                    if ret is _HOOK_SKIPPED:
                        if fail_closed:  # policy hook: fail closed with a block directive
                            results.append({"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE})
                        continue
                else:
                    ret = self._invoke_hook_callback(cb, kwargs)
                if ret is not None:
                    results.append(ret)
            except (Exception, SystemExit) as exc:
                self._report_hook_failure(hook_name, cb, kwargs, exc)
                if fail_closed:  # a guard that raised made no decision: same veto as a timeout
                    results.append(_policy_error_block_directive(hook_name, cb, exc))
        return results

    def _report_hook_failure(
        self, hook_name: str, cb: Callable, kwargs: Dict[str, Any], exc: BaseException, *, surface: str = "Hook"
    ) -> None:
        """One WARNING per distinct (hook, callback, error); identical repeats at DEBUG.

        A callback whose signature names a parameter the hook never sends (``tool_data`` instead
        of ``tool_name``/``args``) fails identically on every tool call — ~1700 WARNING lines an
        hour that bury real signals (#111922). The first report names the fields the hook does
        provide so the plugin author can fix the signature. The key names the callback by
        module/qualname (not ``id()``, which CPython recycles across plugin reloads) and
        truncates the message so a hook that embeds tool args in its error cannot grow the set
        per call; the set is cleared on unload alongside callback quarantine.
        """
        callback_name = getattr(cb, "__name__", repr(cb))
        key = (hook_name, getattr(cb, "__module__", ""), getattr(cb, "__qualname__", callback_name),
               type(exc).__name__, str(exc)[:200])
        if key in self._hook_failures_reported:
            logger.debug("%s '%s' callback %s raised again: %s", surface, hook_name, callback_name, exc)
            return
        self._hook_failures_reported.add(key)
        logger.warning(
            "%s '%s' callback %s raised: %s (%s provides: %s; identical failures are logged at DEBUG from now on)",
            surface, hook_name, callback_name, exc, surface.lower(), ", ".join(sorted(kwargs)) or "no fields")

    def _hook_callback_disabled(self, hook_name: str, cb: Callable) -> bool:
        with self._hook_timeout_lock:
            return (hook_name, id(cb)) in self._hook_disabled_callbacks

    def _begin_hook_run(self, hook_name: str, cb: Callable, kwargs: Dict[str, Any]):
        key = (hook_name, id(cb), _hook_call_identity(kwargs))
        with self._hook_timeout_lock:
            if key[:2] in self._hook_disabled_callbacks:
                return key, _HOOK_DISABLED
            if key in self._hook_running_callbacks:
                return key, _HOOK_SKIPPED
            run = _HookRun()
            self._hook_running_callbacks[key] = run
            return key, run

    def _finish_hook_run(self, key: tuple, run: _HookRun) -> None:
        with self._hook_timeout_lock:
            if self._hook_running_callbacks.get(key) is run:
                self._hook_running_callbacks.pop(key)

    def _hook_worker_finished(self, hook_name: str, cb: Callable, run: _HookRun) -> None:
        with self._hook_timeout_lock:
            run.finished = True
            self.hook_health.finished(hook_name, cb, run)

    def _disable_hook_callback(self, key: tuple, run: _HookRun, cb: Callable, timeout: float) -> None:
        with self._hook_timeout_lock:
            # A reload may have retired this invocation while it was waiting. Its deadline
            # must not disable a replacement registration, even if it uses the same callable.
            if self._hook_running_callbacks.get(key) is not run:
                return
            # Retain the callable so id reuse cannot quarantine an unrelated registration.
            self._hook_disabled_callbacks[key[:2]] = cb
            runs = [self._hook_running_callbacks.pop(k) for k in list(self._hook_running_callbacks)
                    if k[:2] == key[:2]]
            self.hook_health.timed_out(
                key[0], cb, timeout, plugin=self.hook_health.owner(key[0], cb),
                workers=tuple(active for active in runs if not active.finished))
        for active in runs:
            active.cancel()
        logger.warning(
            "Hook '%s' callback %s (%s) timed out after %gs — disabled until plugin reload",
            key[0], getattr(cb, "__name__", repr(cb)), getattr(cb, "__module__", "unknown plugin"), timeout)

    def _forget_hook_callback(self, hook_name: str, cb: Callable) -> None:
        """Registration teardown is the only reset; late workers cannot affect its replacement."""
        key = (hook_name, id(cb))
        with self._hook_timeout_lock:
            self._hook_disabled_callbacks.pop(key, None)
            runs = [self._hook_running_callbacks.pop(k) for k in list(self._hook_running_callbacks)
                    if k[:2] == key]
        for run in runs:
            run.cancel()

    def _run_hook_callback_bounded(
        self, hook_name: str, cb: Callable, kwargs: Dict[str, Any], timeout: float
    ) -> Any:
        """Cap caller latency, quarantine on timeout, and discard every late result."""
        key, run = self._begin_hook_run(hook_name, cb, kwargs)
        if run is _HOOK_DISABLED or run is _HOOK_SKIPPED:
            return run
        callback_name = getattr(cb, "__name__", repr(cb))
        context = contextvars.copy_context()
        outcome: Dict[str, Any] = {}
        failure: Dict[str, BaseException] = {}

        def _invoke() -> Any:
            if run.cancelled.is_set():
                return None
            ret = cb(**self._hook_callback_kwargs(cb, kwargs))
            return asyncio.run(run.await_result(ret)) if inspect.isawaitable(ret) else ret

        def _runner() -> None:
            try:
                outcome["value"] = context.run(_invoke)
            except BaseException as exc:
                failure["exc"] = exc
            finally:
                self._hook_worker_finished(hook_name, cb, run)
                run.done.set()

        thread = threading.Thread(target=_runner, name=f"hermes-hook-{callback_name}"[:40], daemon=True)
        try:
            try:
                thread.start()
            except RuntimeError as exc:
                logger.warning("Hook '%s' callback %s worker failed to start: %s — skipping",
                               hook_name, callback_name, exc)
                return _HOOK_SKIPPED
            if not run.done.wait(timeout=timeout):
                self._disable_hook_callback(key, run, cb, timeout)
                return _HOOK_SKIPPED
            if run.cancelled.is_set():
                return _HOOK_SKIPPED
            if "exc" in failure:
                raise failure["exc"]
            return outcome.get("value")
        finally:
            self._finish_hook_run(key, run)

    async def _run_hook_callback_async(
        self, hook_name: str, cb: Callable, kwargs: Dict[str, Any], timeout: float
    ) -> Any:
        # Keep async callbacks on the caller's loop; a synchronous callback must not block it.
        if not (inspect.iscoroutinefunction(cb) or inspect.iscoroutinefunction(getattr(cb, "__call__", None))):
            return await asyncio.to_thread(self._run_hook_callback_bounded, hook_name, cb, kwargs, timeout)
        key, run = self._begin_hook_run(hook_name, cb, kwargs)
        if run is _HOOK_DISABLED or run is _HOOK_SKIPPED:
            return run

        async def _invoke() -> Any:
            return await cb(**self._hook_callback_kwargs(cb, kwargs))

        task = asyncio.create_task(run.await_result(_invoke()))
        task.add_done_callback(_consume_hook_task)
        task.add_done_callback(lambda _: self._hook_worker_finished(hook_name, cb, run))
        try:
            # wait_for waits for cancellation cleanup, which a broken callback can suppress
            # forever. wait bounds the caller independently of the callback's cooperation.
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                self._disable_hook_callback(key, run, cb, timeout)
                return _HOOK_SKIPPED
            if run.cancelled.is_set():
                return _HOOK_SKIPPED
            return task.result()
        finally:
            if not task.done() and not run.cancelled.is_set():
                run.cancel()
            self._finish_hook_run(key, run)

    def _subscribe_event(self, owner: str, event: str, callback: Callable) -> None:
        """Add an owner-tagged event subscription in registration order."""
        if not callable(callback):
            raise TypeError("Event subscriber callback must be callable")
        with self._event_lock:
            self._subscriptions.setdefault(event, []).append(_EventSubscription(owner, callback))

    def _remove_plugin_subscriptions(self, owner: str) -> int:
        """Remove every subscription owned by *owner*; return the count. Queued envelopes re-check
        membership per callback, so this also cancels already-snapshotted deliveries.

        TODO(#64229): when the central plugin ownership ledger / registration handles land, route this
        owner-tagged bookkeeping through that ledger so per-plugin unload cancels event subscriptions
        alongside every other registration surface. This method is the integration seam.
        """
        removed = 0
        with self._event_lock:
            for event in list(self._subscriptions):
                entries = self._subscriptions[event]
                retained = [entry for entry in entries if entry.owner != owner]
                removed += len(entries) - len(retained)
                if retained:
                    self._subscriptions[event] = retained
                else:
                    del self._subscriptions[event]
        return removed

    def _ensure_event_worker_locked(self) -> None:
        worker = self._event_worker
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(
            target=self._event_worker_loop, args=(self._event_queue,), name="hermes-plugin-events",
            daemon=True,
        )
        self._event_worker = worker
        worker.start()

    def _event_worker_loop(self, dispatch_queue: queue.Queue[Any]) -> None:
        while True:
            item = dispatch_queue.get()
            try:
                if item is _EVENT_WORKER_STOP:
                    return
                self._deliver_event(item)
            finally:
                if item is not _EVENT_WORKER_STOP:
                    self._mark_event_done(item.generation)
                dispatch_queue.task_done()

    def _mark_event_done(self, generation: int) -> None:
        with self._event_idle:
            pending = self._event_pending_by_generation.get(generation, 0)
            if pending > 0:
                self._event_pending_by_generation[generation] = pending - 1
            self._event_idle.notify_all()

    def _deliver_event(self, item: _QueuedPluginEvent) -> None:
        """Deliver one queued event on the host-owned worker thread."""
        from hermes_cli.plugins import resolve_plugin_command_result
        with self._event_lock:
            if item.generation != self._event_generation:
                return
        previous_depth = getattr(self._emit_depth, "value", 0)
        self._emit_depth.value = item.depth
        try:
            for subscription in item.subscriptions:
                with self._event_lock:
                    if item.generation != self._event_generation:
                        break
                    # Owner unload may have removed this entry after the event was queued.
                    if not any(cur is subscription for cur in self._subscriptions.get(item.event, [])):
                        continue
                callback = subscription.callback
                try:
                    # Fresh deep copy per subscriber: no callback can mutate what the next sees.
                    resolve_plugin_command_result(
                        item.context.copy().run(callback, **copy.deepcopy(item.payload)))
                except (Exception, SystemExit) as exc:
                    # A subscriber that fails identically on every emit is reported once (#111922).
                    self._report_hook_failure(item.event, callback, item.payload, exc, surface="Event")
        finally:
            self._emit_depth.value = previous_depth

    def _wait_for_event_dispatch(self, timeout: float = 2.0) -> bool:
        """Wait for the current event generation to become idle (test helper)."""
        with self._event_idle:
            generation = self._event_generation
            return self._event_idle.wait_for(
                lambda: self._event_pending_by_generation.get(generation, 0) == 0, timeout=timeout)

    def _dispatch_event(self, event: str, payload: Dict[str, Any]) -> int:
        """Queue *event* without blocking; return the subscriber count scheduled. Pending work is
        bounded per generation so a blocking subscriber costs one worker and later emits drop."""
        depth = getattr(self._emit_depth, "value", 0)
        if depth >= _EVENT_EMIT_DEPTH_CAP:
            logger.warning(
                "Event bus recursion cap (%d) exceeded while dispatching '%s' "
                "— dropping this emit to prevent an infinite loop", _EVENT_EMIT_DEPTH_CAP, event)
            return 0
        budget_msg = "Event bus pending budget (%d) exhausted while dispatching '%s' — dropping this emit"
        with self._event_lock:
            subscriptions = tuple(self._subscriptions.get(event, []))
            if not subscriptions:
                return 0
            generation = self._event_generation
            pending = self._event_pending_by_generation.get(generation, 0)
            if pending >= _EVENT_PENDING_CAP:
                logger.warning(budget_msg, _EVENT_PENDING_CAP, event)
                return 0
            item = _QueuedPluginEvent(
                event=event, payload=dict(payload), subscriptions=subscriptions, depth=depth + 1,
                generation=generation, context=contextvars.copy_context())
            try:
                self._event_queue.put_nowait(item)
            except queue.Full:
                logger.warning(budget_msg, _EVENT_PENDING_CAP, event)
                return 0
            self._event_pending_by_generation[generation] = pending + 1
            self._ensure_event_worker_locked()
            return len(subscriptions)

    def has_hook(self, hook_name: str) -> bool:
        """Return True when at least one callback is registered for a hook."""
        return bool(self.iter_hook_callbacks(hook_name))

    async def ainvoke_hook(self, hook_name: str, **kwargs: Any) -> List[Any]:
        """:meth:`invoke_hook` for callers that are already on an event loop.

        Same payload narrowing, per-callback isolation and result contract. The difference is
        where an ``async def`` callback runs: here it is awaited on the caller's own loop, so a
        callback that awaits anything scheduled on that loop can make progress. Through the
        sync path it runs on a helper thread while the caller blocks in ``done.wait()`` — on the
        gateway that stalls the whole event loop for the callback's duration. Sync callbacks
        run inline only for unbounded hooks. Bounded callbacks share the sync path's quarantine;
        async cancellation never waits for plugin cleanup. The timing-out ``pre_tool_call`` fails closed.
        """
        from hermes_cli.plugins import _resolve_hook_callback_timeout
        if hook_name != "gateway_platform_event":
            kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
        results: List[Any] = []
        timeout = _resolve_hook_callback_timeout()
        use_timeout = _hook_uses_callback_timeout(hook_name, timeout)
        fail_closed = hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS
        for cb in self.iter_hook_callbacks(hook_name):
            if self._hook_callback_disabled(hook_name, cb):
                continue
            try:
                if use_timeout:
                    ret = await self._run_hook_callback_async(hook_name, cb, kwargs, timeout)
                    if ret is _HOOK_DISABLED:
                        continue
                    if ret is _HOOK_SKIPPED:
                        if fail_closed:
                            results.append({"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE})
                        continue
                else:
                    ret = cb(**self._hook_callback_kwargs(cb, kwargs))
                    if inspect.isawaitable(ret):
                        ret = await ret
                if ret is not None:
                    results.append(ret)
            except (Exception, SystemExit) as exc:
                # Same isolation + failure contract as the sync path (#111922 warn-once, #109624
                # a raising policy guard fails closed).
                self._report_hook_failure(hook_name, cb, kwargs, exc)
                if fail_closed:
                    results.append(_policy_error_block_directive(hook_name, cb, exc))
        return results

    def iter_hook_callbacks(self, hook_name: str) -> tuple[Callable, ...]:
        """Return a stable snapshot of callbacks registered for a hook."""
        hooks = self._hook_reload_snapshot
        return tuple((hooks if hooks is not None else self._hooks).get(hook_name, ()))

    def render_system_prompt_sections(
        self, session_info: Mapping[str, Any]
    ) -> List[RenderedPluginSystemPromptSection]:
        """Render all registered sections deterministically and fail open."""
        frozen_info = types.MappingProxyType(dict(session_info))
        rendered: List[RenderedPluginSystemPromptSection] = []
        total_chars = len(PLUGIN_SECTIONS_START) + len(PLUGIN_SECTIONS_END) + 2
        for _section_id, section in sorted(self._system_prompt_sections.items()):
            if len(rendered) >= MAX_SYSTEM_PROMPT_SECTIONS:
                logger.warning(
                    "Plugin system prompt section %s exceeded the section-count "
                    "budget (%d) and was skipped", section.id, MAX_SYSTEM_PROMPT_SECTIONS)
                continue
            text = self._render_prompt_section_text(section, frozen_info)
            if text is None:
                continue
            rendered_chars = len(format_system_prompt_section(section.id, text))
            if rendered:
                rendered_chars += 2  # canonical ``\n\n`` separator
            if total_chars + rendered_chars > MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS:
                logger.warning(
                    "Plugin system prompt section %s (%s) exceeded the aggregate "
                    "session budget (%d chars) and was skipped", section.id, section.plugin,
                    MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS)
                continue
            rendered.append(
                RenderedPluginSystemPromptSection(
                    id=section.id, content=text, position=section.position, plugin=section.plugin))
            total_chars += rendered_chars
            logger.info(
                "Session plugin prompt section: id=%s plugin=%s position=%s chars=%d", section.id,
                section.plugin, section.position, len(text))
        return rendered

    @staticmethod
    def _render_prompt_section_text(
        section: PluginSystemPromptSection, frozen_info: Mapping[str, Any]
    ) -> Optional[str]:
        """Evaluate one section; return its stripped text or None (with a warning) when skipped."""
        def _skip(detail: str, *args: Any) -> None:
            logger.warning(
                "Plugin system prompt section %s (%s) " + detail, section.id, section.plugin, *args)

        try:
            value = section.content(frozen_info) if callable(section.content) else section.content
        except (Exception, SystemExit) as exc:
            _skip("raised and was skipped: %s", exc)
            return None
        if not isinstance(value, str):
            _skip("returned %s, not str; skipped", type(value).__name__)
            return None
        text = value.strip()
        if not text:
            return None
        if PLUGIN_SECTIONS_START in text or PLUGIN_SECTIONS_END in text:
            _skip("contained a reserved persistence marker and was skipped")
            return None
        if len(text) > section.max_chars:
            _skip("exceeded max_chars (%d > %d) and was skipped", len(text), section.max_chars)
            return None
        return text

    def has_middleware(self, kind: str) -> bool:
        """Return True when at least one callback is registered for middleware."""
        return bool(self._middleware.get(kind))

    def invoke_middleware(self, kind: str, **kwargs: Any) -> List[Any]:
        """Call middleware callbacks for *kind* (each isolated); return non-``None`` results."""
        results: List[Any] = []
        for cb in self._middleware.get(kind, []):
            try:
                ret = cb(**kwargs)
                if ret is not None:
                    results.append(ret)
            except (Exception, SystemExit) as exc:
                # Runs once per tool call like a hook, so a mis-declared callback floods identically.
                self._report_hook_failure(kind, cb, kwargs, exc, surface="Middleware")
        return results
