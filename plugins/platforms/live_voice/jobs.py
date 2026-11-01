"""Attach to exact native executions without granting authority to saved IDs."""
import asyncio
from dataclasses import dataclass, asdict
import hashlib
import json
import uuid

from gateway.control_target import ExecutionTarget
from gateway.platforms.event import MessageEvent, MessageType
from gateway.wake import admit_internal_event


@dataclass(frozen=True)
class VoiceJobAttachment:
    attachment_id: str
    agent_id: str
    project: str
    session_key: str
    session_id: str
    generation: int
    monitor_url: str


class JobAttachments:
    def __init__(self, assistant):
        self.assistant = assistant
        self.targets = {}

    def _roots(self):
        a = self.assistant
        entries = getattr(a.runner.session_store, "_entries", {})
        for key in set(entries) | set(a.runner._sessions):
            state = a.runner._peek_session_state(key)
            event = getattr(getattr(state, "turn", None), "event", None)
            entry = entries.get(key)
            source = getattr(event, "source", None) or (
                a.runner._restored_source(entry) if entry and callable(getattr(a.runner, "_restored_source", None))
                else getattr(entry, "origin", None))
            if source is None or not a.authorized_source(source):
                continue
            cached = getattr(a.runner, "_agent_cache", {}).get(key)
            parent = getattr(getattr(state, "turn", None), "agent", None)
            if parent is None and isinstance(cached, tuple):
                parent = cached[0]
            if parent is not None:
                yield key, source, state, parent

    async def locate(self, agent_id):
        from tools.delegate_tool import delegate_task
        for key, source, state, parent in self._roots():
            if agent_id == "gateway:" + key and state and state.turn.agent is parent:
                return key, source, state, parent, None
            if agent_id.startswith("native:"):
                with self.assistant.runner._profile_scope_for_source(source):
                    result = json.loads(await asyncio.to_thread(delegate_task, action="list", parent_agent=parent))
                child = next((c for c in result.get("subagents", []) if "native:" + str(c["subagent_id"]) == agent_id), None)
                if child:
                    return key, source, state, parent, child
        raise ValueError("No current authorized native Hermes job matches that agent. External agents support status only.")

    async def attach(self, args, did):
        a = self.assistant
        if a.db is None:
            raise ValueError("Durable job attachments are unavailable.")
        rows = (await a.fleet.snapshot()).agents
        token = str(args.get("agent_id") or args.get("project") or "").casefold()
        exact = [r for r in rows if r["agent_id"].casefold() == token]
        matches = exact or [r for r in rows if token and token in r["project"].casefold()
                            and r["state"] in {"running", "waiting_for_input"}]
        if len(matches) != 1:
            raise ValueError("Choose one current agent ID from agents; the job is missing or ambiguous.")
        row = matches[0]
        if a.session.cfg.assistant_role == "project" and row["platform"] == "matrix":
            raise ValueError("Use the personal Matrix conversation to control personal assistant work.")
        key, source, state, parent, child = await self.locate(row["agent_id"])
        child_session_id = child.get("child_session_id", "") if child else ""
        child = child["subagent_id"] if child else None
        adapter = a.runner._delivery_adapter_for(source)
        store = getattr(adapter, "_session_store", None)
        sid = str(store.peek_session_id(key) or "") if store else ""
        generation = state.persistent.run_generation if state else -1
        target = None if child else ExecutionTarget(key, sid, generation, parent)
        if target:
            target.validate(a.runner, source)
        ident = hashlib.sha256(f"{a.owner_key}:{did}:{row['agent_id']}:{generation}".encode()).hexdigest()
        attachment = VoiceJobAttachment(ident, row["agent_id"], row["project"], key, sid, generation, row["monitor_url"])
        # Saved records describe a job; only this live source/object pair can control it.
        record = await asyncio.to_thread(a.db.voice_create_record, "attachment", ident, a.owner_key,
            {**asdict(attachment), "instance": self.instance(), "child_id": child,
             "child_session_id": child_session_id, "source": source.to_dict(),
             "target_home": str(adapter._canonicalize(source).runtime_home)}, status="watching")
        if record["status"] == "detached":
            await asyncio.to_thread(a.db.voice_change_record, "attachment", ident, a.owner_key, {"detached"}, "watching")
        self.targets[ident] = (source, target, parent, child)
        result = {**asdict(attachment), "status": "attached", "controls": ["steer", "stop", "respond"]}
        if not child and args.get("_register_question", True):
            from tools.clarify_gateway import get_pending_for_session
            question = get_pending_for_session(key, include_choice_prompts=True)
            if question is not None and not question.event.is_set():
                result["pending_question"] = await a.interactions.prompt({"question": question.question, "choices": question.choices,
                    "native": {"attachment_id": ident, "clarify_id": question.clarify_id}}, "attach-question:" + ident)
        return result

    def instance(self):
        runner = self.assistant.runner
        if not getattr(runner, "_live_voice_instance", None):
            runner._live_voice_instance = uuid.uuid4().hex
        return runner._live_voice_instance

    async def records(self):
        a = self.assistant
        if a.db is None:
            return []
        return await asyncio.to_thread(a.db.voice_records, "attachment", a.owner_key)

    async def detach(self, args, did):
        a, ident = self.assistant, str(args.get("attachment_id") or "")
        row = await asyncio.to_thread(a.db.voice_change_record, "attachment", ident, a.owner_key,
            {"watching"}, "detached") if a.db else None
        self.targets.pop(ident, None)
        if row is None:
            raise ValueError("Choose a watched attachment from agents.")
        return {"attachment_id": ident, "status": "detached"}

    def authority(self, ident):
        a = self.assistant
        target = self.targets.get(ident)
        if target is None:
            raise ValueError("Attach the current execution before controlling it.")
        source, guard, parent, child = target
        if not a.authorized_source(source):
            raise ValueError("The job is no longer authorized.")
        if guard:
            guard.validate(a.runner, source)
        return target

    async def control(self, args, action, did):
        a, ident = self.assistant, str(args.get("attachment_id") or "")
        source, guard, parent, child = self.authority(ident)
        text = str(args.get("task") or "").strip()
        if action == "steer" and (not text or len(text) > 16000):
            raise ValueError("Provide a correction between 1 and 16000 characters.")
        request = hashlib.sha256(f"{a.owner_key}:{ident}:{did}:{action}".encode()).hexdigest()
        payload = {"attachment_id": ident, "action": action, "task": text}
        claim = await asyncio.to_thread(a.db.voice_create_record, "control", request, a.owner_key, payload)
        if not claim["claimed"]:
            return {"request_id": request, "status": claim["status"], **claim["payload"]}
        from .session import DELEGATION_CONTEXT
        from .projects import PROJECT_CONTEXT
        token, project_token = DELEGATION_CONTEXT.set(None), PROJECT_CONTEXT.set(None)
        try:
            self.authority(ident)
            with a.runner._profile_scope_for_source(source):
                if child:
                    from tools.delegate_tool import delegate_task
                    outcome = json.loads(await asyncio.to_thread(delegate_task, action=action,
                        subagent_id=child, message=text, parent_agent=parent))
                    if "error" in outcome:
                        raise ValueError(str(outcome["error"]))
                else:
                    event = MessageEvent(text="/steer " + text if action == "steer" else "/stop",
                        source=source, message_type=MessageType.COMMAND)
                    event._execution_target = guard
                    await admit_internal_event(a.runner._delivery_adapter_for(source), event)
                    outcome = {"status": action + "_requested"}
            result = {**payload, "outcome": outcome}
            await asyncio.to_thread(a.db.voice_change_record, "control", request, a.owner_key, {"pending"}, "requested", result)
            return {"request_id": request, "status": "requested", **result}
        except Exception:
            await asyncio.to_thread(a.db.voice_change_record, "control", request, a.owner_key, {"pending"}, "uncertain")
            raise
        finally:
            PROJECT_CONTEXT.reset(project_token)
            DELEGATION_CONTEXT.reset(token)

    async def answer_clarify(self, native, answer):
        from tools.clarify_gateway import get_pending_for_session, respond_to_id, TEXT_RESOLVED
        if native["attachment_id"] not in self.targets:
            a = self.assistant
            saved = await asyncio.to_thread(a.db.voice_record, "attachment", native["attachment_id"], a.owner_key)
            if saved is None or saved["payload"].get("instance") != self.instance():
                raise ValueError("The question's execution changed; attach its current conversation.")
            restored = await self.attach({"agent_id": saved["payload"]["agent_id"], "_register_question": False}, "restore:" + native["clarify_id"])
            if any(restored[k] != saved["payload"][k] for k in ("session_id", "generation")):
                raise ValueError("The question's execution changed; inspect its current question.")
            self.targets[native["attachment_id"]] = self.targets[restored["attachment_id"]]
        source, guard, parent, child = self.authority(native["attachment_id"])
        if child:
            raise ValueError("Answer this clarification through its owning project conversation.")
        entry = get_pending_for_session(guard.session_key, include_choice_prompts=True)
        if entry is None or entry.clarify_id != native["clarify_id"]:
            raise ValueError("That clarification is no longer pending.")
        with self.assistant.runner._profile_scope_for_source(source):
            outcome = respond_to_id(entry.clarify_id, guard.session_key, answer)
        if outcome != TEXT_RESOLVED:
            raise ValueError("The clarification did not accept this answer; inspect its current choices.")
        return {"status": "answered", "clarify_id": entry.clarify_id}
