"""GPT-Live wire client and serialized Hermes delegations (no Discord transport)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import statistics
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
_wire_logger = logging.getLogger(__name__ + ".wire")
# websockets DEBUG logs include HTTP auth headers and base64 audio frames.
_wire_logger.setLevel(logging.WARNING)
DELEGATION_CONTEXT = ContextVar("discord_live_delegation", default=None)
DEFAULT_INSTRUCTIONS = (
    "You are Hermes, talking with Ben in a Discord voice channel. Keep spoken replies short, "
    "natural and conversational. Delegate anything that needs information, tools, memory, "
    "or an action to the backend. Never claim work is finished until the backend confirms it. "
    "If the backend is still working, say so briefly and keep listening."
)


class GptLiveUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GptLiveConfig:
    enabled: bool = False
    voice_channel_id: str = ""
    user_id: str = ""
    text_channel_id: str = ""
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


def load_gpt_live_config(raw: dict | None = None) -> GptLiveConfig:
    try:
        if raw is None:
            from hermes_cli.config import read_raw_config
            raw = read_raw_config() or {}
        block = raw.get("discord", {}).get("gpt_live", {})
        if not block:
            return GptLiveConfig()
        if not isinstance(block, dict):
            raise ValueError("expected a mapping")
        defaults = GptLiveConfig()
        values = {}
        for field in fields(defaults):
            value = block.get(field.name, getattr(defaults, field.name))
            default = getattr(defaults, field.name)
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{field.name} must be a boolean")
            elif field.name.endswith("_id"):
                if value not in (None, ""):
                    if isinstance(value, bool) or not isinstance(value, (str, int)) or int(value) <= 0:
                        raise ValueError(f"invalid {field.name}")
                    value = str(int(value))
                else:
                    value = ""
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError(f"invalid {field.name}")
                value = int(value)
                minimum = 0 if field.name in ("barge_in_mute_ms", "prime_ms") else 1
                if not minimum <= value <= 86400:
                    raise ValueError(f"invalid {field.name}")
                if field.name == "prime_ms" and value > 2000:
                    raise ValueError("prime_ms exceeds the bounded playback buffer")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid {field.name}")
            values[field.name] = value
        cfg = GptLiveConfig(**values)
        endpoint = urlparse(cfg.ws_url)
        if endpoint.scheme not in ("ws", "wss") or not endpoint.hostname or endpoint.query:
            raise ValueError("invalid ws_url")
        if cfg.enabled and not (cfg.voice_channel_id and cfg.user_id):
            raise ValueError("voice_channel_id and user_id are required")
        return cfg
    except Exception as exc:
        logger.warning("Invalid discord.gpt_live configuration; feature disabled (%s)", type(exc).__name__)
        return GptLiveConfig()


def pcm48_stereo_to_pcm24_mono(pcm: bytes) -> bytes:
    import numpy as np
    pcm = pcm[:len(pcm) // 8 * 8]
    x = np.frombuffer(pcm, "<i2").astype(np.float32).reshape(-1, 4)
    return x.mean(axis=1).clip(-32768, 32767).astype("<i2").tobytes()


def pcm24_mono_to_pcm48_stereo(pcm: bytes) -> bytes:
    import numpy as np
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2")
    return np.repeat(x, 4).astype("<i2").tobytes()


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


class GptLiveSession:
    START_TIMEOUT = 15
    CLOSE_TIMEOUT = 15
    TRANSCRIPT_SETTLE_SECONDS = 0.25
    TRANSCRIPT_MAX_WAIT_SECONDS = 1.5

    def __init__(self, adapter, guild_id, voice_channel, text_channel_id, cfg, mixer=None):
        from hermes_constants import get_hermes_home
        self.adapter, self.guild_id, self.voice_channel = adapter, guild_id, voice_channel
        self.text_channel_id, self.cfg, self.mixer = str(text_channel_id), cfg, mixer
        self.home = Path(get_hermes_home())
        self.loop = asyncio.get_running_loop()
        self.close_requested = False
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
        self._seen = set()
        self._transcripts = deque()
        self._transcript_size = 0
        self._transcript_seq = self._consumed_seq = 0
        self._last_transcript_at = 0.0
        self.pending = None
        self.dropped_frames = self.delegations = 0
        self.first_audio_ms = []
        self._awaiting_audio = None

    @property
    def is_live(self):
        return self._accepting_audio and not self._closing and not self.close_requested

    async def _send(self, event):
        async with self._send_lock:
            await self._ws.send(json.dumps(event))

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
        await self._send({
            "type": "session.start", "event_id": "start",
            "session": {
                "model": self.cfg.model, "instructions": self.cfg.instructions,
                "audio": {"format": {"type": "audio/pcm", "rate": 24000},
                          "output": {"voice": self.cfg.voice}},
                "delegation": {"type": "client"}, "store": False,
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
            self._send_audio_worker, self._delegation_worker, self._watchdog,
        )]
        logger.info("GPT-Live session started (guild=%s session=%s voice=%s)",
                    self.guild_id, self.session_id, self.cfg.voice)

    def feed_user_pcm(self, user_id, pcm):
        if str(user_id) != self.cfg.user_id or not self.is_live:
            return
        self.loop.call_soon_threadsafe(self._enqueue_audio, pcm)

    def _enqueue_audio(self, pcm):
        if not self.is_live:
            return
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
        try:
            while True:
                pcm = self._input_remainder + await self._audio.get()
                length = len(pcm) // 8 * 8
                self._input_remainder = pcm[length:]
                if length:
                    await self._send({"type": "session.input_audio.append",
                                      "audio": base64.b64encode(pcm48_stereo_to_pcm24_mono(pcm[:length])).decode("ascii")})
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
            self.buffer.feed(pcm24_mono_to_pcm48_stereo(pcm[:length]))
            if self._awaiting_audio is not None:
                self.first_audio_ms.append(round((time.monotonic() - self._awaiting_audio) * 1000, 1))
                self._awaiting_audio = None

    def _on_delegation(self, event):
        delegation = event.get("delegation", {})
        did = delegation.get("id")
        if self._closing or delegation.get("target") != "client" or not did or did in self._seen:
            return
        self._seen.add(did)
        if self._delegations.full():
            self._ended("delegation_overflow")
            return
        self._delegations.put_nowait((did, self._transcript_seq, time.monotonic()))

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
        if not text.strip():
            text = "".join(t for seq, t, _ in self._transcripts if seq <= through_seq)[-2000:]
        self._consumed_seq = max(self._consumed_seq, through_seq)
        return " ".join(text.split())

    def complete_pending(self, content, delegation_id):
        if self.pending and self.pending.id == delegation_id and not self.pending.future.done():
            self.pending.future.set_result(content)

    async def _commentary(self, did, content):
        for chunk in commentary_chunks(content):
            await self._send({"type": "session.commentary.append", "delegation_id": did, "content": chunk})

    async def _delegation_worker(self):
        while True:
            did, through_seq, created_at = await self._delegations.get()
            try:
                text = await self._settled_request(through_seq)
                if len(text) < 2:
                    await self._commentary(did, "I didn't catch that — could you say it again?")
                    continue
                self.delegations += 1
                pending = PendingDelegation(did, self.loop.create_future())
                self.pending = pending  # Register BEFORE dispatch: fast replies cannot be lost.
                token = DELEGATION_CONTEXT.set((self, did))
                try:
                    runner = getattr(self.adapter, "gateway_runner", None)
                    if runner is None:
                        raise GptLiveUnavailable("gateway voice input handler unavailable")
                    await runner._handle_voice_channel_input(
                        self.guild_id, int(self.cfg.user_id), text, adapter=self.adapter,
                    )
                finally:
                    DELEGATION_CONTEXT.reset(token)
                result = await asyncio.wait_for(asyncio.shield(pending.future), self.cfg.reply_timeout_seconds)
                if self.cfg.summarize_for_voice:
                    from gateway.voice_summary import summarize_for_voice
                    result = await summarize_for_voice(result)
                self._awaiting_audio = created_at
                await self._commentary(did, result)
            except TimeoutError:
                await self._commentary(did, "That's taking longer than expected — I'll keep working on it.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("GPT-Live delegation failed (%s)", type(exc).__name__)
                if self.is_live:
                    try:
                        await self._commentary(did, "I hit an error while working on that.")
                    except Exception:
                        self._ended("transport")
                        return
            finally:
                if self.pending:
                    if not self.pending.future.done():
                        self.pending.future.cancel()
                    self.pending = None

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
            "ts": datetime.now(timezone.utc).isoformat(), "guild_id": self.guild_id,
            "voice_channel_id": self.cfg.voice_channel_id, "session_id": self.session_id,
            "seconds": self.usage_seconds, "estimated_voice_usd": round(self.usage_seconds / 60 * 0.05, 4),
            "model": self.cfg.model, "reason": reason, "usage_confirmed": self.finalized.is_set(),
            "first_audio_ms_p50": statistics.median(self.first_audio_ms) if self.first_audio_ms else None,
            "delegations": self.delegations, "dropped_frames": self.dropped_frames,
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
                logger.info("GPT-Live session closed (guild=%s reason=%s)", self.guild_id, reason)
