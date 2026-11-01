"""Bounded spoken progress for admitted requests, independent of their final replies."""
from dataclasses import dataclass

from .agent_status import _label


@dataclass
class RequestStatus:
    started: float
    acknowledged: bool = False
    delayed: bool = False
    latest: str = ""
    spoken: str = ""
    last_update: float = 0


class RequestProgress:
    ACK_SECONDS = 2
    UPDATE_SECONDS = 15
    DELAY_SECONDS = 30
    WORKER_SECONDS = 45

    def __init__(self, session):
        self.session = session
        self.requests = {}
        self.last_spoken = float("-inf")
        self.workers = {}
        self.worker_spoken = {}

    def begin(self, did):
        self.requests[did] = RequestStatus(self.session.loop.time())

    def end(self, did):
        self.requests.pop(did, None)

    def offer(self, did, content):
        from .coordination import is_conversational_hold
        if did in self.requests and not is_conversational_hold(content):
            self.requests[did].latest = _label(content, 320)

    def active(self, did):
        pending = self.session.pending.get(did)
        return bool(did in self.requests and pending and not pending.future.done()
                    and did not in self.session._backend_completed)

    def project_roots(self):
        assistant = self.session.assistant
        roots = {}
        for receipt in assistant.progress_requests.values():
            state = assistant.runner._peek_session_state(receipt["session_key"])
            event = getattr(getattr(state, "turn", None), "event", None)
            if event is not None and event.metadata.get("voice_request_id") == receipt["request_id"]:
                roots["gateway:" + receipt["session_key"]] = receipt.get("session_id")
        return roots

    def observe_fleet(self, snapshot):
        session = self.session
        roots = {"gateway:" + session.backend_session_key} if session.backend_session_key else set()
        project_sessions = self.project_roots()
        roots.update(project_sessions)
        rows = {r["agent_id"]: r for r in snapshot.agents if r["freshness"] == "current"
                and r["state"] == "running" and not r["needs_input"]}
        for ident, sid in project_sessions.items():
            if ident in rows and rows[ident]["session_id"] != sid:
                roots.discard(ident)  # A later execution in that channel is not our request.
        owned = set(roots)
        while children := {r["agent_id"] for r in rows.values() if r["parent_id"] in owned} - owned:
            owned.update(children)
        # The coordinator's wait is reported separately. Never narrate unrelated fleet work.
        self.workers = {ident: r for ident, r in rows.items() if ident in owned
                        and (ident in project_sessions or r["source"] == "native_worker")}
        self.worker_spoken = {ident: value for ident, value in self.worker_spoken.items() if ident in self.workers}
        now = session.loop.time()
        for ident in self.workers:
            self.worker_spoken.setdefault(ident, ("", now))

    def quiet(self, now):
        s = self.session
        return (s.is_live and not s._recovering and now - s._last_user_audio_at >= .8
                and not getattr(s.buffer, "active", False)
                and s.turn.reason != "hold"
                and not (s.turn.open and s.turn.last_voiced is None))

    async def tick(self, now=None):
        s = self.session
        now = s.loop.time() if now is None else now
        if not self.quiet(now) or now - self.last_spoken < 3:
            return
        for did, row in list(self.requests.items()):
            if not self.active(did):
                continue
            fresh = row.latest and row.latest != row.spoken
            if fresh and (not row.acknowledged or now - row.last_update >= self.UPDATE_SECONDS):
                content = "Progress on this request: " + row.latest
            elif not row.acknowledged and now - row.started >= self.ACK_SECONDS:
                content = "Got it — I'm getting an answer to that. You can keep talking while I do."
            elif row.acknowledged and not row.delayed and now - row.last_update >= self.DELAY_SECONDS:
                content = "I'm still waiting for the answer to that request. No confirmed result yet."
            else:
                continue
            through = s._transcript_seq
            sent = await s._commentary(did, content, progress=True,
                guard=lambda: self.active(did) and s._transcript_seq == through and self.quiet(now))
            if sent:
                if row.acknowledged and not fresh:
                    row.delayed = True
                row.acknowledged, row.spoken, row.last_update = True, row.latest, now
                self.last_spoken = now
                s.spoken_progress_sent += 1
            return
        if now - self.last_spoken < self.WORKER_SECONDS:
            return
        for ident, row in self.workers.items():
            activity = _label(row["activity"], 180)
            previous, at = self.worker_spoken[ident]
            if not activity or activity == previous or now - at < self.WORKER_SECONDS:
                continue
            through = s._transcript_seq
            content = f"Latest observed work on {row['project']}: {activity}. The final report hasn't arrived yet."
            sent = await s._commentary(None, content, progress=True,
                guard=lambda: self.workers.get(ident) is row and s._transcript_seq == through and self.quiet(now)
                    and (row["source"] != "gateway" or self.project_roots().get(ident) == row["session_id"]))
            if sent:
                self.worker_spoken[ident] = (activity, now)
                self.last_spoken = now
                s.spoken_progress_sent += 1
            return
