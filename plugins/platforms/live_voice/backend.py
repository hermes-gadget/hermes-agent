"""Normal gateway reply and commentary taps for live voice frontends."""
import logging
from .session import DELEGATION_CONTEXT
logger = logging.getLogger(__name__)

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
            session.complete_pending(content, did)

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
        context = DELEGATION_CONTEXT.get()
        if context and context[0].adapter is self and context[0].chat_id == str(event.source.chat_id):
            event._gpt_live_delegation = context
        return await super().handle_message(event)

    async def _process_message_background(self, event, session_key):
        # Queued gateway turns must keep their own delegation ID, even when they
        # drain from another turn's task. Late answers must never satisfy a new ID.
        token = DELEGATION_CONTEXT.set(getattr(event, "_gpt_live_delegation", None))
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
