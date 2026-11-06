"""Read-only fleet projection: live gateway state plus an optional operations feed."""
import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import math
import time
import urllib.request

from .agent_status import _label
from .projects import discord_link

logger = logging.getLogger(__name__)


def _activity_summary(agent):
    try:
        summary = agent.get_activity_summary() if callable(getattr(agent, "get_activity_summary", None)) else {}
    except Exception as exc:  # health: allow BLE001 -- a foreign sampler cannot hide other agents; exception text may include private activity
        logger.debug("Voice activity sampling failed (%s)", type(exc).__name__)
        summary = {}  # Foreign/initializing samplers must not hide other agents.
    return summary


@dataclass
class AgentStatus:
    agent_id: str
    parent_id: str | None
    project: str
    profile: str
    session_id: str
    platform: str
    channel_id: str
    workspace: str
    objective: str
    state: str
    activity: str
    observed_at: float
    source: str
    monitor_url: str = ""
    last_result: str = ""
    needs_input: bool = False
    freshness: str = "current"
    activity_at: float | None = None
    controls: tuple = ()


@dataclass
class FleetSnapshot:
    agents: list
    observed_at: float
    coverage: dict

    def as_dict(self):
        return asdict(self)

    def quiet_summary(self):
        active = [r for r in self.agents if r["state"] in {"running", "starting", "queued", "waiting_for_input"}
                  and r["freshness"] == "current"]
        examples = [{"project": _label(r["project"], 40), "state": r["state"]} for r in active[:3]]
        summary = {"active": len(active), "needs_input": sum(r["needs_input"] for r in active),
                   "examples": examples, "coverage": {k:v for k,v in self.coverage.items() if isinstance(v, str)}}
        while len(json.dumps(summary, ensure_ascii=False).encode()) > 390 and summary["examples"]:
            summary["examples"].pop()
        return "Current fleet facts; silent context, not a request to speak: " + json.dumps(summary, ensure_ascii=False)


