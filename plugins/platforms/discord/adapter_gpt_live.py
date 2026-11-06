"""Discord lifecycle and final-reply tap for the optional GPT-Live channel."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import logging

from .gpt_live import GptLiveConfig, GptLiveSession, load_gpt_live_config
from plugins.platforms.live_voice.backend import LiveBackendMixin
from plugins.platforms.live_voice.lease import LiveVoiceLease

logger = logging.getLogger(__name__)


class DiscordGptLiveMixin(LiveBackendMixin):
    def _init_gpt_live(self):
        from hermes_constants import get_hermes_home
        self._live_voice_home = get_hermes_home()
        self._gpt_live_leases = {}
        self._gpt_live_cfg = load_gpt_live_config()
        self._gpt_live_sessions = {}
        self._gpt_live_locks = {}
        self._gpt_live_wanted = set()
        self._gpt_live_tasks = set()
        self._gpt_live_retried = set()
        self._gpt_live_connecting = set()

    def _gpt_live_spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._gpt_live_tasks.add(task)

        def finished(done):
            self._gpt_live_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.warning("GPT-Live lifecycle failed (%s)", type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    def _gpt_live_member_present(self, channel):
        cfg = self._gpt_live_cfg
        member = channel.guild.get_member(int(cfg.user_id))
        voice = getattr(member, "voice", None)
        return bool(voice and voice.channel and str(voice.channel.id) == cfg.voice_channel_id)

    async def _reconcile_gpt_live(self):
        cfg = getattr(self, "_gpt_live_cfg", GptLiveConfig())
        if not cfg.enabled:
            return
        channel = self._client.get_channel(int(cfg.voice_channel_id))
        if channel is None:
            logger.warning("GPT-Live voice channel is not visible to this bot")
            return
        if self._gpt_live_member_present(channel):
            self._gpt_live_wanted.add(channel.guild.id)
            await self._start_gpt_live(channel)

    def _gpt_live_voice_state(self, member, before, after):
        cfg = getattr(self, "_gpt_live_cfg", GptLiveConfig())
        if not cfg.enabled:
            return
        gid = member.guild.id
        was = bool(before.channel and str(before.channel.id) == cfg.voice_channel_id)
        now = bool(after.channel and str(after.channel.id) == cfg.voice_channel_id)
        if member.id == self._client.user.id:
            if (was and not now and gid in self._gpt_live_sessions
                    and gid not in self._gpt_live_connecting):
                self._gpt_live_request_stop(gid, "bot_moved")
            return
        if str(member.id) != cfg.user_id:
            return
        if now and not was:
            self._gpt_live_wanted.add(gid)
            self._gpt_live_retried.discard(gid)
            self._gpt_live_spawn(self._start_gpt_live(after.channel))
        elif was and not now:
            self._gpt_live_request_stop(gid, "user_left")

    def _gpt_live_request_stop(self, guild_id, reason):
        self._gpt_live_wanted.discard(guild_id)
        session = self._gpt_live_sessions.get(guild_id)
        if session:
            session.close_requested = True
            session.close_requested_reason = reason
            logger.info("GPT-Live close requested (guild=%s reason=%s)", guild_id, reason)
        self._gpt_live_spawn(self._stop_gpt_live(guild_id, reason))

    async def _start_gpt_live(self, channel, *, attempts=2):
        gid, cfg = channel.guild.id, self._gpt_live_cfg
        async with self._gpt_live_locks.setdefault(gid, asyncio.Lock()):
            if (not cfg.enabled or gid in self._gpt_live_sessions
                    or gid not in self._gpt_live_wanted or not self._gpt_live_member_present(channel)):
                return
            if not self._is_allowed_user(cfg.user_id, guild=channel.guild, is_dm=False):
                logger.warning("GPT-Live configured user is not authorized by this Discord adapter")
                return
            text_id = cfg.text_channel_id or cfg.voice_channel_id
            self._voice_sources.pop(gid, None)
            runner = getattr(self, "gateway_runner", None)
            home, source = self._live_voice_home, None
            if runner and callable(getattr(runner, "_voice_input_source", None)):
                source = runner._voice_input_source(self, gid, int(cfg.user_id), text_id)
                identity = self._canonicalize(source)
                if identity is None or not runner._is_user_authorized_for_source(source):
                    return
                home = identity.runtime_home
            owner = object()
            if not LiveVoiceLease.claim(home, owner):
                self._gpt_live_wanted.discard(gid)
                logger.info("Live voice is busy in another transport for this profile")
                text_channel = self._client.get_channel(int(text_id))
                if text_channel:
                    await text_channel.send("I'm already in a voice call. Please try again when it ends.")
                return
            self._gpt_live_leases[gid] = (home, owner)
            established, session = False, None
            try:
                for attempt in range(attempts):
                    scope = runner._profile_scope_for_source(source) if source else nullcontext()
                    with scope:
                        session = GptLiveSession(self, gid, channel, text_id, cfg)
                        if source is not None:
                            session.backend_session_key = self._source_session_key(source)
                            session.backend_source = source
                        self._gpt_live_sessions[gid] = session
                        self._voice_text_channels[gid] = int(text_id)
                        try:
                            self._gpt_live_connecting.add(gid)
                            try:
                                joined = await self.join_voice_channel(channel, text_channel_id=int(text_id), live_mode=True)
                            finally:
                                self._gpt_live_connecting.discard(gid)
                            if not joined:
                                raise RuntimeError("Discord voice connection failed")
                            session.mixer = self._voice_mixers[gid]
                            if gid not in self._gpt_live_wanted:
                                session.close_requested = True
                            await session.start()
                            if not session.close_requested and gid in self._gpt_live_wanted:
                                established = True
                                return
                        except Exception as exc:  # health: allow BLE001 -- SDK/callback boundary records sanitized failure and preserves recovery; exception reprs can expose auth or private content
                            # WebSocket exception reprs can contain auth headers.
                            logger.warning("GPT-Live startup failed (%s)", type(exc).__name__)
                            if await self._retry_gpt_live_start(session, channel, attempt, attempts):
                                continue
                            await self._gpt_live_notice(text_id)
                        return
            finally:
                if not established:
                    try:
                        if session:
                            reason = session.close_requested_reason or (
                                "start_failed" if gid in self._gpt_live_wanted else "user_left")
                            await session.close(reason)
                    finally:
                        self._gpt_live_sessions.pop(gid, None)
                        try:
                            await self.leave_voice_channel(gid)
                        finally:
                            LiveVoiceLease.release(home, owner)
                            self._gpt_live_leases.pop(gid, None)

    async def _retry_gpt_live_start(self, session, channel, attempt, attempts):
        gid = channel.guild.id
        if attempt + 1 >= attempts or gid not in self._gpt_live_wanted:
            return False
        await session.close("start_failed")
        await asyncio.sleep(2)
        return self._gpt_live_member_present(channel) and gid in self._gpt_live_wanted

    async def _gpt_live_notice(self, text_id, message=None):
        try:
            channel = self._client.get_channel(int(text_id))
            if channel:
                await channel.send(message or "Live voice is unavailable. Check the gateway log and OpenAI project access.")
        except Exception:  # health: allow BLE001 -- SDK/callback boundary records sanitized failure and preserves recovery; exception reprs can expose auth or private content
            logger.warning("Could not post GPT-Live status to its text channel")

    async def _stop_gpt_live(self, guild_id, reason):
        async with self._gpt_live_locks.setdefault(guild_id, asyncio.Lock()):
            session = self._gpt_live_sessions.get(guild_id)
            if not session:
                return
            await session.close(reason)
            self._gpt_live_sessions.pop(guild_id, None)
            try:
                await self.leave_voice_channel(guild_id)
            finally:
                lease = self._gpt_live_leases.pop(guild_id, None)
                if lease is not None:
                    LiveVoiceLease.release(*lease)

    def _gpt_live_session_ended(self, session, reason):
        if self._gpt_live_sessions.get(session.guild_id) is session:
            self._gpt_live_spawn(self._gpt_live_handle_end(session, reason))

    def _gpt_live_receive_failed(self, session):
        # Called on the event loop, never the UDP reader thread. Ignore a late
        # failure from a receiver belonging to a call that already ended.
        if self._gpt_live_sessions.get(session.guild_id) is session and not session.close_requested:
            self._gpt_live_spawn(self._gpt_live_handle_end(session, "microphone_transport"))

    async def _gpt_live_handle_end(self, session, reason):
        gid = session.guild_id
        # This task may have been queued during a failed startup that already retried.
        if self._gpt_live_sessions.get(gid) is not session:
            return
        reconnect = (reason in {"transport", "microphone_transport"} and gid not in self._gpt_live_retried
                     and gid in self._gpt_live_wanted and not session.close_requested)
        if reconnect:
            self._gpt_live_retried.add(gid)
        else:
            self._gpt_live_wanted.discard(gid)
        await self._stop_gpt_live(gid, reason)
        if reconnect:
            await asyncio.sleep(2)
            if gid in self._gpt_live_wanted and self._gpt_live_member_present(session.voice_channel):
                await self._start_gpt_live(session.voice_channel, attempts=1)
        elif reason == "microphone_transport":
            await self._gpt_live_notice(session.chat_id,
                "I couldn't receive your microphone audio after reconnecting. Please leave and rejoin the voice channel.")

    async def _shutdown_gpt_live(self):
        if not hasattr(self, "_gpt_live_tasks"):
            return
        self._gpt_live_wanted.clear()
        for session in list(self._gpt_live_sessions.values()):
            session.close_requested = True
            await self._stop_gpt_live(session.guild_id, "shutdown")
        tasks = [t for t in self._gpt_live_tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
