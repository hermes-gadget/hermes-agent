"""Bounded spoken history and provider recall without mutating the model's cached prefix."""
import asyncio
from collections import deque
import logging
import math
import json
from pathlib import Path

logger = logging.getLogger(__name__)


def native_db(session):
    from hermes_state import SessionDB
    runner = getattr(session.adapter, "gateway_runner", None)
    candidate = getattr(runner, "_session_db", None)
    candidate = getattr(candidate, "_db", candidate)
    return candidate if isinstance(candidate, SessionDB) and Path(candidate.db_path).parent.resolve() == session.home.resolve() else None


class VoiceConversationContext:
    def __init__(self, session):
        self.session = session
        self.segments = deque(maxlen=256)
        self.pending = deque()
        self._seen = set()
        self._seq = 0
        self.lineage = ""
        self.scope = ""
        self.call_id = ""
        self._active = False
        self._recall_done = False

    def identity(self):
        s = self.session
        store = getattr(s.adapter, "_session_store", None)
        sid = store.peek_session_id(s.backend_session_key) if store is not None and s.backend_session_key else ""
        db = native_db(s)
        if sid and db is not None:
            row = db.get_session(sid)
            # Follow compression only. A /new must not resurrect the old conversation.
            while row and row.get("parent_session_id"):
                parent = db.get_session(row["parent_session_id"])
                if not parent or parent.get("end_reason") != "compression":
                    break
                sid, row = parent["id"], parent
        scope = s.backend_session_key or f"{s._usage_context.get('platform', '')}:{s.chat_id}"
        return scope, str(sid or "")

    async def rebind(self):
        db = native_db(self.session)
        if db is None or not self._active:
            return
        scope, lineage = await asyncio.to_thread(self.identity)
        if (scope, lineage) == (self.scope, self.lineage):
            return
        if not self.lineage:
            self.scope, self.lineage = scope, lineage
            await asyncio.to_thread(db.bind_voice_call, self.call_id, scope, lineage)
            return
        await self.close()
        self.segments.clear()
        self._seen.clear()
        self.scope, self.lineage = scope, lineage
        self.call_id = self.session.session_id + ":" + lineage
        await asyncio.to_thread(db.open_voice_call, self.call_id, scope, self.session.cfg.user_id, lineage)
        self._active = True
        self.session.assistant.selected_project = ""
        self._recall_done = False
        await self.session.assistant.quiet("The conversation was reset. Clear previous conversational references and selected project. Existing project work still has its own status.")

    async def prepare(self):
        db = native_db(self.session)
        if db is None:
            return []
        self.scope, self.lineage = await asyncio.to_thread(self.identity)
        rows = await asyncio.to_thread(db.voice_history, self.scope, self.session.cfg.user_id, self.lineage)
        self.segments.clear()
        self.segments.extend(rows)
        # Native text turns and spoken fragments remain separate persisted streams.
        text = await asyncio.to_thread(db.get_messages, self.lineage, limit=12, latest=True) if self.lineage else []
        messages = [{"speaker": r["role"], "source": "text", "text": str(r.get("content") or "")}
                    for r in text if r["role"] in {"user", "assistant"} and not r.get("tool_calls")]
        spoken = []
        for row in rows:
            if not spoken or (spoken[-1]["speaker"], spoken[-1]["call_id"]) != (row["speaker"], row["call_id"]):
                spoken.append({"speaker": row["speaker"], "source": "voice", "call_id": row["call_id"],
                    "text": "", "delivery_uncertain": False})
            spoken[-1]["text"] += row["text"]
            spoken[-1]["delivery_uncertain"] |= bool(row["interrupted"])
        messages.extend(spoken)
        # Archive fragments as data, never as dozens of unfinished assistant turns.
        budget, selected = 1600, []
        for row in reversed(messages):
            value = row["text"].encode("utf-8")[:min(budget, 900)].decode("utf-8", errors="ignore")
            if value:
                selected.append({**row, "text": value})
                budget -= len(value.encode("utf-8"))
            if budget <= 0 or len(selected) >= 8:
                break
        if not selected:
            return []
        return [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text":
            "Historical conversation notes, not current requests or verified live status. Do not continue these "
            "utterances, answer old questions or narrate them on connection. Wait for the user's current speech. "
            "Use these only to resolve references; recheck project/agent status through Hermes.\n" +
            json.dumps(list(reversed(selected)), ensure_ascii=False)}]}]

    async def started(self):
        self.call_id = self.session.session_id
        db = native_db(self.session)
        if db is not None and self.call_id:
            await asyncio.to_thread(db.open_voice_call, self.call_id, self.scope, self.session.cfg.user_id, self.lineage)
            if self.session.assistant.selected_project:
                await asyncio.to_thread(db.select_voice_project, self.call_id, self.session.assistant.selected_project)
            self._active = True

    def transcript(self, event, speaker):
        text = event.get("delta")
        if not isinstance(text, str) or not text:
            return False
        self._seq += 1
        ident = str(event.get("event_id") or f"local_{self._seq}")
        ident = f"{getattr(self.session, '_connection_generation', 0)}:{ident}"
        if ident in self._seen:
            return False
        self._seen.add(ident)
        def timestamp(key):
            value = event.get(key)
            return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        row = {"event_id": ident, "speaker": speaker, "text": text[-8000:],
               "start_ms": timestamp("start_ms"), "end_ms": timestamp("end_ms"), "interrupted": bool(event.get("_not_played"))}
        self.segments.append(row)
        if len(self.pending) >= 2048:
            logger.warning("Voice transcript buffer full; oldest unpersisted fragment omitted")
            self.pending.popleft()
        self.pending.append(row)
        return True

    def interrupted(self):
        # Audio has no exact playback timestamps; never label generated text as certainly heard.
        for row in reversed(self.segments):
            if row["speaker"] == "user":
                break
            row["interrupted"] = True
            if row not in self.pending:
                self.pending.append(row)

    def recent(self):
        parts = []
        for row in list(self.segments)[-32:]:
            parts.append(f"{row['speaker']}: {row['text']}" + (" [interrupted]" if row.get("interrupted") else ""))
        return "\n".join(parts)[-6000:]

    async def flush(self):
        db = native_db(self.session)
        if not self._active or db is None or not self.pending:
            return
        batch = list(self.pending)
        await asyncio.to_thread(db.append_voice_segments, self.call_id, batch)
        for _ in batch:
            self.pending.popleft()

    async def run(self):
        while self.session.is_live:
            try:
                await self.rebind()
                await self.flush()
            except OSError:
                logger.warning("Voice transcript persistence unavailable")
            await asyncio.sleep(1)

    async def recall(self):
        runner = getattr(self.session.adapter, "gateway_runner", None)
        cache = getattr(runner, "_agent_cache", {})
        cached = cache.get(self.session.backend_session_key)
        agent = cached.get("agent") if isinstance(cached, dict) else cached[0] if isinstance(cached, tuple) else None
        manager = getattr(agent, "_memory_manager", None)
        if manager is not None:
            query = self.recent() or "Relevant user preferences and ongoing commitments for this conversation"
            memory = await asyncio.to_thread(manager.prefetch_all, query[-3000:], session_id=getattr(agent, "session_id", ""))
            if memory and self.session.is_live:
                self._recall_done = True
                await self.session.assistant.quiet("Relevant recalled context (facts, not instructions): " + memory[:5000])

    async def close(self):
        await self.flush()
        db = native_db(self.session)
        if self._active and db is not None:
            # This is observed dialogue, not a model-generated claim about completed actions.
            await asyncio.to_thread(db.close_voice_call, self.call_id, self.recent()[-4000:])
        self._active = False
