"""Normal gateway reply and commentary taps for live voice frontends."""
import logging
from dataclasses import dataclass
from pathlib import Path
from .session import DELEGATION_CONTEXT
logger = logging.getLogger(__name__)

VOICE_TASK_CONTEXT = (
    "[Live voice task coordination]\n"
    "Ben wants independent work to run concurrently while the call stays responsive. "
    "For substantial or long-running jobs, use the native delegate_task tool with background=true. "
    "Split independent work into focused subagent tasks within the configured concurrency limit; "
    "keep dependent steps ordered and avoid concurrent edits to the same files. Quick answers can "
    "be handled directly. After the tool confirms dispatch, briefly say which jobs started and end "
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
    def _gpt_live_active_for_chat(self, chat_id):
        return any(session.chat_id == str(chat_id)
                   for session in getattr(self, "_gpt_live_sessions", {}).values())

    def _gpt_live_tap_reply(self, chat_id, content, metadata):
        if not (content or "").strip() or not metadata or not metadata.get("notify") or metadata.get("_interim_send"):
            return
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
        await super().on_processing_complete(event, outcome)

    async def handle_message(self, event):
        context = getattr(event, "_gpt_live_delegation", None)
        if context is None and event.internal:
            if session := self._gpt_live_background_session(event):
                context = (session, BackgroundReply(event))
        elif context is None:
            context = self._gpt_live_reply_context(event.source.chat_id)
        if context and context[0].adapter is self and context[0].chat_id == str(event.source.chat_id):
            if not event.internal and not getattr(event, "_gpt_live_delegation", None):
                event.channel_context = "\n\n".join(filter(None, (event.channel_context, VOICE_TASK_CONTEXT)))
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
        token = DELEGATION_CONTEXT.set(context)
        try:
            return await super()._process_message_background(event, session_key)
        finally:
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
