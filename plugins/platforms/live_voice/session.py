"""Platform-neutral GPT-Live wire client, audio clock and Hermes delegations."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import logging
import math
import statistics
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
_wire_logger = logging.getLogger(__name__ + ".wire")
_wire_logger.setLevel(logging.WARNING)

DELEGATION_CONTEXT = ContextVar("live_voice_delegation", default=None)
DEFAULT_INSTRUCTIONS = (
    "You are Hermes, talking with Ben on a live voice call. Keep spoken replies short, "
    "natural and conversational. Delegate anything that needs information, tools, memory, "
    "or an action to the backend. Never claim work is finished until the backend confirms it. "
    "If the backend is still working, say so briefly and keep listening."
)
BACKEND_STATUS_INSTRUCTIONS = (
    "Hermes tools and memories are available through backend delegation. Backend progress "
    "updates are factual context about that task. Individual actions can finish before the "
    "overall backend reply is ready. Use the latest update for each task: a pending final "
    "reply does not mean that no action has been taken. If Ben asks for an action's status "
    "and it is unknown, say you are waiting for an update and delegate a status check. Do not guess "
    "that work has not started or repeat an outdated status. You can accept new independent "
    "requests while earlier backend work continues. Do not tell Ben he must wait for one job "
    "before asking for another. Keep each job's status distinct. Background task updates "
    "refer to that work, not to the most recent request. A verified subagent roster lists "
    "the workers owned by this conversation. Use their IDs and task labels to track all jobs; "
    "current_tool is current activity and last_tool is only the last tool used. An ended worker "
    "awaiting its report has no confirmed outcome yet. Give brief, useful progress updates "
    "without reading raw roster data aloud. Background work continues when speech is interrupted."
)

class GptLiveUnavailable(RuntimeError):
    pass

@dataclass(frozen=True)
class LiveVoiceConfig:
    enabled: bool = False
    user_id: str = ""
    model: str = "gpt-live-1"
    voice: str = "gleam"
    ws_url: str = "wss://api.openai.com/v1/live/sessions"
    instructions: str = DEFAULT_INSTRUCTIONS
    idle_timeout_seconds: int = 900
    max_session_minutes: int = 60
    reply_timeout_seconds: int = 180
    barge_in: bool = True
    barge_in_min_speech_ms: int = 200
    barge_in_mute_ms: int = 250
    prime_ms: int = 120
    summarize_for_voice: bool = True
    log_usage: bool = True

def commentary_chunks(text: str):
    # UTF-8 bytes bound tokens conservatively, including CJK and unusual Unicode.
    chunk, size = [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > 480:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield "".join(chunk)

@dataclass
class PendingDelegation:
    id: str
    future: asyncio.Future

class LiveVoiceSession:
    START_TIMEOUT = 15
    CLOSE_TIMEOUT = 15
    TRANSCRIPT_SETTLE_SECONDS = 0.25
    TRANSCRIPT_MAX_WAIT_SECONDS = 1.5
    MAX_PARALLEL_REQUESTS = 4

    def __init__(self, adapter, call_id, chat_id, cfg, *, mixer, dispatch,
                 input_to_pcm24, output_from_pcm24, input_frame_bytes=2, usage_context=None):
        from hermes_constants import get_hermes_home
        self.adapter, self.call_id, self.chat_id = adapter, call_id, str(chat_id)
        self.cfg, self.mixer, self._dispatch = cfg, mixer, dispatch
        self._input_to_pcm24, self._output_from_pcm24 = input_to_pcm24, output_from_pcm24
        self._input_frame_bytes = input_frame_bytes
        self._usage_context = dict(usage_context or {})
        self.home = Path(get_hermes_home())
        self.backend_session_key = None
        self.backend_source = None
        self.loop = asyncio.get_running_loop()
        self.close_requested = False
        self.close_requested_reason = None
        self._closing = False
        self._ready, self.finalized = asyncio.Event(), asyncio.Event()
        self._close_lock, self._send_lock = asyncio.Lock(), asyncio.Lock()
        self._ws = self._receiver = None
        self._tasks = []
        self._accepting_audio = self._transport_ended = False
        self._end_notified = False
        self._start_error = None
        self.buffer = None
        self.session_id = ""
        self.usage_seconds = 0.0
        self._usage_logged = False
        self._started_at = self._last_user_audio_at = time.monotonic()
        self._input_remainder = self._output_remainder = b""
        self._audio = asyncio.Queue(maxsize=100)
        self._delegations = asyncio.Queue(maxsize=32)
        self._quiet_acks = asyncio.Queue(maxsize=32)
        self._backend_updates = asyncio.Queue(maxsize=32)
        self._backend_completed = set()
        self.backend_updates_sent = 0
        self._commentary_seq = 0
        self._seen = set()
        self._transcripts = deque()
        self._transcript_size = 0
        self._transcript_seq = self._consumed_seq = 0
        self._last_transcript_at = 0.0
        self.pending: dict[str, PendingDelegation] = {}
        self.dropped_frames = self.delegations = 0
        self.input_frames = self.sent_audio_frames = self.output_audio_bytes = 0
        self.first_audio_ms = []
        self._awaiting_audio = None
        self.prompt_sha256 = ''
        from .agent_status import AgentStatusFeed
        self.agent_status = AgentStatusFeed(self)

    @property
    def is_live(self):
        return self._accepting_audio and not self._closing and not self.close_requested

    async def _send(self, event, *, guard=None):
        async with self._send_lock:
            if guard is not None and not guard():
                return False
            await self._ws.send(json.dumps(event))
            return True

    async def start(self):
        from gateway.platforms._shared import get_scoped_secret
        import websockets
        key = get_scoped_secret("OPENAI_API_KEY") or get_scoped_secret("VOICE_TOOLS_OPENAI_KEY")
        if not key:
            raise GptLiveUnavailable("OpenAI project API key is missing")
        self._ws = await websockets.connect(
            self.cfg.ws_url, additional_headers={"Authorization": f"Bearer {key}"},
            ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024,
            open_timeout=self.START_TIMEOUT, close_timeout=self.CLOSE_TIMEOUT,
            logger=_wire_logger,
        )
        instructions = self.cfg.instructions + "\n\n" + BACKEND_STATUS_INSTRUCTIONS
        self.prompt_sha256 = hashlib.sha256(instructions.encode()).hexdigest()
        await self._send({
            "type": "session.start", "event_id": "start",
            "session": {
                "model": self.cfg.model,
                "instructions": instructions,
                "audio": {"format": {"type": "audio/pcm", "rate": 24000},
                          "output": {"voice": self.cfg.voice}},
                "delegation": {"type": "client", "acknowledgement_delay_ms": 800}, "store": False,
            },
        })
        self._receiver = asyncio.create_task(self._receive())
        await asyncio.wait_for(self._ready.wait(), self.START_TIMEOUT)
        if self._start_error or not self.session_id:
            raise GptLiveUnavailable(self._start_error or "Live transport ended during startup")
        if self.close_requested:
            return
        self.buffer = self.mixer.attach_live(prime_ms=self.cfg.prime_ms, mute_ms=self.cfg.barge_in_mute_ms)
        self._started_at = self._last_user_audio_at = time.monotonic()
        self._accepting_audio = True
        self._tasks = [asyncio.create_task(worker()) for worker in (
            self._send_audio_worker, self._send_quiet_acks, self._delegation_worker, self._send_backend_updates,
            self.agent_status.run, self._watchdog,
        )]
        logger.info("GPT-Live session started (call=%s session=%s voice=%s prompt=%s)",
                    self.call_id, self.session_id, self.cfg.voice, self.prompt_sha256[:12])

    def feed_user_pcm(self, user_id, pcm):
        if str(user_id) != self.cfg.user_id or not self.is_live:
            return
        self.loop.call_soon_threadsafe(self._enqueue_audio, pcm)

    def _enqueue_audio(self, pcm):
        if not self.is_live:
            return
        self.input_frames += 1
        if self.input_frames == 1:
            logger.info("GPT-Live microphone PCM received (call=%s bytes=%d)", self.call_id, len(pcm))
        import numpy as np
        samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(np.float32)
        if samples.size and float(np.mean(samples * samples)) > 300 ** 2:
            self._last_user_audio_at = time.monotonic()
        if self._audio.full():
            self._audio.get_nowait()
            self.dropped_frames += 1
            if self.dropped_frames == 1:
                logger.warning("GPT-Live microphone queue overflow; dropping oldest frames")
        self._audio.put_nowait(pcm)

    def note_user_speech(self):
        self.loop.call_soon_threadsafe(self._barge_in)

    def _barge_in(self):
        if self.is_live:
            self._last_user_audio_at = time.monotonic()
            if self.cfg.barge_in and self.buffer is not None:
                self.buffer.flush()

    async def _send_audio_worker(self):
        # Call transports may stop PCM when client VAD goes quiet. Live requires a
        # continuous, paced stream, including silence, to advance its audio clock.
        pending = bytearray()
        deadline = self.loop.time()
        try:
            while True:
                delay = deadline - self.loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                while not self._audio.empty():
                    pcm = self._input_remainder + self._audio.get_nowait()
                    length = len(pcm) // self._input_frame_bytes * self._input_frame_bytes
                    self._input_remainder = pcm[length:]
                    if length:
                        pending.extend(self._input_to_pcm24(pcm[:length]))
                if len(pending) > 96000:
                    dropped = (len(pending) - 96000 + 959) // 960
                    del pending[:dropped * 960]
                    self.dropped_frames += dropped
                frame = bytes(pending[:960])
                del pending[:960]
                await self._send({"type": "session.input_audio.append",
                                  "audio": base64.b64encode(frame.ljust(960, b"\0")).decode("ascii")})
                self.sent_audio_frames += 1
                # Never catch up a stalled socket with a burst of stale frames.
                deadline = max(deadline + 0.02, self.loop.time())
        except Exception as exc:
            logger.warning("GPT-Live audio send failed (%s)", type(exc).__name__)
            self._ended("transport")

    async def _receive(self):
        handlers = {
            "session.started": self._on_started,
            "session.input_transcript.delta": self._on_transcript,
            "session.output_audio.delta": self._on_audio,
            "session.delegation.created": self._on_delegation,
            "session.usage.updated": self._on_usage,
            "session.closed": self._on_closed,
            "error": self._on_error,
        }
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                handler = handlers.get(event.get("type"))
                if handler:
                    handler(event)
                if self.finalized.is_set():
                    break
        except Exception as exc:
            if not self._closing:
                logger.warning("GPT-Live receive failed (%s)", type(exc).__name__)
        finally:
            self._transport_ended = True
            self._ready.set()
            if not self._closing:
                self._ended("server_closed" if self.finalized.is_set() else "transport")

    def _ended(self, reason):
        if self._closing or self._end_notified:
            return
        self._end_notified = True
        self._accepting_audio = False
        self.adapter._gpt_live_session_ended(self, reason)

    def _on_started(self, event):
        self.session_id = event.get("session", {}).get("id", "")
        self._ready.set()

    def _on_transcript(self, event):
        text = event.get("delta", "")
        if not isinstance(text, str) or not text:
            return
        text = text[-8000:]
        self._transcript_seq += 1
        self._transcripts.append((self._transcript_seq, text, event.get("end_ms")))
        self._transcript_size += len(text)
        while self._transcript_size > 8000:
            self._transcript_size -= len(self._transcripts.popleft()[1])
        self._last_transcript_at = time.monotonic()
        self._last_user_audio_at = self._last_transcript_at

    def _on_audio(self, event):
        if not self.is_live or self.buffer is None:
            return
        pcm = self._output_remainder + base64.b64decode(event.get("delta", ""), validate=True)
        length = len(pcm) // 2 * 2
        self._output_remainder = pcm[length:]
        if length:
            if not self.output_audio_bytes:
                logger.info("GPT-Live output PCM received (call=%s)", self.call_id)
            self.output_audio_bytes += length
            self.buffer.feed(self._output_from_pcm24(pcm[:length]))
            if self._awaiting_audio is not None:
                self.first_audio_ms.append(round((time.monotonic() - self._awaiting_audio) * 1000, 1))
                self._awaiting_audio = None

    def _on_delegation(self, event):
        delegation = event.get("delegation", {})
        did = delegation.get("id")
        if self._closing or delegation.get("target") != "client" or not did or did in self._seen:
            return
        self._seen.add(did)
        if self._delegations.full() or self._quiet_acks.full():
            self._ended("delegation_overflow")
            return
        self._quiet_acks.put_nowait(did)
        self._delegations.put_nowait((did, self._transcript_seq, time.monotonic()))

    async def _send_quiet_acks(self):
        # Independent of request capacity and transcript settlement: don't let
        # automatic "one moment" speech fill a wait for backend work.
        try:
            while True:
                did = await self._quiet_acks.get()
                await self._send({'type': 'session.thinking.append', 'delegation_id': did,
                                  'event_id': f'quiet_ack_{did}', 'content': ''})
        except Exception as exc:
            logger.warning('GPT-Live acknowledgment suppression failed (%s)', type(exc).__name__)
            self._ended('transport')

    def _on_usage(self, event):
        seconds = event.get("usage", {}).get("seconds")
        if not isinstance(seconds, bool) and isinstance(seconds, (float, int)) and math.isfinite(seconds) and seconds >= 0:
            self.usage_seconds = float(seconds)

    def _on_closed(self, event):
        self._on_usage(event)
        self.finalized.set()

    def _on_error(self, event):
        error = event.get("error", {})
        logger.warning("GPT-Live error code=%s client_event_id=%s", error.get("code"), error.get("client_event_id"))
        if not self.session_id:
            self._start_error = "Live startup rejected; check model access and project billing"
            self._ready.set()
        elif error.get("fatal") or error.get("code") in {"session_expired", "server_error", "invalid_api_key"}:
            self._ended("api_error")

    async def _settled_request(self, through_seq):
        deadline = time.monotonic() + self.TRANSCRIPT_MAX_WAIT_SECONDS
        while not self._closing and time.monotonic() < deadline:
            if self._last_transcript_at and time.monotonic() - self._last_transcript_at >= self.TRANSCRIPT_SETTLE_SECONDS:
                break
            await asyncio.sleep(0.05)
        # Snapshot at dispatch; queued delegations retain their original boundary.
        if self._delegations.empty():
            through_seq = self._transcript_seq
        text = "".join(t for seq, t, _ in self._transcripts if self._consumed_seq < seq <= through_seq)
        self._consumed_seq = max(self._consumed_seq, through_seq)
        return " ".join(text.split())

    def complete_pending(self, content, delegation_id):
        if delegation_id in self._backend_completed:
            return
        self._backend_completed.add(delegation_id)
        pending = self.pending.get(delegation_id)
        if pending and not pending.future.done():
            pending.future.set_result(content)
        else:
            # A timed-out turn may finish while a newer request is pending. Keep
            # its result as context for its OWN task without completing the new one.
            self.feed_backend_progress(content, delegation_id, final=True)

    def feed_background_result(self, content):
        # Conversation-owned jobs may finish after their original voice request
        # was acknowledged. Unsolicited commentary never resolves a request.
        if self.is_live and content.strip():
            self.loop.call_soon_threadsafe(self._enqueue_backend_update, None, content, True)

    def feed_backend_progress(self, content, delegation_id, *, final=False):
        if self.is_live and delegation_id in self._seen and content.strip():
            self.loop.call_soon_threadsafe(self._enqueue_backend_update, delegation_id, content, final)

    def _enqueue_backend_update(self, did, content, final):
        if not self.is_live or (did in self._backend_completed and not final):
            return
        if self._backend_updates.full():
            self._backend_updates.get_nowait()
        self._backend_updates.put_nowait((did, content, final))

    async def _send_backend_updates(self):
        try:
            while True:
                did, content, final = await self._backend_updates.get()
                if did is None:
                    if self.cfg.summarize_for_voice:
                        from gateway.voice_summary import summarize_for_voice
                        content = await summarize_for_voice(content, preserve_style=True)
                    await self._commentary(None, "Update on background work: " + content)
                    continue
                for chunk in commentary_chunks(content):
                    if did in self._backend_completed and not final:
                        break
                    await self._send({
                        "type": "session.thinking.append", "delegation_id": did,
                        "event_id": f"backend_update_{self.backend_updates_sent + 1}", "content": chunk,
                    })
                    self.backend_updates_sent += 1
                    if self.backend_updates_sent == 1:
                        logger.info("GPT-Live backend progress sent (call=%s)", self.call_id)
        except Exception as exc:
            logger.warning("GPT-Live backend update failed (%s)", type(exc).__name__)
            self._ended("transport")

    async def _commentary(self, did, content, *, guard=None):
        for chunk in commentary_chunks(content):
            self._commentary_seq += 1
            event = {"type": "session.commentary.append", "delegation_id": did, "content": chunk,
                     "event_id": f"commentary_{self._commentary_seq}"}
            if guard is None:
                await self._send(event)
            elif not await self._send(event, guard=guard):
                return

    async def _delegation_worker(self):
        active = set()

        def finished(task):
            active.discard(task)
            if not task.cancelled() and task.exception():
                logger.warning("GPT-Live request delivery failed (%s)", type(task.exception()).__name__)
                self._ended("transport")

        try:
            while True:
                if len(active) >= self.MAX_PARALLEL_REQUESTS:
                    await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                did, through_seq, created_at = await self._delegations.get()
                # Reserve transcript boundaries in order; backend replies may
                # finish in any order. The gateway still serializes its parent
                # conversation, while native background subagents do the work.
                text = await self._settled_request(through_seq)
                from .coordination import is_conversational_hold
                if not text or is_conversational_hold(text):
                    content = (
                        'Ben asked for a moment. Stay quiet and give him time. This does not cancel existing jobs.'
                        if text else
                        'No new user request since the last handoff. Do not repeat earlier tasks or start new work. '
                        'Continue listening and use the verified status already provided.'
                    )
                    await self._send({'type': 'session.thinking.append', 'delegation_id': did,
                                      'event_id': f'no_work_{did}', 'content': content})
                    continue
                if len(text) < 2:
                    await self._commentary(did, "I didn't catch that — could you say it again?")
                    continue
                task = asyncio.create_task(self._run_delegation(did, text, created_at))
                active.add(task)
                task.add_done_callback(finished)
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    async def _run_delegation(self, did, text, created_at):
        self.delegations += 1
        pending = PendingDelegation(did, self.loop.create_future())
        self.pending[did] = pending  # Register BEFORE dispatch: fast replies cannot be lost.
        token = DELEGATION_CONTEXT.set((self, did))
        try:
            await self._dispatch(text)
            result = await asyncio.wait_for(asyncio.shield(pending.future), self.cfg.reply_timeout_seconds)
            if self.cfg.summarize_for_voice:
                from gateway.voice_summary import summarize_for_voice
                result = await summarize_for_voice(result, preserve_style=True)
            self._awaiting_audio = created_at
            await self._commentary(did, result)
        except TimeoutError:
            await self._commentary(did, "That's taking longer than expected — I'll keep working on it.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("GPT-Live delegation failed (%s)", type(exc).__name__)
            if self.is_live:
                await self._commentary(did, "I hit an error while working on that.")
        finally:
            DELEGATION_CONTEXT.reset(token)
            if not pending.future.done():
                pending.future.cancel()
            if self.pending.get(did) is pending:
                self.pending.pop(did)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - self._started_at >= self.cfg.max_session_minutes * 60:
                self._ended("max_duration")
                return
            if (not self.pending and self._delegations.empty()
                    and now - self._last_user_audio_at >= self.cfg.idle_timeout_seconds):
                self._ended("idle")
                return

    def _write_usage(self, reason):
        if self._usage_logged or not self.cfg.log_usage or not self.session_id:
            return
        self._usage_logged = True
        record = {
            "ts": datetime.now(timezone.utc).isoformat(), "call_id": self.call_id,
            **self._usage_context, "session_id": self.session_id,
            "seconds": self.usage_seconds, "estimated_voice_usd": round(self.usage_seconds / 60 * 0.05, 4),
            "model": self.cfg.model, "reason": reason, "usage_confirmed": self.finalized.is_set(),
            "prompt_sha256": self.prompt_sha256,
            "first_audio_ms_p50": statistics.median(self.first_audio_ms) if self.first_audio_ms else None,
            "delegations": self.delegations, "dropped_frames": self.dropped_frames,
            "input_frames": self.input_frames, "sent_audio_frames": self.sent_audio_frames,
            "output_audio_bytes": self.output_audio_bytes,
            "backend_updates_sent": self.backend_updates_sent,
        }
        try:
            path = self.home / "logs" / "gpt-live-usage.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            logger.warning("Could not write GPT-Live usage log")

    async def close(self, reason):
        async with self._close_lock:
            if self._closing:
                return
            self._closing = True
            self._accepting_audio = False
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.buffer is not None:
                self.buffer.flush()
            try:
                if self._ws is not None and not self._transport_ended:
                    await self._send({"type": "session.close"})
                    await asyncio.wait_for(self.finalized.wait(), self.CLOSE_TIMEOUT)
            except (TimeoutError, OSError):
                logger.warning("GPT-Live close timed out; usage unconfirmed")
            except Exception as exc:
                logger.warning("GPT-Live close failed (%s); usage unconfirmed", type(exc).__name__)
            finally:
                if not self.finalized.is_set() and self.session_id:
                    logger.warning("GPT-Live final usage unconfirmed (session=%s)", self.session_id)
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception as exc:
                        logger.warning("GPT-Live socket close failed (%s)", type(exc).__name__)
                tasks = [t for t in [self._receiver, *self._tasks] if t and t is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.mixer is not None:
                    self.mixer.detach_live()
                await asyncio.to_thread(self._write_usage, reason)
                self._transcripts.clear()
                self._input_remainder = self._output_remainder = b""
                while not self._audio.empty():
                    self._audio.get_nowait()
                logger.info("GPT-Live session closed (call=%s reason=%s)", self.call_id, reason)
