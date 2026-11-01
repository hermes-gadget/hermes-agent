"""Bounded GPT-Live reconnection inside an existing authorized platform call."""
import asyncio
import base64
import json
import logging
import time

logger = logging.getLogger(__name__)


class RecoveryAudio:
    def __init__(self, seconds=5):
        self.limit = seconds * 48000
        self.data = bytearray()
        self.overflow = False

    def append(self, pcm):
        if self.overflow:
            return
        if len(self.data) + len(pcm) > self.limit:
            self.data.clear()
            self.overflow = True
        else:
            self.data.extend(pcm)

    def take(self):
        data, overflow = bytes(self.data), self.overflow
        self.data.clear()
        self.overflow = False
        return data, overflow


class LiveRecoveryMixin:
    RECOVERY_DELAYS = (1, 2, 4, 8)
    RECOVERY_DEADLINE = 30

    def _init_recovery(self):
        self.logical_session_id = ""
        self._connection_generation = 0
        self._connection_ready = asyncio.Event()
        self._connection_ready.set()
        self._recovering = False
        self._recovery_task = None
        self._recovery_audio = RecoveryAudio()
        self._delegation_connections = {}
        self.recoveries = 0
        self.recovery_durations_ms = []
        self._instructions = ""

    def _request_recovery(self, reason):
        if (self.assistant is None or not self.is_live or self.close_requested
                or reason != "transport"):
            return False
        if self._recovering:
            return True
        self._recovering = True
        self._connection_ready.clear()
        if self.buffer is not None:
            self.buffer.flush()
        self.assistant.conversation.interrupted()
        self._recovery_task = asyncio.create_task(self._recover_connection())
        return True

    async def _open_connection(self, history):
        from gateway.platforms._shared import get_scoped_secret
        import websockets
        key = get_scoped_secret("OPENAI_API_KEY") or get_scoped_secret("VOICE_TOOLS_OPENAI_KEY")
        if not key:
            from .session import GptLiveUnavailable
            raise GptLiveUnavailable("OpenAI project API key is missing")
        self._connection_generation += 1
        generation = self._connection_generation
        self._ready.clear()
        self.finalized.clear()
        self._start_error = None
        self.session_id = ""
        self._transport_ended = False
        self._ws = await websockets.connect(self.cfg.ws_url,
            additional_headers={"Authorization": f"Bearer {key}"}, ping_interval=20, ping_timeout=20,
            max_size=8 * 1024 * 1024, open_timeout=self.START_TIMEOUT, close_timeout=self.CLOSE_TIMEOUT,
            logger=logging.getLogger("plugins.platforms.live_voice.session.wire"))
        await self._ws.send(json.dumps({"type": "session.start", "event_id": "start",
            "session": {"model": self.cfg.model, "instructions": self._instructions,
                "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": self.cfg.voice}},
                "delegation": {"type": "client", "acknowledgement_delay_ms": 800}, "store": False,
                **({"input": history} if history else {})}}))
        self._receiver = asyncio.create_task(self._receive(self._ws, generation))
        await asyncio.wait_for(self._ready.wait(), self.START_TIMEOUT)
        if self._start_error or not self.session_id or self._transport_ended:
            from .session import GptLiveUnavailable
            raise GptLiveUnavailable(self._start_error or "Live transport ended during startup")
        if not self.logical_session_id:
            self.logical_session_id = self.session_id

    async def _recover_connection(self):
        started = time.monotonic()
        recovered = False
        try:
            before = {r["request_id"]: r["status"] for r in await asyncio.to_thread(
                self.assistant.db.voice_dispatches, self.assistant.owner_key)} if self.assistant.db else {}
            await self.assistant.interactions.interrupted()
            async with asyncio.timeout(self.RECOVERY_DEADLINE):
                for delay in self.RECOVERY_DELAYS:
                    if self.close_requested or self._closing:
                        return
                    await self._retire_connection()
                    await asyncio.sleep(delay)
                    self.assistant.authorize()
                    await self.assistant.conversation.flush()
                    history = await self.assistant.prepare()
                    try:
                        await self._open_connection(history)
                    except Exception as exc:
                        code = getattr(getattr(exc, "response", None), "status_code", None)
                        if self._start_error or code in {400, 401, 403, 404}:
                            break
                        logger.info("Live reconnect attempt failed (%s)", type(exc).__name__)
                        continue
                    if self.close_requested or self._closing:
                        return
                    audio, overflow = self._recovery_audio.take()
                    if audio and not overflow:
                        await self._ws.send(json.dumps({"type": "session.input_audio.append",
                            "audio": base64.b64encode(audio).decode("ascii")}))
                    self._recovering = False
                    self._connection_ready.set()
                    self.recoveries += 1
                    self.recovery_durations_ms.append((time.monotonic() - started) * 1000)
                    recovered = True
                    await self.assistant.quiet(
                        "Voice connection restored. Continue the same conversation. Saved receipts and pending "
                        "questions are authoritative; do not replay actions. Give one short recap of changed watched jobs.")
                    receipts = await asyncio.to_thread(self.assistant.db.voice_dispatches,
                        self.assistant.owner_key) if self.assistant.db else []
                    changes = [f"{r['payload'].get('project', 'Your project')} has a new result in its project conversation."
                        for r in receipts if r["status"] == "reported" and before.get(r["request_id"]) != "reported"]
                    await self._commentary(None, "I'm back. " + (" ".join(changes[:2]) if changes else "We can carry on."))
                    if overflow:
                        await self._commentary(None, "The connection dropped while you were speaking. Please repeat that request.")
                    return
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, ValueError) as exc:
            logger.warning("Live recovery ended (%s)", type(exc).__name__)
        finally:
            if not recovered and not self._closing and not self.close_requested:
                self._recovering = False
                self._connection_ready.set()
                self._ended("recovery_exhausted")

    async def _retire_connection(self):
        self._connection_generation += 1  # Ignore final callbacks from the old socket.
        receiver = self._receiver
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
        if self._ws is not None:
            await self._ws.close()
        if receiver is not None and receiver is not asyncio.current_task():
            await asyncio.gather(receiver, return_exceptions=True)
        await asyncio.to_thread(self._write_usage, "reconnect")
        self.usage_seconds, self._usage_logged = 0.0, False
        self._output_remainder = b""

    def _wire_event(self, event):
        connection = self._delegation_connections.get(event.get("delegation_id"))
        if connection:
            generation, vendor_id = connection
            return {**event, "delegation_id": vendor_id if generation == self._connection_generation else None}
        return event
