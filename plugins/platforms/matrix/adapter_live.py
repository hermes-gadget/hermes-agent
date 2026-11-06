"""Matrix incoming-call lifecycle and the normal Hermes voice event binding."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from pathlib import Path

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_identity import replace_source
from plugins.platforms.live_voice.backend import LiveBackendMixin
from plugins.platforms.live_voice.lease import LiveVoiceLease
from plugins.platforms.live_voice.playback import LivePlaybackBuffer
from plugins.platforms.live_voice.session import LiveVoiceSession

from .live_config import load_matrix_live_config
from .live_native import NativeCallLink

logger = logging.getLogger(__name__)


class MatrixPlaybackBuffer(LivePlaybackBuffer):
    def __init__(self, flush_native, **kwargs):
        super().__init__(**kwargs)
        self._flush_native = flush_native

    def flush(self):
        super().flush()
        self._flush_native()


class MatrixPlayback:
    def __init__(self, link, generation, spawn, failed):
        self.link, self.generation, self.spawn, self.failed = link, generation, spawn, failed
        self.buffer = self.task = None

    def attach_live(self, **kwargs):
        self.buffer = MatrixPlaybackBuffer(
            lambda: self.spawn(self.link.control("flush", generation=self.generation)),
            frame_bytes=960, sample_alignment=2, **kwargs)
        self.task = self.spawn(self._play())
        return self.buffer

    async def _play(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while self.buffer is not None:
                if delay := max(0, deadline - loop.time()):
                    await asyncio.sleep(delay)
                frame = self.buffer.read_frame()
                if frame:
                    await self.link.audio(self.generation, frame)
                deadline = max(deadline + 0.02, loop.time())
        except (ConnectionError, OSError, TimeoutError):
            self.spawn(self.failed(self.generation, "native_playback"))

    def detach_live(self):
        if self.task:
            self.task.cancel()
        self.buffer = None


@dataclass
class CallState:
    generation: int
    room_id: str
    joined: asyncio.Future
    owner: object
    home: Path
    source: object
    closed: bool = False
    session: object = None


class MatrixLiveMixin(LiveBackendMixin):
    def _init_matrix_live(self):
        from hermes_constants import get_hermes_home
        self._matrix_live_cfg = load_matrix_live_config()
        self._live_voice_home = Path(get_hermes_home())
        self._matrix_link = None
        self._matrix_states = {}
        self._gpt_live_sessions = {}
        self._matrix_live_tasks = set()
        self._matrix_live_lock = asyncio.Lock()
        self._matrix_live_closing = False
        self._matrix_live_restarted = False
        self._matrix_live_error = ""
        self._matrix_speech_ms = self._matrix_silence_ms = 0
        self._matrix_speech_active = False

    def _matrix_spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._matrix_live_tasks.add(task)

        def finished(done):
            self._matrix_live_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.warning("Matrix live voice task failed (%s)", type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def _connect_matrix_live(self):
        if not self._matrix_live_cfg.enabled:
            return
        self._matrix_live_closing = False
        if not self._encryption:
            self._matrix_live_error = "encrypted_chat_required"
            logger.warning("Matrix live voice requires encrypted chat support")
            return
        cfg = self._matrix_live_cfg
        helper = cfg.helper_path or self._live_voice_home / "matrix-call/bin/hermes-matrix-call"
        native = {"homeserver": self._homeserver, "user_id": self._user_id,
                  "allowed_user": cfg.user_id, "store": str(self._live_voice_home / "matrix-call/device")}
        if cfg.ca_file:
            native["ca_file"] = cfg.ca_file
        link = NativeCallLink(helper, self._live_voice_home, native,
                              on_event=self._matrix_native_event, on_audio=self._matrix_native_audio)
        self._matrix_link = link
        try:
            await link.start()
            self._matrix_live_error = ""
            logger.info("Matrix live voice ready (voice=%s)", cfg.voice)
        except asyncio.CancelledError:
            await link.close()
            raise
        except Exception as exc:  # health: allow BLE001 -- SDK/callback boundary records sanitized failure and preserves recovery; exception reprs can expose auth or private content
            self._matrix_live_error = type(exc).__name__
            logger.warning("Matrix live voice unavailable (%s)", type(exc).__name__)
            await link.close()
            if self._matrix_link is link:
                self._matrix_link = None

    async def _matrix_native_event(self, event):
        kind, generation = event.get("event"), event.get("generation")
        if kind == "incoming":
            self._matrix_spawn(self._start_matrix_call(event))
        elif kind in {"joined", "ended"}:
            state = self._matrix_states.get(generation)
            if state is None:
                return
            if kind == "joined":
                if not state.joined.done():
                    state.joined.set_result(True)
            else:
                state.closed = True
                if not state.joined.done():
                    state.joined.set_result(False)
                if state.session:
                    state.session.close_requested = True
                self._matrix_spawn(self._stop_matrix_call(generation, event.get("reason", "peer_left")))
        elif kind == "helper_ended" and not self._matrix_live_closing:
            for state in self._matrix_states.values():
                state.closed = True
                if not state.joined.done():
                    state.joined.set_result(False)
                if state.session:
                    state.session.close_requested = True
            self._matrix_spawn(self._matrix_helper_failed())

    async def _matrix_call_source(self, event):
        # Reuse Matrix's room, mention/thread policy and source builder. A call
        # addresses Hermes explicitly; its real ring event is a valid thread /
        # receipt anchor, unlike synthetic transcript IDs.
        user, room = event.get("user_id"), event.get("room_id")
        if user != self._matrix_live_cfg.user_id or not isinstance(room, str) or not room.startswith("!"):
            return None
        notification = event.get("notification_id")
        if not isinstance(notification, str) or not notification.startswith("$"):
            return None
        context = await self._resolve_message_context(room, user, notification, "", {}, {}, mention_claimed=True)
        if context is None:
            return None
        source = context[-1]
        identity = self._canonicalize(source)
        runner = getattr(self, "gateway_runner", None)
        if identity is None or runner is None or not runner._is_user_authorized_for_source(source):
            return None
        return source, identity.runtime_home

    async def _start_matrix_call(self, event):
        generation = event.get("generation")
        link = self._matrix_link
        if not isinstance(generation, int) or isinstance(generation, bool) or not link or self._matrix_live_closing:
            return
        async with self._matrix_live_lock:
            if generation in self._matrix_states:
                return
            resolved = await self._matrix_call_source(event)
            if resolved is None:
                await link.control("decline", generation=generation)
                return
            source, home = resolved
            owner = object()
            if not LiveVoiceLease.claim(home, owner):
                await link.control("decline", generation=generation)
                await self.send(source.chat_id, "I'm already in a voice call. Please try again when it ends.")
                return
            state = CallState(generation, source.chat_id, asyncio.get_running_loop().create_future(), owner, home, source)
            self._matrix_states[generation] = state
            try:
                await link.control("accept", generation=generation)
                joined = await asyncio.wait_for(asyncio.shield(state.joined), 40)
                if not joined or state.closed:
                    return

                async def dispatch(text):
                    event = MessageEvent(text=text, source=replace_source(source), message_type=MessageType.VOICE,
                                         user_id=source.user_id, user_name=source.user_name,
                                         raw_message={"matrix_live": True, "generation": generation})
                    # Normal Matrix ingress preserves identity, tools, memory,
                    # queued turns and authorisation; there is no transcript post.
                    await self.handle_message(event)

                with self.gateway_runner._profile_scope_for_source(source):
                    mixer = MatrixPlayback(link, generation, self._matrix_spawn, self._stop_matrix_call)
                    state.session = LiveVoiceSession(self, generation, source.chat_id, self._matrix_live_cfg,
                        mixer=mixer, dispatch=dispatch, input_to_pcm24=lambda pcm: pcm,
                        output_from_pcm24=lambda pcm: pcm,
                        usage_context={"platform": "matrix", "room_id": source.chat_id})
                    state.session.backend_session_key = self._source_session_key(source)
                    state.session.backend_source = source
                    self._gpt_live_sessions[generation] = state.session
                    await state.session.start()
                if not state.closed:
                    return
            except asyncio.CancelledError:
                state.closed = True
                raise
            except Exception as exc:  # health: allow BLE001 -- SDK/callback boundary records sanitized failure and preserves recovery; exception reprs can expose auth or private content
                self._matrix_live_error = type(exc).__name__
                logger.warning("Matrix live call failed (%s)", type(exc).__name__)
                if not state.closed:
                    await self.send(source.chat_id, "Live voice is unavailable. Check the gateway log.")
            finally:
                if state.closed or state.session is None or not state.session.is_live:
                    await self._close_matrix_state(state, "start_failed" if not state.closed else "peer_left")

    def _matrix_native_audio(self, generation, pcm):
        session = self._gpt_live_sessions.get(generation)
        if session is None or not session.is_live:
            return
        session.feed_user_pcm(self._matrix_live_cfg.user_id, pcm)
        import numpy as np
        samples = np.frombuffer(pcm, "<i2").astype(np.float32)
        duration = len(pcm) / 48  # mono PCM16, 24 kHz: bytes per millisecond
        if samples.size and float(np.mean(samples * samples)) > 300 ** 2:
            self._matrix_speech_ms += duration
            self._matrix_silence_ms = 0
            if not self._matrix_speech_active and self._matrix_speech_ms >= self._matrix_live_cfg.barge_in_min_speech_ms:
                self._matrix_speech_active = True
                session.note_user_speech()
        else:
            self._matrix_silence_ms += duration
            if self._matrix_silence_ms >= 200:
                self._matrix_speech_ms = 0
                self._matrix_speech_active = False

    def _gpt_live_session_ended(self, session, reason):
        if self._gpt_live_sessions.get(session.call_id) is session:
            self._matrix_spawn(self._stop_matrix_call(session.call_id, reason))

    async def _close_matrix_state(self, state, reason):
        state.closed = True
        try:
            if state.session:
                await state.session.close(reason)
            if self._matrix_link:
                with suppress(ConnectionError, OSError, TimeoutError):
                    await self._matrix_link.control("hangup", generation=state.generation)
        finally:
            self._gpt_live_sessions.pop(state.generation, None)
            self._matrix_states.pop(state.generation, None)
            LiveVoiceLease.release(state.home, state.owner)
            self._matrix_speech_ms = self._matrix_silence_ms = 0
            self._matrix_speech_active = False

    async def _stop_matrix_call(self, generation, reason):
        async with self._matrix_live_lock:
            if state := self._matrix_states.get(generation):
                await self._close_matrix_state(state, reason)

    async def _matrix_helper_failed(self):
        for generation in list(self._matrix_states):
            await self._stop_matrix_call(generation, "native_transport")
        link, self._matrix_link = self._matrix_link, None
        if link:
            await link.close()
        if not self._matrix_live_closing and not self._matrix_live_restarted:
            self._matrix_live_restarted = True
            await asyncio.sleep(2)
            await self._connect_matrix_live()

    async def _disconnect_matrix_live(self):
        if not hasattr(self, "_matrix_live_tasks"):
            return
        self._matrix_live_closing = True
        for state in self._matrix_states.values():
            state.closed = True
            if not state.joined.done():
                state.joined.set_result(False)
            if state.session:
                state.session.close_requested = True
        for generation in list(self._matrix_states):
            await self._stop_matrix_call(generation, "shutdown")
        link, self._matrix_link = self._matrix_link, None
        if link:
            await link.close()
        tasks = [task for task in self._matrix_live_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def matrix_live_diagnostics(self):
        return {"enabled": self._matrix_live_cfg.enabled, "ready": self._matrix_link is not None,
                "active_calls": len(self._gpt_live_sessions), "last_error": self._matrix_live_error}
