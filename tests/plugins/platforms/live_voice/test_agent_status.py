"""Actual native ownership, lifecycle correlation and voice publication invariants."""
import asyncio
import gc
import json
from types import SimpleNamespace
import weakref

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import set_session_vars, clear_session_vars
from plugins.platforms.live_voice.agent_status import observe_agent_tool, observe_agent_lifecycle
from plugins.platforms.live_voice.backend import LiveBackendMixin
from plugins.platforms.live_voice.session import DELEGATION_CONTEXT, LiveVoiceConfig, LiveVoiceSession, PendingDelegation
from tools.delegate_tool import _register_subagent, _unregister_subagent


class Agent:
    def __init__(self, sid, db, parent=None):
        self.session_id, self._session_db = sid, db
        self._delegate_depth = 0 if parent is None else parent._delegate_depth + 1
        self._parent_session_id = None if parent is None else parent.session_id
        self._delegate_parent_ref = None if parent is None else weakref.ref(parent)
        self.tool, self.calls = 'web_search', 1

    def get_activity_summary(self):
        return {'current_tool': self.tool, 'api_call_count': self.calls, 'args': 'private args'}


class Binding(LiveBackendMixin):
    def __init__(self, home, platform):
        self.home, self.authorized, self.current_id = home, True, 'parent'
        self.aliases = {}
        def resolve(sid):
            while sid in self.aliases:
                sid = self.aliases[sid]
            return sid
        self.db = SimpleNamespace(resolve_resume_session_id=resolve)
        self.source = SessionSource(platform=platform, chat_id='chat', user_id='ben', chat_type='dm')
        self._gpt_live_sessions = {}
        self.gateway_runner = SimpleNamespace(_is_user_authorized_for_source=lambda _: self.authorized)
        self._session_store = SimpleNamespace(peek_session_id=lambda _: self.current_id)
        self.wire = []

    def _canonicalize(self, source):
        return SimpleNamespace(runtime_home=self.home)

    def _source_session_key(self, source):
        return f'{source.platform}:{source.chat_id}:{source.user_id}:{source.thread_id}'

    def session(self):
        s = LiveVoiceSession(self, 1, 'chat', LiveVoiceConfig(user_id='ben'), mixer=None, dispatch=None,
                            input_to_pcm24=lambda x: x, output_from_pcm24=lambda x: x)
        s.backend_source, s.backend_session_key = self.source, self._source_session_key(self.source)
        s._accepting_audio = True
        async def send(text):
            self.wire.append(json.loads(text))
        s._ws = SimpleNamespace(send=send)
        self._gpt_live_sessions[1] = s
        return s


def register(ident, child, *, goal='Research task', parent_id=None):
    _register_subagent({'subagent_id': ident, 'agent': child, 'parent_id': parent_id,
        'owner_agent_session_id': child._parent_session_id, 'goal': goal, 'model': 'test',
        'status': 'running', 'started_at': 1, 'tool_count': 2, 'last_tool': 'terminal',
        'live_transcript': '/private/transcript'})


