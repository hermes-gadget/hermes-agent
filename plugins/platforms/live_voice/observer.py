"""One owner/profile observer shared by calls and connected adapters."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import time
import weakref

from .delivery import matrix_destination, send_text
from .fleet import FleetStatusProvider, FleetSnapshot
from .notifications import NotificationPolicy

logger = logging.getLogger(__name__)


class OwnerContext:
    def __init__(self, runner, home, config, db):
        self.runner, self.home, self.config, self.db = runner, Path(home).resolve(), config, db
        self.owner_ids = {p: str((config.get(p, {}).get("gpt_live") or {}).get("user_id") or "") for p in ("matrix", "discord")}
        self.owner_key = hashlib.sha256((str(self.home) + json.dumps(self.owner_ids, sort_keys=True)).encode()).hexdigest()
        self.session = SimpleNamespace(cfg=SimpleNamespace(assistant_role="personal"))
        self.observer = None

    def authorize(self):
        from hermes_constants import get_hermes_home
        if get_hermes_home().resolve() != self.home:
            raise ValueError("The observer must run in its owning profile scope.")

    def authorized_source(self, source):
        return (str(source.user_id or "") == self.owner_ids.get(source.platform.value)
                and bool(self.runner._is_user_authorized_for_source(source)))


class OwnerObserver:
    def __init__(self, context, *, managed_db=False):
        self.context = context
        self.fleet = FleetStatusProvider(context)
        self.adapters = set()
        self.calls = weakref.WeakSet()
        self.latest = None
        self._sample_lock = asyncio.Lock()
        self.task = None
        self.managed_db = managed_db
        self.loop = asyncio.get_running_loop()
        self._results = set()
        self.closed = False
        self._activity = {}

    def receive_child_result(self, home, session_id, summary, status):
        if self.closed:
            return
        task = asyncio.create_task(self.record_child_result(home, session_id, summary, status))
        self._results.add(task)
        def finished(done):
            self._results.discard(done)
            if not done.cancelled() and done.exception():
                logger.warning("Voice child result observation failed (%s)", type(done.exception()).__name__)
        task.add_done_callback(finished)

    async def record_child_result(self, home, session_id, summary, status):
        from gateway.run import _profile_runtime_scope
        from gateway.session import SessionSource
        c = self.context
        with _profile_runtime_scope(c.home):
            watches = await asyncio.to_thread(c.db.voice_records, "attachment", c.owner_key)
            for watch in watches:
                p = watch["payload"]
                if (watch["status"] != "watching" or p.get("child_session_id") != session_id
                        or p.get("target_home") != home or not c.authorized_source(SessionSource.from_dict(p["source"]))):
                    continue
                result = str(summary or f"Child finished with status {status}; inspect its owning conversation.")
                row = {"agent_id": p["agent_id"], "project": p["project"], "platform": p["source"]["platform"],
                       "monitor_url": p["monitor_url"], "attachment_id": watch["id"]}
                await self.enqueue("attached-result:" + watch["id"], "completion", row, p["project"] + ": " + result[:900])
                await asyncio.to_thread(c.db.voice_change_record, "attachment", watch["id"], c.owner_key,
                    {"watching"}, "reported", {**p, "result": result[:12000], "child_status": status})

    async def sample(self):
        async with self._sample_lock:
            if self.latest is None or time.time() - self.latest.observed_at > 2:
                self.latest = await self.fleet.snapshot()
            return self.latest

    async def snapshot_for(self, assistant):
        assistant.authorize()
        snapshot = await self.sample()
        rows = []
        for row in snapshot.agents:
            data = dict(row)
            if assistant.session.cfg.assistant_role == "project" and data["platform"] == "matrix":
                data.update(project="Personal assistant", objective="", activity="", workspace="", last_result="")
            rows.append(data)
        return FleetSnapshot(rows, snapshot.observed_at, snapshot.coverage)

    async def enqueue(self, ident, kind, row, text):
        c = self.context
        existing = await asyncio.to_thread(c.db.voice_record, "notice", ident, c.owner_key)
        if existing:
            return existing
        row = {k: row[k] for k in ("agent_id", "project", "platform", "monitor_url", "request_id", "attachment_id") if k in row}
        payload = {"kind": kind, "agent": row, "text": text}
        return await asyncio.to_thread(c.db.voice_create_record, "notice", ident, c.owner_key, payload, status="queued")

    async def project_result(self, request_id, payload):
        row = {"agent_id": "request:" + request_id, "project": payload["project"],
               "platform": "discord", "monitor_url": payload["monitor_url"], "request_id": request_id}
        await self.enqueue("result:" + request_id, "completion", row,
            payload["project"] + ": " + payload["result"][:900])

    async def record_result(self, adapter, event, outcome):
        c = self.context
        if not c.authorized_source(event.source):
            return
        from .artifacts import capture_artifacts
        request_id = event.metadata.get("voice_request_id")
        if request_id:
            receipts = await asyncio.to_thread(c.db.voice_dispatches, c.owner_key, 100)
            receipt = next((r for r in receipts if r["request_id"] == request_id), None)
            if receipt is not None:
                await capture_artifacts(c, adapter, event, request_id, receipt["payload"]["monitor_url"])
                from .fulfillment import complete_handoffs
                await complete_handoffs(c, request_id)
            return
        generation = getattr(event, "_execution_generation", None)
        if generation is None:
            return
        key = adapter._source_session_key(event.source)
        watches = await asyncio.to_thread(c.db.voice_records, "attachment", c.owner_key)
        for watch in watches:
            p = watch["payload"]
            if (watch["status"] != "watching" or p["session_key"] != key or p["generation"] != generation
                    or p.get("instance") != getattr(c.runner, "_live_voice_instance", None) or p.get("child_id")):
                continue
            result = getattr(event, "_completed_response", "") or getattr(event, "_streamed_final_response", "")
            if not result:
                result = f"The agent finished its turn ({getattr(outcome, 'value', outcome)}); inspect its conversation."
            row = {"agent_id": p["agent_id"], "project": p["project"], "platform": event.source.platform.value,
                   "monitor_url": p["monitor_url"], "attachment_id": watch["id"]}
            await self.enqueue("attached-result:" + watch["id"], "completion", row, p["project"] + ": " + result[:900])
            await capture_artifacts(c, adapter, event, watch["id"], p["monitor_url"])
            await asyncio.to_thread(c.db.voice_change_record, "attachment", watch["id"], c.owner_key,
                {"watching"}, "reported", {**p, "result": result[:12000]})

    def _pending(self, row):
        if not row["agent_id"].startswith("gateway:"):
            return None, ""
        from tools.clarify_gateway import get_pending_for_session
        key = row["agent_id"].removeprefix("gateway:")
        pending = get_pending_for_session(key, include_choice_prompts=True)
        if pending is not None and not pending.event.is_set():
            return pending, pending.clarify_id
        state = self.context.runner._peek_session_state(key)
        approvals = getattr(getattr(state, "persistent", None), "approvals", None)
        return None, hashlib.sha256(json.dumps(approvals, sort_keys=True, default=str).encode()).hexdigest() if approvals else ""

    async def tick(self):
        c, now = self.context, time.time()
        snapshot = await self.sample()
        waiting = {}
        for row in snapshot.agents:
            if not row["needs_input"]:
                continue
            pending, request = self._pending(row)
            if not request:
                continue
            key = row["agent_id"].removeprefix("gateway:")
            state = c.runner._peek_session_state(key)
            generation = state.persistent.run_generation if state else -1
            ident = hashlib.sha256(f"{row['agent_id']}:{generation}:{request}".encode()).hexdigest()
            waiting[ident] = (row, pending)
            await self.enqueue(ident, "input", row, row["project"] + " needs your input. " + (
                pending.question if pending else "An approval is waiting in its conversation.") + "\n" + row["monitor_url"])
        policy = NotificationPolicy(c.config)
        await self._enqueue_milestones(snapshot, policy, now)
        calls = [a for a in self.calls if a.session.is_live and not a.session.close_requested]
        notices = await asyncio.to_thread(c.db.voice_records, "notice", c.owner_key, limit=500)
        for notice in reversed(notices):
            if notice["status"] != "queued":
                continue
            p = notice["payload"]
            if p["kind"] == "input" and notice["id"] not in waiting:
                await asyncio.to_thread(c.db.voice_change_record, "notice", notice["id"], c.owner_key, {"queued"}, "resolved")
                continue
            if not policy.allows(p["kind"], now):
                continue
            call = next((a for a in calls if not a.session._recovering and (
                time.monotonic() - a.session._last_user_audio_at >= 3)
                and not getattr(a.session.buffer, "active", False)), None)
            if calls and call is None:
                continue
            if call:
                if p["kind"] == "completion":
                    group = [r for r in notices if r["status"] == "queued" and r["payload"]["kind"] == "completion"][:3]
                    if len(group) > 1:
                        await call.quiet("Verified grouped results (data): " + json.dumps([r["payload"] for r in group
                            if call.session.cfg.assistant_role != "project" or r["payload"]["agent"].get("platform") != "matrix"]))
                        labels = [r["payload"]["agent"]["project"] if call.session.cfg.assistant_role != "project"
                            or r["payload"]["agent"].get("platform") != "matrix" else "your personal assistant" for r in group]
                        await self._deliver_group(group, lambda: call.session._commentary(None,
                            "Results are ready for " + ", ".join(labels) + ". The details are in their conversations."))
                        break
                text = p["text"]
                personal = p["agent"].get("platform") == "matrix" and call.session.cfg.assistant_role == "project"
                if personal:
                    text = "Your personal assistant needs your input in Matrix." if p["kind"] == "input" else "Your personal assistant has an update in Matrix."
                elif p["kind"] == "input" and (pending := waiting[notice["id"]][1]) is not None:
                    existing = [r for r in await call.interactions.snapshot() if (r.get("native") or {}).get("clarify_id") == pending.clarify_id]
                    if not existing:
                        attached = await call.jobs.attach({"agent_id": p["agent"]["agent_id"], "_register_question": False}, "clarify:" + pending.clarify_id)
                        foreground = any(r["status"] == "foreground" for r in call.interactions.cached)
                        await call.interactions.prompt({"question": pending.question, "choices": pending.choices,
                            "native": {"attachment_id": attached["attachment_id"], "clarify_id": pending.clarify_id}},
                            "clarify:" + pending.clarify_id, background=foreground)
                        if foreground:
                            text = p["agent"]["project"] + " has another question waiting in its project conversation."
                await self._deliver_notice(notice, lambda: call.session._commentary(None, text))
                break  # One notice per observation cycle; keep the conversation available.
            elif p["kind"] == "input":
                try:
                    source = matrix_destination(c)
                except ValueError:
                    continue  # Keep queued until one verified personal DM is configured.
                await self._deliver_notice(notice, lambda: send_text(c, source, p["text"]))
            else:
                # Native project delivery already carries off-call results.
                await asyncio.to_thread(c.db.voice_change_record, "notice", notice["id"], c.owner_key, {"queued"}, "available")
        from .fulfillment import complete_handoffs
        deliveries = await asyncio.to_thread(c.db.voice_records, "delivery", c.owner_key, limit=500)
        for delivery in deliveries:
            if delivery["status"] == "awaiting_artifact" and delivery["payload"].get("followup"):
                await complete_handoffs(c, delivery["payload"]["followup"]["request_id"])

    async def _enqueue_milestones(self, snapshot, policy, now):
        c = self.context
        if policy.mode(now) == "milestones":
            for row in snapshot.agents:
                if row["source"] != "gateway" or row["state"] != "running" or not row["activity"]:
                    continue
                prior, announced = self._activity.get(row["agent_id"], ("", 0))
                if row["activity"] != prior and now - announced >= 120:
                    key = row["agent_id"].removeprefix("gateway:")
                    state = c.runner._peek_session_state(key)
                    generation = state.persistent.run_generation if state else -1
                    ident = hashlib.sha256(f"{key}:{generation}:{row['activity']}".encode()).hexdigest()
                    await self.enqueue("activity:" + ident, "milestone", row,
                        row["project"] + " activity: " + row["activity"])
                    self._activity[row["agent_id"]] = (row["activity"], now)

    async def _deliver_notice(self, notice, deliver):
        await self._deliver_group([notice], deliver)

    async def _deliver_group(self, notices, deliver):
        c = self.context
        claimed = []
        for notice in notices:
            claim = await asyncio.to_thread(c.db.voice_change_record, "notice", notice["id"], c.owner_key, {"queued"}, "delivering")
            if claim is not None:
                claimed.append(notice)
        if not claimed:
            return
        try:
            await deliver()
            for notice in claimed:
                await asyncio.to_thread(c.db.voice_change_record, "notice", notice["id"], c.owner_key, {"delivering"}, "delivered")
        except Exception:
            for notice in claimed:
                await asyncio.to_thread(c.db.voice_change_record, "notice", notice["id"], c.owner_key, {"delivering"}, "uncertain")
            raise

    async def run(self):
        from gateway.run import _profile_runtime_scope
        from hermes_cli.config import read_raw_config
        while True:
            try:
                with _profile_runtime_scope(self.context.home):
                    self.context.config = await asyncio.to_thread(read_raw_config) or {}
                    if any((self.context.config.get(p, {}).get("gpt_live") or {}).get("assistant_enabled") for p in ("matrix", "discord")):
                        await self.tick()
            except Exception as exc:  # health: allow BLE001 -- SDK/callback boundary records sanitized failure and preserves recovery; exception reprs can expose auth or private content
                logger.warning("Live assistant observation unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(10)

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.gather(*self._results, return_exceptions=True)
        if self.managed_db:
            from hermes_state_registry import release
            await asyncio.to_thread(release, self.context.db)
            self.managed_db = False


async def start_observer(adapter):
    from hermes_cli.config import read_raw_config
    from hermes_constants import get_hermes_home
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        return None
    config, home = await asyncio.to_thread(read_raw_config) or {}, get_hermes_home().resolve()
    if not any((config.get(p, {}).get("gpt_live") or {}).get("assistant_enabled") for p in ("matrix", "discord")):
        return None
    if not hasattr(runner, "_live_voice_observers"):
        runner._live_voice_observers = {}
        runner._live_voice_observer_lock = asyncio.Lock()
    async with runner._live_voice_observer_lock:
        observer = runner._live_voice_observers.get(str(home))
        if observer is None:
            from hermes_state_registry import acquire, release
            opening = asyncio.create_task(asyncio.to_thread(acquire, home / "state.db"))
            try:
                db = await asyncio.shield(opening)
            except asyncio.CancelledError:
                db = await opening
                await asyncio.to_thread(release, db)
                raise
            observer = OwnerObserver(OwnerContext(runner, home, config, db), managed_db=True)
            runner._live_voice_observers[str(home)] = observer
            observer.task = asyncio.create_task(observer.run())
        observer.adapters.add(adapter)
        return observer


async def stop_observer(adapter):
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        return
    if not hasattr(runner, "_live_voice_observer_lock"):
        return
    async with runner._live_voice_observer_lock:
        for home, observer in list(runner._live_voice_observers.items()):
            observer.adapters.discard(adapter)
            if not observer.adapters:
                await observer.close()
                runner._live_voice_observers.pop(home, None)


def observe_native_child(child_session_id=None, child_summary=None, child_status=None, **kwargs):
    """The native lifecycle supplies a verified result, including for synchronous children."""
    if not child_session_id:
        return
    from gateway.run import _gateway_runner_ref
    from hermes_constants import get_hermes_home
    runner = _gateway_runner_ref()
    home = str(get_hermes_home().resolve())
    for observer in list(getattr(runner, "_live_voice_observers", {}).values()):
        observer.loop.call_soon_threadsafe(observer.receive_child_result,
            home, child_session_id, child_summary, child_status)
