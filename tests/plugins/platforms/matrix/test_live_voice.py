"""Matrix binding and actual child-process boundary contracts."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.live_voice.lease import LiveVoiceLease
from plugins.platforms.matrix import adapter_live as live
from plugins.platforms.matrix.live_config import MatrixLiveConfig, load_matrix_live_config
from plugins.platforms.matrix.live_native import NativeCallLink


@pytest.fixture
def child(tmp_path):
    path = tmp_path / 'native-peer'
    path.write_text(f'#!{sys.executable}\n' + '''import os,socket,struct,json,sys
s=socket.socket(fileno=int(sys.argv[2]))
def exact(n):
 b=b''
 while len(b)<n:
  p=s.recv(n-len(b))
  if not p:sys.exit(0)
  b+=p
 return b
def emit(v):
 b=b'\\0'+json.dumps(v).encode();s.sendall(struct.pack('>I',len(b))+b)
while True:
 n=struct.unpack('>I',exact(4))[0];b=exact(n)
 if b[0]==1:
  s.sendall(struct.pack('>I',len(b))+b);continue
 c=json.loads(b[1:]);op=c['op']
 if op=='init':emit({'event':'ready','protocol':1,'secrets':any(k in os.environ for k in ['OPENAI_API_KEY','MATRIX_ACCESS_TOKEN','MATRIX_PASSWORD'])})
 elif op=='bad':s.sendall(struct.pack('>I',65537))
 elif op=='crash':sys.exit(7)
 elif op=='quit':sys.exit(0)
 else:emit({'event':op})
''')
    path.chmod(0o700)
    return path


@pytest.mark.asyncio
async def test_private_child_pcm_profiles_and_reaping(child, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'must-stay-in-parent')
    monkeypatch.setenv('MATRIX_ACCESS_TOKEN', 'must-stay-in-parent')
    for name in ['a', 'b', 'a']:
        home=tmp_path/name;home.mkdir(exist_ok=True)
        monkeypatch.setenv('HERMES_HOME', str(home))
        events=asyncio.Queue();audio=asyncio.Queue()
        link=NativeCallLink(child,home,{},on_event=events.put,on_audio=lambda g,p:audio.put_nowait((g,p)))
        await link.start()
        assert link.ready.result()['secrets'] is False
        await link.audio(91,b'\x01\x00'*480)
        assert await asyncio.wait_for(audio.get(),2)==(91,b'\x01\x00'*480)
        await link.close()
        assert link.process.returncode==0
        assert link._read_task.done() and link._stderr_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize('op',['bad','crash'])
async def test_broken_helper_fails_closed_and_is_reaped(child,tmp_path,op):
    events=asyncio.Queue()
    link=NativeCallLink(child,tmp_path,{},on_event=events.put,on_audio=lambda *_:None)
    await link.start();await link.control(op)
    assert (await asyncio.wait_for(events.get(),2))['event']=='helper_ended'
    await link.close()
    assert link.process.returncode is not None


def test_config_reads_current_profile_a_b_a(tmp_path,monkeypatch):
    for name,voice in [('a','gleam'),('b','custom'),('a','gleam')]:
        home=tmp_path/name;home.mkdir(exist_ok=True)
        (home/'config.yaml').write_text(f'matrix:\n  gpt_live:\n    enabled: true\n    user_id: "@ben:test"\n    voice: {voice}\n')
        monkeypatch.setenv('HERMES_HOME',str(home))
        assert load_matrix_live_config().voice==voice
    assert not load_matrix_live_config({'matrix':{'gpt_live':{'enabled':'yes'}}}).enabled


class Binding(live.MatrixLiveMixin):
    def __init__(self,home):
        self._init_matrix_live()
        self._matrix_live_cfg=MatrixLiveConfig(enabled=True,user_id='@ben:test')
        self._matrix_link=SimpleNamespace(control=AsyncMock(),close=AsyncMock())
        self.source=SessionSource(platform=Platform.MATRIX,chat_id='!dm:test',chat_type='dm',user_id='@ben:test',user_name='Ben')
        self._resolve_message_context=AsyncMock(return_value=(self.source,))
        self._canonicalize=lambda source:SimpleNamespace(runtime_home=home)
        self._source_session_key=lambda source: 'matrix-private-call'
        self.gateway_runner=SimpleNamespace(_is_user_authorized_for_source=lambda _:True,_profile_scope_for_source=lambda _:nullcontext())
        self.send=AsyncMock();self.handle_message=AsyncMock()


def ring(generation=1):
    return {'generation':generation,'room_id':'!dm:test','user_id':'@ben:test','notification_id':'$ring'}


@pytest.mark.asyncio
async def test_authorized_call_dispatches_voice_without_transcript_post(tmp_path,monkeypatch):
    adapter=Binding(tmp_path);created=[]
    class Session:
        def __init__(self,owner,generation,chat,cfg,**kwargs):
            self.dispatch=kwargs['dispatch'];self.call_id=generation;self.chat_id=chat;self.is_live=False;self.close_requested=False
            created.append(self)
        async def start(self):self.is_live=True
        async def close(self,reason):self.is_live=False
    monkeypatch.setattr(live,'LiveVoiceSession',Session)
    async def control(op,**kwargs):
        if op=='accept':await adapter._matrix_native_event({'event':'joined','generation':kwargs['generation']})
    adapter._matrix_link.control.side_effect=control
    await adapter._start_matrix_call(ring())
    assert len(created)==1
    await created[0].dispatch('Please check the load')
    message=adapter.handle_message.call_args.args[0]
    assert message.message_type.value=='voice'
    assert message.source.platform==Platform.MATRIX and message.source.chat_id=='!dm:test'
    adapter.send.assert_not_called()
    await adapter._disconnect_matrix_live()
    owner=object();assert LiveVoiceLease.claim(tmp_path,owner);LiveVoiceLease.release(tmp_path,owner)


@pytest.mark.asyncio
async def test_busy_lease_declines_before_opening_api(tmp_path):
    adapter=Binding(tmp_path);discord=object();assert LiveVoiceLease.claim(tmp_path,discord)
    try:
        await adapter._start_matrix_call(ring())
        adapter._matrix_link.control.assert_awaited_once_with('decline',generation=1)
        assert not adapter._gpt_live_sessions
        assert adapter.send.await_count==1
    finally:LiveVoiceLease.release(tmp_path,discord)


@pytest.mark.asyncio
async def test_untrusted_or_unresolved_ring_never_accepts(tmp_path):
    adapter=Binding(tmp_path)
    for event in [{**ring(),'user_id':'@other:test'},{**ring(),'notification_id':'synthetic'}]:
        await adapter._start_matrix_call(event)
    adapter.gateway_runner._is_user_authorized_for_source=lambda _:False
    await adapter._start_matrix_call(ring(2))
    assert all(call.args[0]=='decline' for call in adapter._matrix_link.control.await_args_list)
    assert not adapter._matrix_states


@pytest.mark.asyncio
async def test_cancelled_join_releases_profile_lease(tmp_path):
    adapter=Binding(tmp_path)
    task=asyncio.create_task(adapter._start_matrix_call(ring()))
    while not adapter._matrix_states:await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not adapter._matrix_states
    owner=object();assert LiveVoiceLease.claim(tmp_path,owner);LiveVoiceLease.release(tmp_path,owner)


def test_stale_audio_cannot_feed_current_generation(tmp_path):
    adapter=Binding(tmp_path)
    session=SimpleNamespace(is_live=True,feed_user_pcm=lambda *_:pytest.fail('stale audio fed'))
    adapter._gpt_live_sessions[8]=session
    adapter._matrix_native_audio(7,b'\x01\x00'*480)


def test_lease_old_owner_cannot_release_new_owner(tmp_path):
    first,second,third=object(),object(),object()
    assert LiveVoiceLease.claim(tmp_path,first)
    LiveVoiceLease.release(tmp_path,first)
    assert LiveVoiceLease.claim(tmp_path,second)
    LiveVoiceLease.release(tmp_path,first)
    assert not LiveVoiceLease.claim(tmp_path,third)
    LiveVoiceLease.release(tmp_path,second)
