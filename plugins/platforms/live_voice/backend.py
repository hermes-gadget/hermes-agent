"""Normal gateway reply and commentary taps for live voice frontends."""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from .session import DELEGATION_CONTEXT
logger = logging.getLogger(__name__)

VOICE_TASK_CONTEXT = (
    "[Live voice task coordination]\n"
    "Ben wants independent work to run concurrently while the call stays responsive. "
    "This turn coordinates work: use the native delegate_task tool for ALL work that needs tools, "
    "lookups, research, memory access or actions. Top-level delegation runs in the background automatically. "
    "Do not do the work using terminal, file, search or other worker tools in the parent; the voice "
    "coordination policy blocks those calls here while preserving them for subagents. "
    "Split independent work into focused subagent tasks within the configured concurrency limit; "
    "keep dependent steps ordered and avoid concurrent edits to the same files. Simple conversation "
    "and restating verified results can be handled directly. After confirmed dispatch, name the jobs and end "
    "the turn promptly so Ben can ask for other work. Do not wait or poll for completion in this turn. "
    "Use delegate_task list/steer/stop for status or changes to running jobs. Preserve normal "
    "permissions and approval checks. Report completed work only when its result arrives, name "
    "the relevant job, and distinguish dispatch from completion."
)


@dataclass
class BackgroundReply:
    event: object
    delivered: bool = False


