"""Private voice requests keep their identity through upstream's durable admission."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_envelope import snapshot_native, restore_native
from gateway.session_results import execution_result
from hermes_state_runtime import RuntimeStoreError
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.live_voice.projects import PROJECT_CONTEXT
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_voice_project_metadata_survives_native_admission(tmp_path, monkeypatch):
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '22')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path/'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture'))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='voice')
    runner._wire_adapter_handlers(adapter)
    seen, sent = [], []
    started, release = asyncio.Event(), asyncio.Event()

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id='result')

    async def turn(event, *args, **kwargs):
        seen.append((event.metadata, PROJECT_CONTEXT.get()))
        started.set()
        await release.wait()
        execution_result.get()['result'] = {'final_response':'done','messages':[]}
        return 'done'

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    source = adapter.build_source(chat_id='33', user_id='22', chat_type='dm')
    event = MessageEvent(text='Run the project task', source=source, message_id='posted-voice-task',
                         queue_only=True, allow_gateway_control=False,
                         metadata={'voice_request_id':'voice-request-1'})
    event._voice_project_receipt = (SimpleNamespace(db=None), 'voice-request-1', '33')
    try:
        restored = restore_native(snapshot_native(runner,event), runner)
        assert restored.metadata == event.metadata
        for bad in ('', 3, 'x'*257, {'nested':'id'}):
            with pytest.raises(RuntimeStoreError, match='invalid_params'):
                snapshot_native(runner,replace(event,metadata={'voice_request_id':bad}))
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            snapshot_native(runner,replace(event,metadata={'voice_request_id':'ok','authority':'owner'}))
        await adapter.handle_message(event)
        await asyncio.wait_for(started.wait(),20)
        assert seen[0][0] == event.metadata
        assert seen[0][1] == event._voice_project_receipt
        second = replace(event, message_id='second-post', metadata={'voice_request_id':'voice-request-2'})
        second._voice_project_receipt = (SimpleNamespace(db=None), 'voice-request-2', '33')
        await adapter.handle_message(second)
        ordinary = replace(event, message_id='ordinary-post', metadata={})
        await adapter.handle_message(ordinary)
        release.set()
        await async_wait_until(lambda: len(seen) == 3 and len(sent) == 3 and not adapter._background_tasks,
                               timeout=20,what='voice project response')
        assert seen[1] == (second.metadata, second._voice_project_receipt)
        assert seen[2] == ({}, None)
        assert authority._voice_admission_bindings == {}
    finally:
        release.set()
        for task in list(adapter._background_tasks):task.cancel()
        await asyncio.gather(*list(adapter._background_tasks),return_exceptions=True)
        authority.db.close()
