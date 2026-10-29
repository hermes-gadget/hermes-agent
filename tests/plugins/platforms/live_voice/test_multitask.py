"""Concurrent voice requests and conversation-owned background result delivery."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.live_voice.backend import BackgroundReply, LiveBackendMixin
from plugins.platforms.live_voice.session import (
    DELEGATION_CONTEXT, LiveVoiceConfig, LiveVoiceSession, PendingDelegation,
)


def session_for(adapter, chat="33", user="22"):
    cfg = LiveVoiceConfig(user_id=user, summarize_for_voice=False)
    return LiveVoiceSession(adapter, 1, chat, cfg, mixer=None, dispatch=None,
        input_to_pcm24=lambda p: p, output_from_pcm24=lambda p: p)


@pytest.mark.asyncio
async def test_independent_requests_finish_out_of_order_with_bounded_admission_and_cleanup():
    session = session_for(SimpleNamespace(_gpt_live_session_ended=Mock()))
    session._accepting_audio = True
    started = {str(i): asyncio.Event() for i in range(1, 7)}
    release = {did: asyncio.Event() for did in started}
    replies = asyncio.Queue()

    async def dispatch(text):
        owner, did = DELEGATION_CONTEXT.get()
        assert owner is session and text == f"job {did}"
        started[did].set()
        await release[did].wait()
        session.complete_pending(f"finished {did}", did)

    async def send(event):
        await replies.put(event)

    session._dispatch, session._send = dispatch, send
    for i in range(1, 7):
        session._transcript_seq = i
        session._transcripts.append((i, f"job {i}", 0))
        session._delegations.put_nowait((str(i), i, 0))
    # A settled transcript boundary; tests synchronize on events, not sleeps.
    session._last_transcript_at = 1
    worker = asyncio.create_task(session._delegation_worker())
    try:
        await asyncio.wait_for(asyncio.gather(*(started[str(i)].wait() for i in range(1, 5))), 5)
        assert not started['5'].is_set() and not started['6'].is_set()
        release['2'].set()
        reply = await asyncio.wait_for(replies.get(), 5)
        assert reply['delegation_id'] == '2' and reply['content'] == 'finished 2'
        await asyncio.wait_for(started['5'].wait(), 5)
        assert not started['6'].is_set()
        assert not session.pending['1'].future.done()
        release['1'].set()
        reply = await asyncio.wait_for(replies.get(), 5)
        assert reply['delegation_id'] == '1' and reply['content'] == 'finished 1'
        await asyncio.wait_for(started['6'].wait(), 5)
        assert not session.pending['3'].future.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert not session.pending


class Transport:
    async def handle_message(self, event):
        self.admitted.append(event)

    async def _process_message_background(self, event, session_key):
        final = 'Clock job: server time checked. Research job: comparison finished.'
        self.presented.append(final)  # Normal chat delivery always remains available.
        self._gpt_live_tap_reply(event.source.chat_id, final, {'notify': True})
        event._streamed_final_response = final
        self._gpt_live_complete_stream(event)


class Binding(LiveBackendMixin, Transport):
    def __init__(self, home):
        self.admitted, self.presented = [], []
        self.home, self.authorized = home, True
        self._gpt_live_sessions = {}
        self.gateway_runner = SimpleNamespace(_is_user_authorized_for_source=lambda _: self.authorized)

    def _canonicalize(self, source):
        return SimpleNamespace(runtime_home=self.home)

    def _event_session_key(self, event):
        s = event.source
        return (s.platform, s.chat_id, s.thread_id, s.user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform,user,chat', [
    (Platform.DISCORD, '22', '33'), (Platform.MATRIX, '@ben:test', '!dm:test'),
])
async def test_background_results_preserve_requests_scope_mute_and_redial(tmp_path, platform, user, chat):
    binding = Binding(tmp_path)
    session = session_for(binding, chat, user)
    session.home = tmp_path
    session._accepting_audio = True
    binding._gpt_live_sessions[1] = session

    def event(**kwargs):
        source = SessionSource(platform=platform, chat_id=chat, user_id=user, chat_type='dm')
        return MessageEvent(text='two background jobs completed', source=source, internal=True,
            metadata={'notification_origin': 'process_registry_synthetic'}, **kwargs)

    session.backend_session_key = binding._event_session_key(event())
    pending = PendingDelegation('new', asyncio.get_running_loop().create_future())
    session.pending['new'] = pending
    token = DELEGATION_CONTEXT.set((session, 'new'))
    try:
        voice = MessageEvent(text='Please research two topics', source=event().source,
                             channel_context='Existing room context')
        await binding.handle_message(voice)
        assert voice.queue_only and voice.text == 'Please research two topics'
        assert voice.channel_context.startswith('Existing room context')
        once = voice.channel_context
        await binding.handle_message(voice)
        assert voice.channel_context == once

        completion = event()
        await binding.handle_message(completion)
        assert isinstance(completion._gpt_live_delegation[1], BackgroundReply)
        await binding._process_message_background(completion, session.backend_session_key)
        did, content, final = await asyncio.wait_for(session._backend_updates.get(), 5)
        assert did is None and final and 'Clock job' in content and 'Research job' in content
        assert session._backend_updates.empty() and not pending.future.done()

        # Exercise the actual wire sink: one unsolicited report, no current-task ID.
        wire = asyncio.Queue()
        async def send(value):
            await wire.put(value)
        session._send = send
        session._backend_updates.put_nowait((did, content, final))
        updates = asyncio.create_task(session._send_backend_updates())
        try:
            report = await asyncio.wait_for(wire.get(), 5)
            assert report['type'] == 'session.commentary.append' and report['delegation_id'] is None
            assert not pending.future.done()
        finally:
            updates.cancel()
            await asyncio.gather(updates, return_exceptions=True)

        for kind in ['other_user', 'missing_user', 'other_profile', 'other_thread', 'unauthorized', 'untrusted']:
            wrong = event()
            if kind == 'other_user': wrong.source.user_id = 'someone-else'
            if kind == 'missing_user': wrong.source.user_id = None
            if kind == 'other_profile': binding.home = tmp_path / 'another-profile'
            if kind == 'other_thread': wrong.source.thread_id = 'another-thread'
            if kind == 'unauthorized': binding.authorized = False
            if kind == 'untrusted': wrong.metadata['notification_origin'] = 'untrusted'
            await binding.handle_message(wrong)
            assert not hasattr(wrong, '_gpt_live_delegation')
            binding.home, binding.authorized = tmp_path, True

        muted = event()
        await binding.handle_message(muted)
        muted._notification_reply_muted = True  # Decided after ingress by the owning run.
        await binding._process_message_background(muted, session.backend_session_key)
        assert session._backend_updates.empty() and not pending.future.done()

        queued = event()
        await binding.handle_message(queued)
        replacement = session_for(binding, chat, user)
        replacement.home, replacement._accepting_audio = tmp_path, True
        replacement.backend_session_key = session.backend_session_key
        binding._gpt_live_sessions[1] = replacement
        await binding._process_message_background(queued, session.backend_session_key)
        assert replacement._backend_updates.empty() and session._backend_updates.empty()
        assert len(binding.presented) == 3  # Text survives the redial.

        # Jobs belong to the conversation: completion arriving after a redial
        # may be announced on the new call, without reviving the previous one.
        later = event()
        await binding.handle_message(later)
        await binding._process_message_background(later, replacement.backend_session_key)
        assert (await asyncio.wait_for(replacement._backend_updates.get(), 5))[0] is None
        assert not pending.future.done()
    finally:
        DELEGATION_CONTEXT.reset(token)