class LiveBackendMixin:
    async def start_live_assistant_observer(self):
        from .observer import start_observer
        return await start_observer(self)

    async def stop_live_assistant_observer(self):
        from .observer import stop_observer
        await stop_observer(self)

    def additional_toolsets_for_source(self, source):
        from .assistant_tools import register_tools
        extra = super().additional_toolsets_for_source(source)
        cfg = getattr(self, "_matrix_live_cfg", None) or getattr(self, "_gpt_live_cfg", None)
        if cfg and cfg.enabled and cfg.assistant_enabled and str(source.user_id or "") == cfg.user_id:
            voice_channel = getattr(cfg, "text_channel_id", "") or getattr(cfg, "voice_channel_id", "")
            if voice_channel and str(source.chat_id) != voice_channel:
                return extra
            register_tools()
            return [*extra, "live_voice"]
        return extra

    def _gpt_live_agent_roster(self, session, *, observe=False):
        # Recheck the current call, source, profile and authorization on every
        # read/publication; an inherited tool context is insufficient by itself.
        from .agent_status import AgentRoster
        source = session.backend_source
        runner = getattr(self, 'gateway_runner', None)
        authorized = getattr(runner, '_is_user_authorized_for_source', None)
        if ((not observe and (not session.is_live or getattr(self, '_gpt_live_sessions', {}).get(session.call_id) is not session))
                or source is None or str(source.chat_id) != session.chat_id
                or not session.cfg.user_id or str(source.user_id or '') != session.cfg.user_id
                or not callable(authorized) or not authorized(source)
                or not session.backend_session_key
                or self._source_session_key(source) != session.backend_session_key):
            return None
        identity = self._canonicalize(source)
        if identity is None or Path(identity.runtime_home).resolve() != session.home.resolve():
            return None
        rosters = getattr(self, '_gpt_live_agent_rosters', None)
        if rosters is None:
            rosters = self._gpt_live_agent_rosters = {}
        key = (str(session.home.resolve()), session.backend_session_key, session.cfg.user_id)
        if key not in rosters:
            if observe and getattr(self, '_gpt_live_sessions', {}).get(session.call_id) is not session:
                return None  # Do not revive an evicted conversation from a late hook.
            if len(rosters) >= 8:
                active = {(str(s.home.resolve()), s.backend_session_key, s.cfg.user_id)
                          for s in getattr(self, '_gpt_live_sessions', {}).values() if s.is_live}
                oldest = next((k for k in rosters if k not in active), None)
                if oldest is not None:
                    rosters.pop(oldest)
            rosters[key] = AgentRoster(session.home)
        roster = rosters[key]
        store = getattr(self, '_session_store', None)
        if store is not None:
            roster.validate_epoch(store.peek_session_id(session.backend_session_key))
        return roster

    def _gpt_live_active_for_chat(self, chat_id):
        return any(session.chat_id == str(chat_id)
                   for session in getattr(self, "_gpt_live_sessions", {}).values())

    def _gpt_live_tap_reply(self, chat_id, content, metadata):
        if not (content or "").strip() or not metadata or not metadata.get("notify") or metadata.get("_interim_send"):
            return
        from .projects import PROJECT_CONTEXT
        project = PROJECT_CONTEXT.get()
        if project and project[2] == str(chat_id):
            project[0].project_result(project[1], content)
        context = self._gpt_live_reply_context(chat_id)
        if context:
            session, did = context
            if isinstance(did, BackgroundReply):
                from gateway.warning_notifications import diagnostic_wake_muted
                if (not did.delivered and not diagnostic_wake_muted(did.event)
                        and self._gpt_live_background_session(did.event) is session):
                    did.delivered = True
                    session.feed_background_result(content)
            else:
                session.complete_pending(content, did)

    def _gpt_live_background_session(self, event):
        # Only the normal gateway's trusted completion turns may announce
        # conversation-owned background work. No inference from the latest task.
        if (not event.internal or event.metadata.get("notification_origin") != "process_registry_synthetic"):
            return None
        runner = getattr(self, "gateway_runner", None)
        authorized = getattr(runner, "_is_user_authorized_for_source", None)
        if not callable(authorized) or not authorized(event.source):
            return None
        identity = self._canonicalize(event.source)
        if identity is None:
            return None
        key = self._event_session_key(event)
        for session in getattr(self, "_gpt_live_sessions", {}).values():
            if (session.is_live and session.chat_id == str(event.source.chat_id)
                    and session.cfg.user_id and session.cfg.user_id == str(event.source.user_id or "")
                    and Path(identity.runtime_home).resolve() == session.home.resolve()
                    and session.backend_session_key == key):
                return session

    def _gpt_live_reply_context(self, chat_id):
        context = DELEGATION_CONTEXT.get()
        if context:
            session, did = context
            if (session.chat_id == str(chat_id)
                    and getattr(self, "_gpt_live_sessions", {}).get(session.call_id) is session):
                return context

    def on_assistant_commentary(self, chat_id, content):
        super().on_assistant_commentary(chat_id, content)
        context = self._gpt_live_reply_context(chat_id)
        if context:
            session, did = context
            if not isinstance(did, BackgroundReply):
                session.feed_backend_progress(content, did)

    def _gpt_live_text_failure(self, chat_id):
        sessions = [s for s in getattr(self, "_gpt_live_sessions", {}).values()
                    if s.chat_id == str(chat_id)]
        for session in sessions:
            if not getattr(session, "text_failure_logged", False):
                session.text_failure_logged = True
                logger.warning("GPT-Live text delivery failed; check chat send permissions")
        return bool(sessions)

    def _gpt_live_complete_stream(self, event):
        # The gateway seals streamed responses by edit and can omit send(notify=True).
        # Its processing-complete event carries the authoritative full final text.
        final = getattr(event, "_streamed_final_response", None)
        if final:
            self._gpt_live_tap_reply(event.source.chat_id, final, {"notify": True})

    async def on_processing_complete(self, event, outcome):
        self._gpt_live_complete_stream(event)
        from .projects import PROJECT_CONTEXT
        project = PROJECT_CONTEXT.get()
        if project and project[2] == str(event.source.chat_id):
            service, request_id, _ = project
            if service.db is not None:
                from gateway.run import _profile_runtime_scope
                from .artifacts import capture_artifacts
                from .fulfillment import complete_handoffs
                with _profile_runtime_scope(service.home):
                    receipts = await asyncio.to_thread(service.db.voice_dispatches, service.owner_key, 100)
                    receipt = next((r for r in receipts if r["request_id"] == request_id), None)
                    if receipt is not None:
                        await capture_artifacts(service, self, event, request_id, receipt["payload"]["monitor_url"])
                        await complete_handoffs(service, request_id)
        from gateway.run import _profile_runtime_scope
        runner = getattr(self, "gateway_runner", None)
        for observer in list(getattr(runner, "_live_voice_observers", {}).values()):
            try:
                with _profile_runtime_scope(observer.context.home):
                    await observer.record_result(self, event, outcome)
            except Exception as exc:
                logger.warning("Voice result observation failed (%s)", type(exc).__name__)
        context = getattr(event, "_gpt_live_delegation", None)
        if context and getattr(context[0], "assistant", None) is not None and context[0].is_live:
            session = context[0]
            if not session.assistant.conversation._recall_done:
                session._tasks.append(asyncio.create_task(session.assistant.conversation.recall()))
        await super().on_processing_complete(event, outcome)

    async def handle_message(self, event):
        context = getattr(event, "_gpt_live_delegation", None)
        if context is None and event.internal:
            if session := self._gpt_live_background_session(event):
                context = (session, BackgroundReply(event))
        elif context is None:
            context = self._gpt_live_reply_context(event.source.chat_id)
        if context and context[0].adapter is self and context[0].chat_id == str(event.source.chat_id):
            session = context[0]
            if not event.internal and not getattr(event, "_gpt_live_delegation", None):
                note = VOICE_TASK_CONTEXT
                if session.cfg.assistant_enabled:
                    from .persona import coordinator_note
                    note = coordinator_note(session.cfg.assistant_role)
                    note += "\n\n" + session.assistant.turn_context()
                event.channel_context = "\n\n".join(filter(None, (event.channel_context, note)))
                event.queue_only = True
            event._gpt_live_delegation = context
        return await super().handle_message(event)

    async def _process_message_background(self, event, session_key):
        # Queued gateway turns must keep their own delegation ID, even when they
        # drain from another turn's task. Late answers must never satisfy a new ID.
        context = getattr(event, "_gpt_live_delegation", None)
        if context:
            session, did = context
            if isinstance(did, BackgroundReply):
                if self._gpt_live_background_session(event) is not session:
                    context = None  # A queued completion must not jump to a redialed call.
            else:
                session.backend_session_key = session_key
                session.backend_source = event.source
        token = DELEGATION_CONTEXT.set(context)
        from .projects import PROJECT_CONTEXT
        project_token = PROJECT_CONTEXT.set(getattr(event, "_voice_project_receipt", None))
        try:
            return await super()._process_message_background(event, session_key)
        finally:
            PROJECT_CONTEXT.reset(project_token)
            DELEGATION_CONTEXT.reset(token)

    def _should_auto_tts_for_chat(self, chat_id):
        context = DELEGATION_CONTEXT.get()
        if self._gpt_live_active_for_chat(chat_id) or (context and context[0].chat_id == str(chat_id)):
            return False
        return super()._should_auto_tts_for_chat(chat_id)

    def _streaming_tts_turn_completed(self, session_key, turn_marker=None, *, event=None):
        # The voice frontend owns speech for this turn, including after a close.
        # This also suppresses the runner's explicit /voice all fallback.
        context = DELEGATION_CONTEXT.get() or getattr(event, "_gpt_live_delegation", None)
        if context and context[0].adapter is self:
            return True
        return super()._streaming_tts_turn_completed(session_key, turn_marker, event=event)
