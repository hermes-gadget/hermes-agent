"""Live voice privacy, wire lifecycle, reply delivery, and delegation invariants."""

import asyncio
import base64
import inspect
import json
import struct
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

np = pytest.importorskip("numpy")
websockets = pytest.importorskip("websockets")
pytest.importorskip("discord")

from gateway.config import PlatformConfig
from gateway.run_voice import GatewayVoiceMixin
from plugins.platforms.discord.adapter import DiscordAdapter, VoiceReceiver
from plugins.platforms.discord.gpt_live import (
    DELEGATION_CONTEXT, GptLiveConfig, GptLiveSession, PendingDelegation,
    commentary_chunks, load_gpt_live_config, pcm24_mono_to_pcm48_stereo, pcm48_stereo_to_pcm24_mono,
)
from plugins.platforms.discord import adapter as discord_adapter_module
from plugins.platforms.discord import adapter_gpt_live as live_adapter_module
from plugins.platforms.discord.voice_mixer import FRAME_SIZE, SILENCE_FRAME, VoiceMixer


def config(**kwargs):
    return GptLiveConfig(enabled=True, user_id="22", voice_channel_id="33", summarize_for_voice=False, **kwargs)


@pytest.mark.parametrize("block", [None, {}, {"enabled": False}, {"enabled": "false"},
                                    {"enabled": True, "user_id": []}, {"enabled": True, "prime_ms": -1}])
def test_disabled_or_malformed_config_is_safe(block):
    assert load_gpt_live_config({"discord": {"gpt_live": block}}).enabled is False


def test_config_normalizes_ids_and_uses_gleam():
    cfg = load_gpt_live_config({"discord": {"gpt_live": {
        "enabled": True, "user_id": 22, "voice_channel_id": "00033", "text_channel_id": "",
    }}})
    assert cfg.enabled and cfg.user_id == "22" and cfg.voice_channel_id == "33"
    assert cfg.text_channel_id == "" and cfg.voice == "gleam"


def test_pcm_conversion_and_commentary_preserve_content():
    pcm = np.array([1000, -1000, 3000, 1000] * 480, dtype="<i2").tobytes()
    mono = pcm48_stereo_to_pcm24_mono(pcm)
    assert len(mono) == 960 and np.all(np.frombuffer(mono, "<i2") == 1000)
    stereo = pcm24_mono_to_pcm48_stereo(mono)
    assert len(stereo) == FRAME_SIZE and np.all(np.frombuffer(stereo, "<i2") == 1000)
    text = "語🙂abc" * 600
    chunks = list(commentary_chunks(text))
    assert "".join(chunks) == text and max(len(c.encode()) for c in chunks) <= 480


def test_receiver_only_forwards_mapped_allowed_audio_and_never_buffers_live():
    frames, onsets = [], []
    receiver = VoiceReceiver(
        MagicMock(), stream_callback=lambda uid, pcm: frames.append((uid, pcm)),
        stream_user_ids={22}, utterance_capture=False, speech_onset_callback=lambda: onsets.append(True),
    )
    receiver.map_ssrc(100, 22)
    receiver.map_ssrc(101, 99)
    pcm = np.full(1920, 1000, dtype="<i2").tobytes()
    for ssrc in (101, 102, *([100] * 12)):
        receiver._capture_decoded(ssrc, pcm)
    assert len(frames) == 12 and {uid for uid, _ in frames} == {22}
    assert len(onsets) == 1
    assert not receiver._buffers and receiver.check_silence() == []
    # Unknown and other users are discarded before transport decryption/Opus.
    receiver._running = True
    receiver._secret_key = b"invalid"
    receiver._on_packet(struct.pack(">BBHII", 0x80, 0x78, 1, 0, 102) + b"0123456789")
    assert not receiver._decoders


def adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))


