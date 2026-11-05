"""Enforced worker ownership, fresh requests and the retained summary route."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from gateway.session_context import clear_session_vars, set_session_vars
from plugins.platforms.live_voice.session import DELEGATION_CONTEXT, LiveVoiceConfig, LiveVoiceSession


class Parent:
    def __init__(self, depth=0):
        self._delegate_depth = depth
        self.session_id = f'parent-{depth}'


def test_discovered_voice_policy_blocks_parent_work_but_allows_children_and_other_sessions(tmp_path, monkeypatch):
    from hermes_cli import plugins
    import model_tools
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    directory = tmp_path / 'plugins' / 'voice-policy-test'
    directory.mkdir(parents=True)
    (directory / 'plugin.yaml').write_text(json.dumps({'name': 'voice-policy-test', 'version': '0.1.0'}))
    (directory / '__init__.py').write_text(
        'from plugins.platforms.live_voice.coordination import enforce_voice_coordination\n'
        'def register(ctx):\n'
        '    ctx.register_hook("pre_tool_call", enforce_voice_coordination)\n')
    (tmp_path / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['voice-policy-test']}}))
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    manager.discover_and_load()
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: manager)
    executed = []
    def execute(*args, **kwargs):
        executed.append(args[0])
        return json.dumps({'executed': True})
    monkeypatch.setattr(model_tools, '_execute_tool', execute)
    voice = SimpleNamespace(home=tmp_path, backend_session_key='voice-key', is_live=False)
    token = DELEGATION_CONTEXT.set((voice, 'accepted-before-hangup'))
    session_tokens = set_session_vars(session_key='voice-key')
    parent, child = Parent(), Parent(1)
    try:
        with bind_subagent_parent(parent):
            result = model_tools.handle_function_call('voice_worker_probe', {})
            assert 'delegate_task' in result and not executed
            assert plugins.get_pre_tool_call_block_message('delegate_task', {'goal': 'check clock'}) is None
            assert plugins.get_pre_tool_call_block_message('clarify', {'question': 'Which server?'}) is None
            from agent.delegation_context import delegated_child_context
            with delegated_child_context('inherited-marker'):
                assert 'delegate_task' in model_tools.handle_function_call('voice_worker_probe', {})
                assert not executed  # An ambient marker cannot turn a real root into a worker.
        with bind_subagent_parent(child):
            assert json.loads(model_tools.handle_function_call('voice_worker_probe', {}))['executed']
        # An inherited voice context must not restrict another profile or conversation.
        with bind_subagent_parent(parent):
            voice.home = tmp_path / 'other-profile'
            assert json.loads(model_tools.handle_function_call('voice_worker_probe', {}))['executed']
            voice.home, voice.backend_session_key = tmp_path, 'other-conversation'
            assert json.loads(model_tools.handle_function_call('voice_worker_probe', {}))['executed']
        assert len(executed) == 3
    finally:
        clear_session_vars(session_tokens)
        DELEGATION_CONTEXT.reset(token)
        manager.unload()


@pytest.mark.asyncio
async def test_consumed_requests_and_standalone_holds_never_dispatch_or_summarize(monkeypatch):
    import gateway.voice_summary as summary_module
    summary = AsyncMock(return_value='Clock check confirmed.')
    monkeypatch.setattr(summary_module, 'summarize_for_voice', summary)
    session = LiveVoiceSession(SimpleNamespace(), 1, 'chat', LiveVoiceConfig(user_id='ben'),
        mixer=None, dispatch=None, input_to_pcm24=lambda p: p, output_from_pcm24=lambda p: p)
    session._accepting_audio = True
    session._last_transcript_at = 1
    session._transcripts.append((1, 'Check the clock', 0))
    session._transcript_seq = session._consumed_seq = 1
    sent, dispatched = asyncio.Queue(), []
    async def send(event):
        await sent.put(event)
    async def dispatch(text):
        dispatched.append(text)
        owner, did = DELEGATION_CONTEXT.get()
        owner.complete_pending('Clock job: verified the server time.', did)
    session._send, session._dispatch = send, dispatch
    worker = asyncio.create_task(session._delegation_worker())
    try:
        session._delegations.put_nowait(('repeated', 1, 0))
        quiet = await asyncio.wait_for(sent.get(), 5)
        assert quiet['type'] == 'session.thinking.append' and quiet['delegation_id'] == 'repeated'
        for seq, text in [(2, 'One moment, Hermes.'), (3, 'Okay, just a second please')]:
            session._transcript_seq = seq
            session._transcripts.append((seq, text, 0))
            session._delegations.put_nowait((str(seq), seq, 0))
            assert (await asyncio.wait_for(sent.get(), 5))['type'] == 'session.thinking.append'
        assert not dispatched and summary.await_count == 0 and not session.pending

        # A hold phrase inside an actual instruction must remain actionable.
        session._transcript_seq = 4
        request = 'Wait one moment before checking the server time'
        session._transcripts.append((4, request, 0))
        session._delegations.put_nowait(('work', 4, 0))
        reply = await asyncio.wait_for(sent.get(), 5)
        assert reply['type'] == 'session.commentary.append' and reply['delegation_id'] == 'work'
        assert dispatched == [request]
        summary.assert_awaited_once_with('Clock job: verified the server time.', preserve_style=True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_automatic_ack_is_suppressed_before_transcript_settlement_and_duplicate_events():
    session = LiveVoiceSession(SimpleNamespace(), 1, 'chat', LiveVoiceConfig(), mixer=None,
        dispatch=None, input_to_pcm24=lambda p: p, output_from_pcm24=lambda p: p)
    sent = asyncio.Queue()
    settling, release = asyncio.Event(), asyncio.Event()
    async def settle(through_seq):
        settling.set()
        await release.wait()
        return ''
    async def send(event):
        await sent.put(event)
    session._settled_request, session._send = settle, send
    ack = asyncio.create_task(session._send_quiet_acks())
    worker = asyncio.create_task(session._delegation_worker())
    event = {'delegation': {'target': 'client', 'id': 'one'}}
    try:
        session._on_delegation(event)
        session._on_delegation(event)
        await asyncio.wait_for(settling.wait(), 5)
        quiet = await asyncio.wait_for(sent.get(), 5)
        assert quiet['content'] == '' and quiet['delegation_id'] == 'one'
        assert quiet['type'] == 'session.thinking.append' and quiet['event_id']
        assert not release.is_set() and sent.empty() and session.delegations == 0
    finally:
        ack.cancel()
        worker.cancel()
        await asyncio.gather(ack, worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_style_preserving_summary_keeps_configured_auxiliary_task_and_factual_fallback(monkeypatch):
    import agent.auxiliary_client
    import gateway.voice_summary as summary_module
    monkeypatch.setattr(summary_module, '_voice_config', lambda: {'tts_summary_max_chars': 150})
    text = 'Sorted — the clock check passed, at 14:05 UTC. The disk audit is still running. ' + 'Detailed evidence. ' * 30
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='Sorted — clock checked at 14:05 UTC. The disk audit is still running.'))])
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(agent.auxiliary_client, 'async_call_llm', call)
    result = await summary_module.summarize_for_voice(text, preserve_style=True)
    assert result == response.choices[0].message.content
    kwargs = call.await_args.kwargs
    assert kwargs['task'] == 'voice_summary'  # DeepSeek remains selected by the existing profile config.
    assert not any(k in kwargs for k in ('model', 'provider', 'base_url'))
    assert kwargs['messages'][1]['content'] == text
    call.side_effect = RuntimeError('synthetic outage')
    fallback = await summary_module.summarize_for_voice(text, preserve_style=True)
    assert fallback.startswith('Sorted') and '14:05 UTC' in fallback and 'still running' in fallback
