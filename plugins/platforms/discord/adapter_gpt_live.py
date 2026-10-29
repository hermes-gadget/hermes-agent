"""Discord lifecycle and final-reply tap for the optional GPT-Live channel."""

from __future__ import annotations

import asyncio
import logging

from .gpt_live import DELEGATION_CONTEXT, GptLiveConfig, GptLiveSession, load_gpt_live_config

logger = logging.getLogger(__name__)


class DiscordGptLiveMixin:
    def _init_gpt_live(self):
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

    def _gpt_live_active_for_chat(self, chat_id):
        return any(session.text_channel_id == str(chat_id)
                   for session in getattr(self, "_gpt_live_sessions", {}).values())

    def _gpt_live_tap_reply(self, chat_id, content, metadata):
        if not (content or "").strip() or not metadata or not metadata.get("notify") or metadata.get("_interim_send"):
            return
        context = DELEGATION_CONTEXT.get()
        if context:
            session, did = context
            if (session.text_channel_id == str(chat_id)
                    and getattr(self, "_gpt_live_sessions", {}).get(session.guild_id) is session):
                session.complete_pending(content, did)

    def _gpt_live_text_failure(self, chat_id):
        sessions = [s for s in getattr(self, "_gpt_live_sessions", {}).values()
                    if s.text_channel_id == str(chat_id)]
        for session in sessions:
            if not getattr(session, "text_failure_logged", False):
                session.text_failure_logged = True
                logger.warning("GPT-Live text delivery failed; check View Channel and Send Messages permissions")
        return bool(sessions)

    def _gpt_live_complete_stream(self, event):
        # The gateway seals streamed responses by edit and can omit send(notify=True).
        # Its processing-complete event carries the authoritative full final text.
        final = getattr(event, "_streamed_final_response", None)
        if final:
            self._gpt_live_tap_reply(event.source.chat_id, final, {"notify": True})

    async def handle_message(self, event):
        context = DELEGATION_CONTEXT.get()
        if context and context[0].text_channel_id == str(event.source.chat_id):
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
        if self._gpt_live_active_for_chat(chat_id) or (context and context[0].text_channel_id == str(chat_id)):
            return False
        return super()._should_auto_tts_for_chat(chat_id)

    def _streaming_tts_turn_completed(self, session_key, turn_marker=None, *, event=None):
        # The voice frontend owns speech for this turn, including after a close.
        # This also suppresses the runner's explicit /voice all fallback.
        context = DELEGATION_CONTEXT.get() or getattr(event, "_gpt_live_delegation", None)
        if context:
            return True
        return super()._streaming_tts_turn_completed(session_key, turn_marker, event=event)

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
            for attempt in range(attempts):
                session = GptLiveSession(self, gid, channel, text_id, cfg)
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
                        return
                except Exception as exc:
                    # Exception reprs from WebSocket transports can contain headers.
                    logger.warning("GPT-Live startup failed (%s)", type(exc).__name__)
                    if attempt + 1 < attempts and gid in self._gpt_live_wanted:
                        await session.close("start_failed")
                        await asyncio.sleep(2)
                        if self._gpt_live_member_present(channel) and gid in self._gpt_live_wanted:
                            continue
                    await self._gpt_live_notice(text_id)
                reason = session.close_requested_reason or (
                    "start_failed" if gid in self._gpt_live_wanted else "user_left")
                await session.close(reason)
                self._gpt_live_sessions.pop(gid, None)
                await self.leave_voice_channel(gid)
                return

    async def _gpt_live_notice(self, text_id):
        try:
            channel = self._client.get_channel(int(text_id))
            if channel:
                await channel.send("Live voice is unavailable. Check the gateway log and OpenAI project access.")
        except Exception:
            logger.warning("Could not post GPT-Live status to its text channel")

    async def _stop_gpt_live(self, guild_id, reason):
        async with self._gpt_live_locks.setdefault(guild_id, asyncio.Lock()):
            session = self._gpt_live_sessions.get(guild_id)
            if not session:
                return
            await session.close(reason)
            self._gpt_live_sessions.pop(guild_id, None)
            await self.leave_voice_channel(guild_id)

    def _gpt_live_session_ended(self, session, reason):
        if self._gpt_live_sessions.get(session.guild_id) is session:
            self._gpt_live_spawn(self._gpt_live_handle_end(session, reason))

    async def _gpt_live_handle_end(self, session, reason):
        gid = session.guild_id
        # This task may have been queued during a failed startup that already retried.
        if self._gpt_live_sessions.get(gid) is not session:
            return
        reconnect = (reason == "transport" and gid not in self._gpt_live_retried
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