@pytest.mark.asyncio
async def test_final_send_taps_before_delivery_without_swallowing_text(monkeypatch):
    a = adapter()
    cfg = config()
    session = GptLiveSession(a, 1, None, "33", cfg)
    a._gpt_live_sessions[1] = session
    channel = SimpleNamespace(id=33, send=AsyncMock(return_value=SimpleNamespace(id=44)))
    a._client = SimpleNamespace(get_channel=lambda _: channel)
    a._resolve_channel = AsyncMock(return_value=channel)
    a._record_response_async = AsyncMock(side_effect=lambda _, result, *args: result)
    a._last_self_message_id = {}
    a._reply_to_mode = "off"
    token = DELEGATION_CONTEXT.set((session, "one"))
    try:
        future = asyncio.get_running_loop().create_future()
        session.pending = PendingDelegation("one", future)
        await a.send("33", "Tool preview")
        assert not future.done()
        result = await a.send("33", "Final answer", metadata={"notify": True})
        assert result.success and await future == "Final answer"
        assert channel.send.await_count == 2
        channel.send.assert_awaited_with(content="Final answer", reference=None)

        session.pending = PendingDelegation("two", asyncio.get_running_loop().create_future())
        await a.send("33", "Late answer", metadata={"notify": True})
        assert not session.pending.future.done()
        DELEGATION_CONTEXT.set((session, "two"))
        channel.send.side_effect = RuntimeError("Missing Send Messages")
        result = await a.send("33", "Still spoken", metadata={"notify": True})
        assert not result.success and session.pending.future.result() == "Still spoken"
        assert a._should_auto_tts_for_chat("33") is False
        assert (await a.play_tts("33", "/unused.mp3")).success
        assert a._streaming_tts_turn_completed("session") is True
    finally:
        DELEGATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_stream_completion_uses_authoritative_final_only():
    a = adapter()
    session = GptLiveSession(a, 1, None, "33", config())
    a._gpt_live_sessions[1] = session
    session.pending = PendingDelegation("one", asyncio.get_running_loop().create_future())
    event = SimpleNamespace(source=SimpleNamespace(chat_id="33"))
    token = DELEGATION_CONTEXT.set((session, "one"))
    try:
        a._gpt_live_complete_stream(event)
        assert not session.pending.future.done()
        event._streamed_final_response = "The completed streamed answer"
        a._gpt_live_complete_stream(event)
        assert session.pending.future.result() == event._streamed_final_response
    finally:
        DELEGATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_fake_live_server_delegates_pcm_and_finalizes_usage_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-project-key")
    completed, accepted = asyncio.Event(), []
    tone = np.full(480, 500, dtype="<i2").tobytes()

    async def serve(ws):
        start = json.loads(await ws.recv())
        assert start["session"]["store"] is False
        assert start["session"]["audio"]["output"]["voice"] == "gleam"
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "fake_live"}}))
        for event in (
            {"type": "session.input_transcript.delta", "delta": "what's the weather", "end_ms": 100},
            {"type": "session.delegation.created", "delegation": {"id": "one", "target": "client"}},
            {"type": "session.delegation.created", "delegation": {"id": "one", "target": "client"}},
        ):
            await ws.send(json.dumps(event))
        async for raw in ws:
            event = json.loads(raw)
            accepted.append(event)
            if event["type"] == "session.commentary.append":
                assert event["delegation_id"] == "one" and event["content"] == "Sunny."
                for reply in (
                    {"type": "session.output_audio.delta", "delta": base64.b64encode(tone[:11]).decode()},
                    {"type": "session.output_audio.delta", "delta": base64.b64encode(tone[11:]).decode()},
                    {"type": "session.usage.updated", "usage": {"seconds": 12}},
                    {"type": "session.usage.updated", "usage": {"seconds": 15}},
                ):
                    await ws.send(json.dumps(reply))
            elif event["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 15}}))
                completed.set()
                break

    a = adapter()
    async def handle(gid, uid, text, *, adapter, echo_transcript):
        assert (gid, uid, text) == (1, 22, "what's the weather")
        assert echo_transcript is False
        context = DELEGATION_CONTEXT.get()
        context[0].complete_pending("Sunny.", context[1])
    a.gateway_runner = SimpleNamespace(_handle_voice_channel_input=handle)
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        cfg = config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", prime_ms=20)
        mixer = VoiceMixer()
        session = GptLiveSession(a, 1, None, "33", cfg, mixer)
        a._gpt_live_sessions[1] = session
        await session.start()
        try:
            session.feed_user_pcm(99, b"\x01" * FRAME_SIZE)
            session.feed_user_pcm(22, b"\x01" * 7)
            session.feed_user_pcm(22, b"\x01" * (FRAME_SIZE - 7))
            async with asyncio.timeout(5):
                while session.usage_seconds != 15:
                    await asyncio.sleep(0.01)
            assert mixer.read() == pcm24_mono_to_pcm48_stereo(tone)
            assert session.delegations == 1 and len(session.first_audio_ms) == 1
        finally:
            await session.close("user_left")
        await completed.wait()
        await session.close("again")
    sent_audio = [e for e in accepted if e["type"] == "session.input_audio.append"]
    payloads = [base64.b64decode(event["audio"]) for event in sent_audio]
    assert payloads and all(len(pcm) == 960 for pcm in payloads)
    assert len([pcm for pcm in payloads if any(pcm)]) == 1
    records = [json.loads(line) for line in (tmp_path / "logs" / "gpt-live-usage.jsonl").read_text().splitlines()]
    assert len(records) == 1 and records[0]["seconds"] == 15 and records[0]["usage_confirmed"]
    assert "weather" not in json.dumps(records) and mixer.read() == SILENCE_FRAME


@pytest.mark.asyncio
async def test_auto_join_reconciliation_and_state_are_idempotent():
    a = adapter()
    a._gpt_live_cfg = config()
    member = SimpleNamespace(id=22, voice=None)
    guild = SimpleNamespace(id=1, get_member=lambda _: member)
    channel = SimpleNamespace(id=33, guild=guild)
    member.guild, member.voice = guild, SimpleNamespace(channel=channel)
    a._client = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _: channel)
    a._is_allowed_user = MagicMock(return_value=True)
    a._start_gpt_live = AsyncMock()
    await a._reconcile_gpt_live()
    a._start_gpt_live.assert_awaited_once_with(channel)
    a._start_gpt_live.reset_mock()
    a._gpt_live_voice_state(member, SimpleNamespace(channel=channel), SimpleNamespace(channel=channel))
    a._start_gpt_live.assert_not_awaited()
    a._stop_gpt_live = AsyncMock()
    a._gpt_live_voice_state(member, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    await asyncio.gather(*a._gpt_live_tasks)
    a._stop_gpt_live.assert_awaited_once_with(1, "user_left")
    assert 1 not in a._gpt_live_wanted


def test_gateway_voice_dispatch_signature_remains_compatible():
    params = inspect.signature(GatewayVoiceMixin._handle_voice_channel_input).parameters
    assert list(params)[:4] == ["self", "guild_id", "user_id", "transcript"]
    assert params["adapter"].kind == inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_leave_during_start_closes_before_disconnecting_and_duplicate_starts_do_not_join_twice(monkeypatch):
    a = adapter()
    a._gpt_live_cfg = config()
    member = SimpleNamespace(id=22, voice=None)
    guild = SimpleNamespace(id=1, get_member=lambda _: member)
    channel = SimpleNamespace(id=33, guild=guild)
    member.voice = SimpleNamespace(channel=channel)
    a._gpt_live_wanted.add(1)
    a._is_allowed_user = MagicMock(return_value=True)
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    class StartingSession:
        def __init__(self, *args):
            self.close_requested = False
            self.close_requested_reason = None

        async def start(self):
            entered.set()
            await release.wait()

        async def close(self, reason):
            order.append(("closed", reason))

    monkeypatch.setattr(live_adapter_module, "GptLiveSession", StartingSession)
    async def join(*args, **kwargs):
        a._voice_mixers[1] = MagicMock()
        return True
    a.join_voice_channel = AsyncMock(side_effect=join)
    a.leave_voice_channel = AsyncMock(side_effect=lambda _: order.append(("left", None)))
    first = asyncio.create_task(a._start_gpt_live(channel))
    duplicate = asyncio.create_task(a._start_gpt_live(channel))
    await asyncio.wait_for(entered.wait(), 5)
    member.voice = None
    a._gpt_live_request_stop(1, "user_left")
    release.set()
    await asyncio.gather(first, duplicate, *a._gpt_live_tasks)
    a.join_voice_channel.assert_awaited_once()
    assert order == [("closed", "user_left"), ("left", None)]
    assert not a._gpt_live_sessions


@pytest.mark.asyncio
async def test_live_join_replaces_existing_legacy_receiver_and_timeout(monkeypatch):
    a = adapter()
    channel = SimpleNamespace(id=33, guild=SimpleNamespace(id=1))
    vc = MagicMock(channel=channel)
    a._client = MagicMock()
    a._voice_clients[1] = vc
    old_receiver, old_task, timeout = MagicMock(), MagicMock(), MagicMock()
    a._voice_receivers[1], a._voice_listen_tasks[1] = old_receiver, old_task
    a._voice_timeout_tasks[1] = timeout
    session = SimpleNamespace(cfg=config(), feed_user_pcm=MagicMock(), note_user_speech=MagicMock())
    a._gpt_live_sessions[1] = session
    receiver_factory = MagicMock()
    monkeypatch.setattr(discord_adapter_module, "VoiceReceiver", receiver_factory)
    a._install_voice_mixer = AsyncMock()
    assert await a.join_voice_channel(channel, text_channel_id=33, live_mode=True)
    old_receiver.stop.assert_called_once()
    old_task.cancel.assert_called_once()
    timeout.cancel.assert_called_once()
    assert not a._voice_listen_tasks and not a._voice_timeout_tasks
    assert receiver_factory.call_args.kwargs["utterance_capture"] is False
    assert receiver_factory.call_args.kwargs["stream_user_ids"] == {"22"}
    a._install_voice_mixer.assert_awaited_once_with(1, vc, live_mode=True)


@pytest.mark.asyncio
async def test_gateway_allowlist_still_blocks_live_transcript():
    a = adapter()
    a._voice_text_channels[1] = 33
    a._client = SimpleNamespace(get_channel=MagicMock())
    a.handle_message = AsyncMock()
    runner = GatewayVoiceMixin()
    runner._canonicalize = lambda source, **kwargs: source
    runner._is_user_authorized_for_source = lambda source: False
    await runner._handle_voice_channel_input(1, 22, "Do something", adapter=a)
    a.handle_message.assert_not_awaited()
    a._client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_config_keys_and_usage_remain_in_their_own_profile(tmp_path, monkeypatch):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope

    seen = []
    async def serve(ws):
        seen.append(ws.request.headers["Authorization"])
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": f"fake_{len(seen)}"}}))
        assert json.loads(await ws.recv())["type"] == "session.close"
        await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 0}}))

    homes = [tmp_path / "a", tmp_path / "b"]
    for i, home in enumerate(homes):
        home.mkdir()
        (home / ".env").write_text(f"OPENAI_API_KEY=project-{i}\n")
        (home / "config.yaml").write_text(
            f"discord:\n  gpt_live:\n    enabled: true\n    user_id: 22\n    voice_channel_id: {33+i}\n"
        )
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        for home in [homes[0], homes[1], homes[0]]:
            monkeypatch.setenv("HERMES_HOME", str(home))
            token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
            try:
                cfg = load_gpt_live_config()
                assert cfg.voice_channel_id == ("33" if home == homes[0] else "34")
                cfg = replace(cfg, ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
                a = adapter()
                session = GptLiveSession(a, 1, None, cfg.voice_channel_id, cfg, VoiceMixer())
                await session.start()
                await session.close("test")
            finally:
                reset_secret_scope(token)
    assert seen == ["Bearer project-0", "Bearer project-1", "Bearer project-0"]
    for home, count in zip(homes, [2, 1]):
        records = (home / "logs" / "gpt-live-usage.jsonl").read_text().splitlines()
        assert len(records) == count


@pytest.mark.asyncio
async def test_transport_reconnects_once_and_expected_bot_leave_preserves_reconnect(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    drops = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    connections = []

    async def serve(ws):
        index = len(connections)
        connections.append(ws)
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": f"fake_{index}"}}))
        started[index].set()
        await drops[index].wait()
        await ws.close()

    a = adapter()
    member = SimpleNamespace(id=22, voice=None)
    guild = SimpleNamespace(id=1, get_member=lambda _: member)
    channel = SimpleNamespace(id=33, guild=guild)
    member.voice = SimpleNamespace(channel=channel)
    bot = SimpleNamespace(id=999, guild=guild)
    a._client = SimpleNamespace(user=bot)
    a._is_allowed_user = MagicMock(return_value=True)
    a._gpt_live_wanted.add(1)
    async def join(*args, **kwargs):
        a._voice_mixers[1] = VoiceMixer()
        return True
    async def leave(gid):
        a._gpt_live_voice_state(bot, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    a.join_voice_channel = AsyncMock(side_effect=join)
    a.leave_voice_channel = AsyncMock(side_effect=leave)
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        a._gpt_live_cfg = config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        await a._start_gpt_live(channel)
        drops[0].set()
        await asyncio.wait_for(started[1].wait(), 8)
        async with asyncio.timeout(5):
            while not a._gpt_live_sessions[1].is_live:
                await asyncio.sleep(0.01)
        second = a._gpt_live_sessions[1]
        drops[1].set()
        async with asyncio.timeout(5):
            await second._receiver
            while a._gpt_live_tasks:
                await asyncio.sleep(0.01)
    assert len(connections) == 2 and a.join_voice_channel.await_count == 2
    assert not a._gpt_live_sessions and not a._gpt_live_wanted


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "max_duration"])
async def test_cost_watchdog_closes_with_bounded_lifetime(reason):
    a = SimpleNamespace(_gpt_live_session_ended=MagicMock())
    session = GptLiveSession(a, 1, None, "33", config())
    session._accepting_audio = True
    session._last_user_audio_at -= session.cfg.idle_timeout_seconds + 1
    if reason == "max_duration":
        session._started_at -= session.cfg.max_session_minutes * 60 + 1
    await asyncio.wait_for(session._watchdog(), 5)
    a._gpt_live_session_ended.assert_called_once_with(session, reason)
    assert not session.is_live


@pytest.mark.asyncio
async def test_disconnect_waits_for_server_final_usage(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    close_received, finalize = asyncio.Event(), asyncio.Event()
    async def serve(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "fake_live"}}))
        async for raw in ws:
            if json.loads(raw)["type"] == "session.close":
                break
        close_received.set()
        await finalize.wait()
        await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 4}}))

    a = adapter()
    a.leave_voice_channel = AsyncMock()
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        cfg = config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        session = GptLiveSession(a, 1, None, "33", cfg, VoiceMixer())
        a._gpt_live_sessions[1] = session
        await session.start()
        stop = asyncio.create_task(a._stop_gpt_live(1, "user_left"))
        await asyncio.wait_for(close_received.wait(), 5)
        a.leave_voice_channel.assert_not_awaited()
        finalize.set()
        await asyncio.wait_for(stop, 5)
    assert session.finalized.is_set() and session.usage_seconds == 4
    a.leave_voice_channel.assert_awaited_once_with(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("echo_transcript", [False, True])
async def test_gateway_ingress_preserves_replies_with_optional_transcript_echo(streamed, echo_transcript):
    a = adapter()
    posted, edited = [], []
    async def post(*args, **kwargs):
        posted.append(args[0] if args else kwargs["content"])
        return SimpleNamespace(id=444)
    async def edit(**kwargs):
        edited.append(kwargs["content"])
        return SimpleNamespace(id=444)
    channel = SimpleNamespace(id=33, send=AsyncMock(side_effect=post),
                              get_partial_message=lambda _: SimpleNamespace(edit=AsyncMock(side_effect=edit)))
    a._client = SimpleNamespace(get_channel=lambda _: channel)
    a._resolve_channel = AsyncMock(return_value=channel)
    a.send_typing = AsyncMock()
    a.stop_typing = AsyncMock()
    a._record_discord_processing_start = MagicMock()
    a._record_discord_processing_complete = MagicMock()
    a._voice_text_channels[1] = 33
    a._auto_tts_default = True
    session = GptLiveSession(a, 1, None, "33", config())
    a._gpt_live_sessions[1] = session
    session.pending = PendingDelegation("one", asyncio.get_running_loop().create_future())
    runner = GatewayVoiceMixin()
    runner._canonicalize = lambda source, **kwargs: source
    runner._is_user_authorized_for_source = lambda source: True
    runner._is_duplicate_voice_transcript = lambda *args: False
    a.gateway_runner = runner

    async def agent(event):
        assert event.text == "Check the weather" and event.source.chat_id == "33"
        await a.send("33", "Checking the weather", metadata={"_interim_send": True})
        if streamed:
            await a.send("33", "Draft", metadata={"_interim_send": True})
            await a.edit_message("33", "444", "Sunny.", finalize=True)
            event._streamed_final_response = "Sunny."
            return None
        return "Sunny."
    a.set_message_handler(agent)
    token = DELEGATION_CONTEXT.set((session, "one"))
    try:
        await runner._handle_voice_channel_input(
            1, 22, "Check the weather", adapter=a, echo_transcript=echo_transcript)
    finally:
        DELEGATION_CONTEXT.reset(token)
    # This path creates the real delivery ledger and can wait for filesystem I/O.
    answer = await asyncio.wait_for(asyncio.shield(session.pending.future), 30)
    await asyncio.gather(*tuple(a._background_tasks))
    assert answer == "Sunny."
    assert any("Check the weather" in text for text in posted) is echo_transcript
    assert "Checking the weather" in posted
    assert "Sunny." in (edited if streamed else posted)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_backend_progress_reaches_live_before_the_final_reply(monkeypatch, streamed):
    from gateway.config import Platform, StreamingConfig
    from gateway.run_turn_runner import TurnRunner
    from gateway.stream_consumer import StreamConsumerConfig
    from gateway.turn_context import TurnContext

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    progress_received, finish_backend, final_received = (asyncio.Event() for _ in range(3))
    accepted, posted, consumers, tasks = [], [], [], []
    progress = "The example jobs are finished. I am checking the cause."

    async def serve(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "fake_live"}}))
        await ws.send(json.dumps({"type": "session.input_transcript.delta", "delta": "Check the example jobs"}))
        await ws.send(json.dumps({"type": "session.delegation.created", "delegation": {"id": "one", "target": "client"}}))
        async for raw in ws:
            event = json.loads(raw)
            accepted.append(event)
            if event["type"] == "session.thinking.append" and progress in event["content"]:
                progress_received.set()
            elif event["type"] == "session.commentary.append":
                final_received.set()
            elif event["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 0}}))
                break

    a = adapter()
    async def post(*args, **kwargs):
        posted.append(args[0] if args else kwargs["content"])
        return SimpleNamespace(id=444)
    channel = SimpleNamespace(send=AsyncMock(side_effect=post),
                              get_partial_message=lambda _: SimpleNamespace(edit=AsyncMock()))
    a._client = SimpleNamespace(get_channel=lambda _: channel)
    a._resolve_channel = AsyncMock(return_value=channel)
    a._record_response_async = AsyncMock(side_effect=lambda _, result, *args: result)
    a._reply_to_mode = "off"

    async def handle(gid, uid, text, *, adapter, echo_transcript):
        ctx = TurnContext(
            source=SimpleNamespace(platform=Platform.DISCORD, chat_id="33"), user_config={},
            resolve_display_setting=lambda *args: streamed, interim_assistant_messages_enabled=True,
            _status_adapter=a, _status_chat_id="33", _run_still_current=lambda: True,
        )
        runner = SimpleNamespace(
            config=SimpleNamespace(streaming=StreamingConfig(enabled=streamed)), _delivery_adapter_for=lambda _: a,
            _build_stream_consumer_config=lambda *args, **kwargs: (StreamConsumerConfig(edit_interval=0.01), None),
        )
        consumer, delta, interim, _ = TurnRunner(runner, ctx)._setup_stream_consumer("discord")
        consumers.append(consumer)
        tasks.append(asyncio.create_task(consumer.run()))
        def narrate():
            if streamed:
                delta(progress)
            interim(progress, already_streamed=streamed)
        await asyncio.to_thread(narrate)
        await finish_backend.wait()
        session, did = DELEGATION_CONTEXT.get()
        session.complete_pending("The diagnosis is finished.", did)
        consumer.finish("The diagnosis is finished.")

    a.gateway_runner = SimpleNamespace(_handle_voice_channel_input=handle)
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        session = GptLiveSession(a, 1, None, "33", config(
            ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"), VoiceMixer())
        a._gpt_live_sessions[1] = session
        try:
            await session.start()
            await asyncio.wait_for(progress_received.wait(), 5)
            assert not session.pending.future.done()
            assert not any(event["type"] == "session.commentary.append" for event in accepted)
            updates = [event for event in accepted if event["type"] == "session.thinking.append"]
            assert updates and all(event["delegation_id"] == "one" for event in updates)
            finish_backend.set()
            await asyncio.wait_for(final_received.wait(), 5)
        finally:
            finish_backend.set()
            await session.close("test")
            for consumer in consumers:
                consumer.finish()
            await asyncio.gather(*tasks)
    assert any(progress in text for text in posted)


@pytest.mark.asyncio
async def test_late_backend_completion_updates_its_own_task_without_completing_a_new_one():
    a = adapter()
    session = GptLiveSession(a, 1, None, "33", config())
    session._accepting_audio = True
    session._seen.update({"old", "new"})
    session.pending = PendingDelegation("new", asyncio.get_running_loop().create_future())
    sent, received = [], asyncio.Event()
    async def send(event):
        sent.append(event)
        received.set()
    session._send = send
    session.feed_backend_progress("Old request still pending", "old")
    session.complete_pending("Old request completed successfully", "old")
    task = asyncio.create_task(session._send_backend_updates())
    try:
        await asyncio.wait_for(received.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(sent) == 1 and sent[0]["type"] == "session.thinking.append"
    assert sent[0]["delegation_id"] == "old" and "completed successfully" in sent[0]["content"]
    assert not session.pending.future.done()
    session._accepting_audio = False
    session.feed_backend_progress("New update after disconnect", "new")
    assert session._backend_updates.empty()


@pytest.mark.asyncio
async def test_audio_clock_keeps_sending_silence_when_discord_vad_stops():
    a = SimpleNamespace(_gpt_live_session_ended=MagicMock())
    session = GptLiveSession(a, 1, None, "33", config())
    session._accepting_audio = True
    first, finished = asyncio.Event(), asyncio.Event()
    payloads = []

    async def send(event):
        pcm = base64.b64decode(event["audio"])
        assert len(pcm) == 960
        payloads.append(pcm)
        first.set()
        nonzero = [i for i, data in enumerate(payloads) if any(data)]
        if nonzero and len(payloads) >= nonzero[-1] + 3:
            finished.set()
    session._send = send
    task = asyncio.create_task(session._send_audio_worker())
    try:
        await asyncio.wait_for(first.wait(), 5)
        # One RTP frame, then Discord emits no more packets at all.
        session.feed_user_pcm(22, np.full(1920, 1000, dtype="<i2").tobytes())
        await asyncio.wait_for(finished.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert payloads[0] == b"\0" * 960
    assert len([data for data in payloads if any(data)]) == 1
    assert payloads[-2:] == [b"\0" * 960, b"\0" * 960]


@pytest.mark.asyncio
async def test_discord_handshake_retry_does_not_cancel_start_but_real_move_stops_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    async def serve(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "fake_live"}}))
        async for raw in ws:
            if json.loads(raw)["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 0}}))
                break

    a = adapter()
    member = SimpleNamespace(id=22, voice=None)
    guild = SimpleNamespace(id=1, get_member=lambda _: member)
    channel = SimpleNamespace(id=33, guild=guild)
    member.voice = SimpleNamespace(channel=channel)
    bot = SimpleNamespace(id=999, guild=guild)
    a._client = SimpleNamespace(user=bot)
    a._is_allowed_user = MagicMock(return_value=True)
    a._gpt_live_wanted.add(1)
    async def join(*args, **kwargs):
        # discord.py retries a stale voice session (4006) by temporarily leaving.
        a._gpt_live_voice_state(bot, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
        await asyncio.sleep(0)
        a._voice_mixers[1] = VoiceMixer()
        return True
    a.join_voice_channel = AsyncMock(side_effect=join)
    a.leave_voice_channel = AsyncMock()
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        a._gpt_live_cfg = config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        await a._start_gpt_live(channel)
        session = a._gpt_live_sessions[1]
        assert session.is_live and not session.close_requested and 1 in a._gpt_live_wanted
        a.leave_voice_channel.assert_not_awaited()
        a._gpt_live_voice_state(bot, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
        await asyncio.gather(*tuple(a._gpt_live_tasks))
    assert not a._gpt_live_sessions and 1 not in a._gpt_live_wanted
    assert session.close_requested_reason == "bot_moved"
    a.leave_voice_channel.assert_awaited_once_with(1)