async def lifecycle(parent, child, ident=None, **extra):
    observe_agent_lifecycle(parent_session_id=parent.session_id, child_session_id=child.session_id,
                            **({'child_subagent_id': ident, 'child_goal': 'Research task'} if ident else {}), **extra)
    # Flush the thread-safe lifecycle handoff, without a timing-dependent sleep.
    done = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(done.set_result, None)
    await done


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', [Platform.DISCORD, Platform.MATRIX])
async def test_roster_tracks_all_owned_workers_with_throttled_factual_updates(tmp_path, monkeypatch, platform):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    binding = Binding(tmp_path, platform)
    session = binding.session()
    parent = Agent('parent', binding.db)
    child = Agent('child', binding.db, parent)
    grandchild = Agent('grandchild', binding.db, child)
    foreign_parent = Agent('another-conversation', binding.db)
    foreign = Agent('foreign', binding.db, foreign_parent)
    token = DELEGATION_CONTEXT.set((session, 'voice-request'))
    vars_tokens = set_session_vars(session_key=session.backend_session_key)
    session.pending['new-request'] = PendingDelegation('new-request', asyncio.get_running_loop().create_future())
    try:
        with bind_subagent_parent(parent):
            observe_agent_tool(tool_name='delegate_task')
        await lifecycle(parent, child, 'worker-1')
        await lifecycle(child, grandchild, 'worker-2', parent_subagent_id='worker-1')
        for ident, agent in [('worker-1', child), ('worker-2', grandchild), ('foreign', foreign)]:
            register(ident, agent, goal='Research Bearer abcdefghijklmnopqrstuvwxyz012345',
                     parent_id='worker-1' if agent is grandchild else None)
        await session.agent_status.tick(now=10)
        rows = {r['id']: r for r in session.agent_status.last_snapshot['agents']}
        assert set(rows) == {'worker-1', 'worker-2'}
        assert rows['worker-1']['current_tool'] == 'web_search' and rows['worker-1']['last_tool'] == 'terminal'
        assert all(e['type'] == 'session.thinking.append' and e['delegation_id'] is None for e in binding.wire)
        assert not any(x in json.dumps(binding.wire) for x in ('private args', '/private/transcript', 'abcdefghijklmnopqrstuvwxyz012345'))
        session.agent_status.wake.clear()
        with bind_subagent_parent(parent):
            for _ in range(20):
                observe_agent_tool(tool_name='terminal')
        barrier = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(barrier.set_result, None)
        await barrier
        assert not session.agent_status.wake.is_set()  # Tool volume cannot create a roster-poll storm.
        quiet_count = len(binding.wire)
        await session.agent_status.tick(now=20)
        assert len(binding.wire) == quiet_count  # No unchanged roster spam.
        child.calls = 3
        await session.agent_status.tick(now=30)
        assert len(binding.wire) > quiet_count and all(e['type'] != 'session.commentary.append' for e in binding.wire)
        delta = ''.join(e['content'] for e in binding.wire[quiet_count:])
        assert 'worker-1' in delta and 'worker-2' not in delta
        await session.agent_status.tick(now=55)
        assert any(e['type'] == 'session.commentary.append' and '2 workers' in e['content'] for e in binding.wire)
        assert len({e['event_id'] for e in binding.wire}) == len(binding.wire)
        assert not session.pending['new-request'].future.done()

        # Native absence alone never claims success. The real stop hook supplies outcome.
        _unregister_subagent('worker-1')
        await session.agent_status.tick(now=60)
        assert next(r for r in session.agent_status.last_snapshot['agents'] if r['id'] == 'worker-1')['state'] == 'awaiting_report'
        await lifecycle(parent, child, child_status='timeout', child_summary='private result', tool_call_history='private tools')
        await session.agent_status.tick(now=65)
        assert next(r for r in session.agent_status.last_snapshot['agents'] if r['id'] == 'worker-1')['state'] == 'timeout'
        assert 'private result' not in json.dumps(binding.wire) and 'private tools' not in json.dumps(binding.wire)

        for change in ('authorization', 'profile', 'user', 'conversation'):
            before = len(binding.wire)
            old_home, old_user, old_key = binding.home, session.backend_source.user_id, session.backend_session_key
            if change == 'authorization': binding.authorized = False
            if change == 'profile': binding.home = tmp_path / 'other'
            if change == 'user': session.backend_source.user_id = 'other'
            if change == 'conversation': session.backend_session_key = 'other'
            child.calls += 1
            await session.agent_status.tick(now=200)
            assert len(binding.wire) == before
            binding.authorized, binding.home = True, old_home
            session.backend_source.user_id, session.backend_session_key = old_user, old_key
    finally:
        for ident in ('worker-1', 'worker-2', 'foreign'):
            _unregister_subagent(ident)
        clear_session_vars(vars_tokens)
        DELEGATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_lifecycle_survives_compression_redial_and_cannot_cross_new_or_send_races(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    binding = Binding(tmp_path, Platform.DISCORD)
    session = binding.session()
    parent = Agent('parent', binding.db)
    child = Agent('child', binding.db, parent)
    token = DELEGATION_CONTEXT.set((session, 'old-request'))
    vars_tokens = set_session_vars(session_key=session.backend_session_key)
    try:
        with bind_subagent_parent(parent):
            observe_agent_tool(tool_name='delegate_task')
        await lifecycle(parent, child, 'quick-worker')
        roster = binding._gpt_live_agent_roster(session)
        assert roster.total_seen == 1
        binding.aliases.update(parent='parent-compressed', child='child-compressed')
        parent.session_id, child.session_id = 'parent-compressed', 'child-compressed'
        binding.current_id = parent.session_id
        session._accepting_audio = False
        replacement = binding.session()
        await lifecycle(parent, child, child_status='completed')  # Original closed-call context.
        assert binding._gpt_live_agent_roster(replacement) is roster
        parent_ref = weakref.ref(parent)
        del parent
        gc.collect()
        assert parent_ref() is None  # Tracker does not retain AIAgents.
        await replacement.agent_status.tick(now=10)
        assert replacement.agent_status.last_snapshot['agents'][0]['state'] == 'completed'
        assert roster.total_seen == 1  # No registry poll was needed for this quick job.

        # Publication checks inside the send lock, including a /new while waiting.
        live_parent = Agent('parent-compressed', binding.db)
        live_child = Agent('live-child', binding.db, live_parent)
        await lifecycle(live_parent, live_child, 'active-worker')
        started_send = asyncio.Event()
        original_send = replacement._send
        async def blocked_send(*args, **kwargs):
            started_send.set()
            return await original_send(*args, **kwargs)
        replacement._send = blocked_send
        async with replacement._send_lock:
            tick = asyncio.create_task(replacement.agent_status.tick(now=200))
            await asyncio.wait_for(started_send.wait(), 5)
            before_send = len(binding.wire)
            binding.current_id = 'brand-new'
        await tick
        assert len(binding.wire) == before_send
        assert binding._gpt_live_agent_roster(replacement).rows == {}
        assert roster.epoch == 1
        before = len(binding.wire)
        # Late old context cannot reset the new epoch or resurrect a child.
        old_parent = Agent('parent-compressed', binding.db)
        with bind_subagent_parent(old_parent):
            observe_agent_tool(tool_name='delegate_task')
        await lifecycle(old_parent, child, 'late-worker')
        assert roster.root_id == 'brand-new' and roster.rows == {}
        await session.agent_status.tick(now=300)
        assert len(binding.wire) == before  # Closed caller never publishes.
    finally:
        clear_session_vars(vars_tokens)
        DELEGATION_CONTEXT.reset(token)