class FleetStatusProvider:
    def __init__(self, assistant):
        self.assistant = assistant
        self._last = None
        self._external = None
        self._external_task = None
        self._external_at = 0
        self._external_error = False

    async def snapshot(self):
        service, now = self.assistant, time.time()
        if getattr(service, "observer", None) is not None:
            return await service.observer.snapshot_for(service)
        service.authorize()
        runner = service.runner
        store = getattr(runner, "session_store", None)
        entries = dict(getattr(store, "_entries", {}))
        states = dict(getattr(runner, "_sessions", {}))
        rows, roots, channel_agents = [], [], {}
        for key in sorted(set(entries) | set(states)):
            entry, state = entries.get(key), states.get(key)
            event = getattr(getattr(state, "turn", None), "event", None)
            restored = getattr(runner, "_restored_source", None)
            source = getattr(event, "source", None) or (restored(entry) if entry is not None and callable(restored) else getattr(entry, "origin", None))
            if source is None or not service.authorized_source(source):
                continue
            sid = getattr(entry, "session_id", "") or ""
            agent = getattr(getattr(state, "turn", None), "agent", None)
            platform = source.platform.value
            summary = _activity_summary(agent)
            activity = summary.get("description", "")
            queue = getattr(getattr(state, "conversation", None), "queued_events", [])
            pending = getattr(getattr(state, "persistent", None), "approvals", None)
            from tools.clarify_gateway import get_pending_for_session
            pending = pending or get_pending_for_session(key, include_choice_prompts=True)
            status = "waiting_for_input" if pending else "running" if agent is not None else "queued" if queue else "idle"
            if status == "running" and not callable(getattr(agent, "get_activity_summary", None)):
                status = "starting"
            ident = f"gateway:{key}"
            row = AgentStatus(ident, None, _label(source.chat_name or source.chat_id), source.profile or "default",
                str(sid), platform, str(source.chat_id), _label(getattr(agent, "cwd", ""), 240),
                _label(getattr(event, "text", "")), status, _label(activity), now, "gateway",
                discord_link(source.guild_id, source.chat_id) if platform == "discord" and source.guild_id else "",
                needs_input=bool(pending), activity_at=summary.get("last_activity_at"))
            if agent is not None:
                row.controls = ("attach", "steer", "stop", "respond")
            cached = getattr(runner, "_agent_cache", {}).get(key)
            parent = agent if callable(getattr(agent, "get_activity_summary", None)) else (
                cached[0] if isinstance(cached, tuple) else None)
            db = getattr(parent, "_session_db", None)
            if db is not None and sid:
                details = await asyncio.to_thread(db.get_session, sid)
                if details:
                    row.workspace = _label(details.get("cwd"), 240) or row.workspace
                    row.activity = row.activity or _label(details.get("last_activity_description"))
                    row.activity_at = row.activity_at or details.get("last_activity_at")
                messages = await asyncio.to_thread(db.get_messages, sid, limit=1, latest=True)
                if messages and messages[0]["role"] == "assistant" and not messages[0].get("tool_calls"):
                    row.last_result = _label(messages[0].get("content"), 400)
            if service.session.cfg.assistant_role == "project" and platform == "matrix":
                row.project, row.objective, row.activity, row.workspace, row.last_result = "Personal assistant", "", "", "", ""
                row.controls = ()
            rows.append(row)
            if parent is not None:
                roots.append((parent, row))
            channel_agents[f"inst:{platform}:{source.chat_id}"] = row
        # Native ownership metadata covers children created outside voice too.
        from tools.delegate_tool import delegate_task
        for parent_agent, parent in roots:
            payload = json.loads(await asyncio.to_thread(delegate_task, action="list", parent_agent=parent_agent))
            for child in payload.get("subagents", []):
                ident = str(child["subagent_id"])
                child_row = AgentStatus(f"native:{ident}",
                    f"native:{child['parent_id']}" if child.get("parent_id") else parent.agent_id,
                    parent.project, parent.profile, "", parent.platform, parent.channel_id, "",
                    _label(child.get("goal")), "running", _label(child.get("current_tool") or child.get("last_tool")),
                    now, "native_worker", parent.monitor_url)
                child_row.controls = ("attach", "steer", "stop")
                if service.session.cfg.assistant_role == "project" and parent.platform == "matrix":
                    child_row.objective = child_row.activity = ""
                    child_row.controls = ()
                rows.append(child_row)
        coverage = {"gateway": "current", "native_workers": "current", "operations_feed": "not_configured"}
        await self._append_external_rows(rows, channel_agents, coverage, service, now)
        result = FleetSnapshot([asdict(r) for r in rows], now, coverage)
        self._last = result
        return result

    async def _append_external_rows(self, rows, channel_agents, coverage, service, now):
        url = (service.config.get("voice", {}).get("assistant") or {}).get("fleet_url", "")
        if url:
            if self._external_task is None and now - self._external_at >= 10:
                self._external_task = asyncio.create_task(asyncio.to_thread(self._read_external, url))
            if self._external_task is not None:
                try:
                    self._external = await asyncio.wait_for(asyncio.shield(self._external_task), 2.5)
                    self._external_at = now
                    self._external_task = None
                    self._external_error = False
                except TimeoutError:
                    if self._external_task.done():
                        self._external_at = now
                        self._external_task = None
                        self._external_error = True
                    coverage["operations_feed"] = "unavailable"
                except (OSError, ValueError):
                    self._external_at = now
                    self._external_task = None
                    self._external_error = True
                    coverage["operations_feed"] = "unavailable"
            if self._external:
                age = now - self._external.get("now", 0)
                coverage["operations_feed"] = "unavailable" if self._external_error else "current" if 0 <= age <= 30 else "stale"
                settings = service.config.get("voice", {}).get("assistant") or {}
                owners = settings.get("fleet_owners", [])
                owners = owners if isinstance(owners, list) and all(isinstance(v, str) for v in owners) else []
                external = self._external_rows(self._external, channel_agents, now, owners, self._external_error)
                unattributed = max(0, len(self._external.get("workers", [])) - len(external))
                native_ids = {r.agent_id.removeprefix("native:") for r in rows if r.source == "native_worker"}
                external = [r for r in external if r.agent_id.removeprefix("operations:w:sub:") not in native_ids]
                rows.extend(external)
                coverage["unattributed_workers"] = unattributed

    @staticmethod
    def _read_external(url):
        with urllib.request.urlopen(url, timeout=2) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("operations feed exceeds the response limit")
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("ok") is False
                or type(payload.get("now")) not in (int, float) or not math.isfinite(payload["now"])):
            raise ValueError("operations feed has no observation time")
        return payload

    @staticmethod
    def _external_rows(payload, parents, now, allowed_owners=(), force_stale=False):
        rows = []
        for child in payload.get("workers", []):
            parent = parents.get(str(child.get("parent_ref") or ""))
            meta = child.get("meta") or {}
            if parent is None:
                if meta.get("owner") not in allowed_owners:
                    continue  # Only operator-configured owner IDs may include unparented workers.
                parent = AgentStatus(str(child.get("parent_ref") or "external"), None,
                    "Coding workspace", "external", str(meta.get("deck_id") or ""),
                    "coding_worker", "", _label(meta.get("cwd"), 240), "", "unknown", "", payload["now"], "operations_feed")
            age = child.get("last_age_s")
            seen = payload["now"] - age if type(age) in (int, float) and math.isfinite(age) and age >= 0 else 0
            stale = force_stale or not seen or now - seen > 30 or now < seen
            state = {"busy": "running", "active": "running", "done": "completed",
                     "failed": "failed", "waiting": "waiting_for_input", "cancelled": "cancelled"}.get(child.get("state"), "unknown")
            if meta.get("deck_status") == "notLoaded":
                state = "unknown"
            row = AgentStatus("operations:" + str(child.get("wid")), parent.agent_id,
                parent.project, parent.profile, parent.session_id, parent.platform, parent.channel_id, parent.workspace,
                _label(child.get("objective") or child.get("label")), "unknown" if stale else state,
                _label(child.get("summary")), seen, "operations_feed", parent.monitor_url,
                freshness="stale" if stale else "current")
            if parent.platform == "matrix" and parent.project == "Personal assistant":
                row.objective = row.activity = row.workspace = ""
            rows.append(row)
        return rows

    async def run(self):
        previous = None
        announced = set()
        while self.assistant.session.is_live:
            try:
                result = await self.snapshot()
                self.assistant.session.request_progress.observe_fleet(result)
                snapshot = result.as_dict()
                # Idle conversation history, old results and ticking timestamps belong in
                # on-request tools, not in a repeated stream to the speaking model.
                serialized = result.quiet_summary()
                if serialized != previous:
                    await self.assistant.quiet(serialized)
                    previous = serialized
                waiting = {r["agent_id"] for r in snapshot["agents"] if r["needs_input"]}
                new_waiting = [r for r in snapshot["agents"] if r["agent_id"] in waiting - announced]
                if (new_waiting and getattr(self.assistant, "observer", None) is None
                        and self.assistant.session.loop.time() - self.assistant.session._last_transcript_at >= 3):
                    labels = ", ".join(r["project"] for r in new_waiting[:3])
                    await self.assistant.session._commentary(None, f"{labels} needs your input in its project conversation.")
                    announced |= {r["agent_id"] for r in new_waiting}
                announced &= waiting
            except Exception as exc:  # health: allow BLE001 -- optional observation must remain isolated; log only type to protect private session/provider details
                logger.debug("Fleet observation failed (%s)", type(exc).__name__)
                if previous != "unavailable":
                    await self.assistant.quiet("Fleet observation unavailable. Prior agent states may be stale; do not guess.")
                    previous = "unavailable"
            await asyncio.sleep(10)
