import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from plugins.platforms.discord import adapter_gpt_live as discord
from plugins.platforms.discord.gpt_live import GptLiveConfig
from plugins.platforms.live_voice.lease import LiveVoiceLease


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['join','retry'])
async def test_cancelled_discord_start_does_not_hold_matrix_busy(tmp_path,monkeypatch,stage):
    adapter=discord.DiscordGptLiveMixin();adapter._init_gpt_live()
    adapter._live_voice_home=tmp_path
    adapter._gpt_live_cfg=GptLiveConfig(enabled=True,user_id='22',voice_channel_id='33')
    adapter._voice_sources={};adapter._voice_mixers={1:object()};adapter._voice_text_channels={}
    adapter._is_allowed_user=lambda *_,**kw:True
    channel=SimpleNamespace(id=33,guild=SimpleNamespace(id=1))
    adapter._gpt_live_member_present=lambda _:True
    adapter._gpt_live_wanted.add(1)
    entered=asyncio.Event()
    class Starting:
        def __init__(self,*args):self.close_requested=False;self.close_requested_reason=None
        async def start(self):raise ConnectionError('synthetic startup failure')
        async def close(self,reason):entered.set()
    monkeypatch.setattr(discord,'GptLiveSession',Starting)
    async def join(*args,**kwargs):
        if stage=='join':entered.set();await asyncio.Event().wait()
        return True
    adapter.join_voice_channel=join;adapter.leave_voice_channel=AsyncMock()
    task=asyncio.create_task(adapter._start_gpt_live(channel))
    await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not adapter._gpt_live_sessions and not adapter._gpt_live_leases
    matrix_owner=object()
    assert LiveVoiceLease.claim(tmp_path,matrix_owner)
    LiveVoiceLease.release(tmp_path,matrix_owner)
    adapter.leave_voice_channel.assert_awaited_once_with(1)
