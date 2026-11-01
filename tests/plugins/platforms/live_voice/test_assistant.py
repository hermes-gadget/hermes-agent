"""Native Discord admission, durable retry protection, fleet scope and spoken continuity."""
import asyncio
from dataclasses import replace
from contextlib import nullcontext
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from gateway.session_state import SessionState
from hermes_state import SessionDB
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.live_voice.session import DELEGATION_CONTEXT, LiveVoiceConfig, LiveVoiceSession


class Channel:
    def __init__(self, ident, name, guild, parent=None):
        self.id, self.name, self.guild = ident, name, guild
        self.parent, self.parent_id = parent, parent.id if parent else None
        self.topic, self.sent = "", []
        self.fail = False

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise OSError("fake Discord is unavailable")
        self.sent.append(content)
        message = SimpleNamespace(id=1000 + len(self.sent), content=content, channel=self,
            author=SimpleNamespace(display_name="Hermes"), edit=AsyncMock())
        async def create_thread(**kw):
            thread = Channel(44, "task-thread", self.guild, self)
            self.guild.threads.append(thread)
            return thread
        message.create_thread = create_thread
        return message


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = {"matrix": {"gpt_live": {"enabled": True, "user_id": "@ben:test"}},
           "discord": {"gpt_live": {"enabled": True, "user_id": "22", "voice_channel_id": "11"}},
           "voice": {"assistant": {"projects": {"radio": {"channel_id": "33"}}}}}
    (tmp_path / "config.yaml").write_text(json.dumps(raw))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("personal", "matrix", user_id="@ben:test")
    db.create_session("coding", "discord", user_id="22")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._primary_profile_name = "default"
    runner._profile_adapters = {}
    runner._sessions, runner._agent_cache = {}, {}
    runner._session_db = SimpleNamespace(_db=db)
    runner.session_store = SimpleNamespace(_entries={},
        get_or_create_session=lambda source: SimpleNamespace(session_id="coding"))
    runner._profile_scope_for_source = lambda _: nullcontext()
    runner._is_user_authorized_for_source = lambda s: s.user_id in {"22", "@ben:test"} and str(s.chat_id) != "99"
    target = DiscordAdapter(PlatformConfig(enabled=True, token="test-token", extra={"auto_thread": False}))
    target.gateway_runner = runner
    target._session_store = SimpleNamespace(peek_session_id=lambda _: "coding")
    guild = SimpleNamespace(id=7, name="projects", forum_channels=[], threads=[])
    channel = Channel(33, "radio", guild)
    denied = Channel(99, "other-owner", guild)
    guild.text_channels = [channel, denied]
    target._client = SimpleNamespace(guilds=[guild], user=SimpleNamespace(id=123),
        get_channel=lambda i: next((c for c in [*guild.text_channels, *guild.threads] if c.id == i), None))
    runner.adapters = {Platform.DISCORD: target}
    source = SessionSource(Platform.MATRIX, "!personal:test", user_id="@ben:test", chat_type="dm")
    origin = SimpleNamespace(gateway_runner=runner, _gpt_live_sessions={},
        _session_store=SimpleNamespace(peek_session_id=lambda _: "personal"),
        _canonicalize=lambda s: SimpleNamespace(runtime_home=tmp_path))
    session = LiveVoiceSession(origin, 1, source.chat_id,
        LiveVoiceConfig(user_id="@ben:test", assistant_enabled=True, summarize_for_voice=False),
        mixer=None, dispatch=None, input_to_pcm24=lambda p: p, output_from_pcm24=lambda p: p,
        usage_context={"platform": "matrix"})
    session.session_id = "vendor-call"
    session.backend_source, session.backend_session_key = source, "personal-key"
    session._accepting_audio = True
    origin._gpt_live_sessions[1] = session
    session._send = AsyncMock()
    started, release = asyncio.Queue(), asyncio.Event()
    async def run(event):
        if event.is_command():
            event._gateway_accepted = True
            await started.put(event)
            return "Steering request accepted."
        await started.put(event)
        await release.wait()
        return "Radio project: tests passed; changes are ready for review."
    target.set_message_handler(run)
    yield SimpleNamespace(session=session, service=session.assistant, runner=runner, target=target,
        channel=channel, db=db, started=started, release=release, home=tmp_path)
    release.set()
    # The base adapter owns the task lifecycle and final delivery, as in production.
    for task in list(target._session_tasks.values()):
        task.cancel()
    await asyncio.gather(*list(target._session_tasks.values()), return_exceptions=True)
    await asyncio.gather(*list(session.assistant._result_tasks), return_exceptions=True)
    db.close()


@pytest.mark.asyncio
async def test_project_runs_in_native_channel_with_context_and_exactly_once_receipt(rig):
    service = rig.service
    token = DELEGATION_CONTEXT.set((rig.session, "voice-request"))
    try:
        first = await service.handle({"action": "dispatch", "project": "radio", "task": "Fix the radio parser",
                                      "context": "Preserve packet compatibility."}, "voice-request")
    finally:
        DELEGATION_CONTEXT.reset(token)
    event = await asyncio.wait_for(rig.started.get(), 5)
    assert first["status"] == "accepted" and first["monitor_url"].startswith("https://discord.com/channels/7/33/")
    assert rig.channel.sent[0].startswith("**Task requested")
    assert event.source.platform == Platform.DISCORD and event.source.user_id == "22"
    assert event.queue_only and event.channel_context.endswith("Preserve packet compatibility.")
    assert not hasattr(event, "_gpt_live_delegation") and event.internal is False
    second = await service.handle({"action": "dispatch", "project": "radio", "task": "Fix the radio parser",
                                  "context": "Preserve packet compatibility.", "request_id": first["request_id"]}, "retry")
    assert second == first and len(rig.channel.sent) == 1 and rig.started.empty()
    rig.release.set()
    tasks = list(rig.target._session_tasks.values())
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    await asyncio.gather(*list(service._result_tasks))
    records = rig.db.voice_dispatches(service.owner_key)
    assert records[0]["status"] == "reported" and "tests passed" in records[0]["payload"]["result"]
    assert len([s for s in rig.channel.sent if "tests passed" in str(s)]) == 1
    assert rig.session._backend_updates.qsize() == 1


@pytest.mark.asyncio
async def test_explicit_destinations_win_and_ambiguous_channel_references_require_choice(rig):
    rig.channel.topic = "Related channels: <#77> and <#88>"
    configured = await rig.service.projects.resolve("radio")
    assert configured.channel_id == "33"
    rig.service.config["voice"]["assistant"]["projects"] = {}
    explicit = await rig.service.projects.resolve("33")
    assert explicit.channel_id == "33"
    with pytest.raises(ValueError, match="multiple channels"):
        await rig.service.projects.resolve("radio")
    destination = Channel(77, "implementation", rig.channel.guild)
    rig.channel.guild.text_channels.append(destination)
    rig.channel.topic = "Run project tasks in <#77>"
    designated = await rig.service.projects.resolve("radio")
    assert designated.channel_id == "77" and designated.project == "radio"


@pytest.mark.asyncio
async def test_missing_visibility_refused_authority_and_hangup_never_start_hidden_work(rig):
    rig.channel.fail = True
    with pytest.raises(OSError):
        await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Build it"}, "failure")
    assert rig.started.empty() and not rig.target._session_tasks
    projects = await rig.service.handle({"action": "projects"}, "list")
    assert {r["channel_id"] for r in projects["projects"]} == {"33"}
    rig.session._accepting_audio = False
    with pytest.raises(ValueError, match="authorized"):
        await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Build it"}, "late")
    assert rig.started.empty()


@pytest.mark.asyncio
async def test_fleet_includes_nonvoice_channel_agents_and_marks_external_stale(rig):
    source = rig.target.build_source(chat_id="33", chat_name="Radio", chat_type="group", user_id="22", guild_id="7")
    key = rig.target._source_session_key(source)
    entry = SessionEntry(key, "coding", datetime.now(), datetime.now(), origin=source)
    rig.runner.session_store._entries[key] = entry
    state = rig.runner._sessions[key] = SessionState()
    state.turn.agent = SimpleNamespace(get_activity_summary=lambda: {"description": "Running parser tests"}, cwd="/repo/radio")
    state.turn.event = SimpleNamespace(source=source, text="Fix packet parsing", metadata={})
    snapshot = await rig.service.fleet.snapshot()
    row = next(r for r in snapshot.agents if r["channel_id"] == "33")
    assert row["state"] == "running" and row["activity"] == "Running parser tests"
    assert row["monitor_url"] == "https://discord.com/channels/7/33"
    parent = __import__("plugins.platforms.live_voice.fleet", fromlist=["AgentStatus"]).AgentStatus(**row)
    external = {"now": snapshot.observed_at, "workers": [{"wid": "worker", "parent_ref": "inst:discord:33",
        "state": "busy", "last_age_s": 100, "objective": "Old activity"}]}
    rows = rig.service.fleet._external_rows(external, {"inst:discord:33": parent}, snapshot.observed_at)
    assert rows[0].state == "unknown" and rows[0].freshness == "stale"


@pytest.mark.asyncio
async def test_spoken_history_survives_redial_but_reset_and_owner_boundaries_hold(rig):
    context = rig.service.conversation
    await context.prepare()
    await context.started()
    context.transcript({"event_id": "u1", "delta": "Move the appointment to Friday.", "start_ms": 1, "end_ms": 3}, "user")
    context.transcript({"event_id": "a1", "delta": "Which appointment?", "start_ms": 4, "end_ms": 8}, "assistant")
    await context.flush()
    context.interrupted()
    await context.close()
    history = await context.prepare()
    assert any("Which appointment?" in item["content"][0]["text"] for item in history)
    rows = rig.db.voice_history("personal-key", "@ben:test", "personal")
    assert len(rows) == 2 and rows[-1]["interrupted"]
    assert not rig.db.voice_history("personal-key", "@other:test", "personal")
    assert not rig.db.voice_history("personal-key", "@ben:test", "new-conversation")


@pytest.mark.asyncio
async def test_dispatch_claims_are_durable_and_owner_bound(rig):
    db = rig.db
    first = db.claim_voice_dispatch("one", "owner", "fingerprint", {"task": "test"})
    assert first["claimed"]
    assert not db.claim_voice_dispatch("one", "owner", "fingerprint", {"task": "test"})["claimed"]
    reopened = SessionDB(rig.home / "state.db")
    try:
        assert not reopened.claim_voice_dispatch("one", "owner", "fingerprint", {"task": "test"})["claimed"]
    finally:
        reopened.close()
    with pytest.raises(ValueError):
        db.claim_voice_dispatch("one", "other", "fingerprint", {})


@pytest.mark.asyncio
async def test_deleting_conversation_removes_its_spoken_history(rig):
    rig.db.open_voice_call("old-call", "personal-key", "@ben:test", "personal")
    rig.db.append_voice_segments("old-call", [{"event_id": "u", "speaker": "user", "text": "Private spoken detail"}])
    assert rig.db.voice_history("personal-key", "@ben:test", "personal")
    rig.db.delete_session("personal")
    assert not rig.db.voice_history("personal-key", "@ben:test", "personal")


@pytest.mark.asyncio
async def test_admitted_coordinator_can_finish_visible_dispatch_after_hangup(rig):
    context = (rig.session, "accepted-request")
    state = rig.runner._sessions["personal-key"] = SessionState()
    state.turn.event = SimpleNamespace(_gpt_live_delegation=context)
    rig.session._accepting_audio = False
    rig.session.adapter._gpt_live_sessions.clear()
    token = DELEGATION_CONTEXT.set(context)
    try:
        receipt = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Finish the parser"}, context[1])
        assert receipt["status"] == "accepted" and rig.channel.sent
        state.turn.event = SimpleNamespace()
        with pytest.raises(ValueError, match="authorized"):
            await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Unowned request"}, "stale")
    finally:
        DELEGATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_thread_setup_and_prompt_inheritance_and_controls_target_the_active_request(rig):
    rig.target.config.extra.update(auto_thread=True, channel_prompts={"33": "Use the radio repository and its coding model."})
    receipt = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Fix packets"}, "thread")
    event = await asyncio.wait_for(rig.started.get(), 5)
    assert event.source.parent_chat_id == "33" and event.source.thread_id == "44"
    assert event.channel_prompt == rig.target.config.extra["channel_prompts"]["33"]
    assert "/44/" in receipt["monitor_url"]
    state = rig.runner._sessions[receipt["session_key"]] = SessionState()
    state.turn.event = event
    result = await rig.service.handle({"action": "steer", "request_id": receipt["request_id"], "task": "Keep the old format"}, "correction")
    assert result["status"] == "steer_requested"
    control = await asyncio.wait_for(rig.started.get(), 5)
    assert control.text == "/steer Keep the old format" and control.source.chat_id == "44"
    state.turn.event = SimpleNamespace(metadata={"voice_request_id": "another-job"})
    with pytest.raises(ValueError, match="no other job"):
        await rig.service.handle({"action": "stop", "request_id": receipt["request_id"]}, "wrong")


@pytest.mark.asyncio
async def test_optional_toolset_respects_surface_disables_and_profile_scope(rig, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    from plugins.platforms.live_voice.assistant_tools import handle, register_tools
    rig.target._gpt_live_cfg = replace(rig.target._gpt_live_cfg, enabled=True, assistant_enabled=True,
        voice_channel_id="11", user_id="22")
    register_tools()
    source = rig.target.build_source(chat_id="11", user_id="22", chat_type="group", guild_id="7")
    tools = rig.runner._resolve_enabled_toolsets_for_source({}, source, "discord")
    assert "live_voice" in tools
    disabled = rig.runner._resolve_enabled_toolsets_for_source({"agent": {"disabled_toolsets": ["live_voice"]}}, source, "discord")
    assert "live_voice" not in disabled
    project = rig.target.build_source(chat_id="33", user_id="22", chat_type="group", guild_id="7")
    assert "live_voice" not in rig.runner._resolve_enabled_toolsets_for_source({}, project, "discord")
    token = DELEGATION_CONTEXT.set((rig.session, "status"))
    scope = set_session_vars(session_key="personal-key")
    try:
        result = json.loads(await asyncio.to_thread(handle, {"action": "agents"}))
        assert "coverage" in result
        monkeypatch.setenv("HERMES_HOME", str(rig.home / "another-profile"))
        denied = json.loads(await asyncio.to_thread(handle, {"action": "dispatch", "project": "radio", "task": "Wrong profile"}))
        assert "originating voice coordinator" in str(denied) and rig.started.empty()
        monkeypatch.setenv("HERMES_HOME", str(rig.home))
        assert "coverage" in json.loads(await asyncio.to_thread(handle, {"action": "agents"}))
    finally:
        clear_session_vars(scope)
        DELEGATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_enabled_wire_session_seeds_history_and_records_actual_spoken_fragments(rig, monkeypatch):
    import websockets
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-local-server")
    rig.db.open_voice_call("previous", "personal-key", "@ben:test", "personal")
    rig.db.append_voice_segments("previous", [
        {"event_id": "question", "speaker": "assistant", "text": "Which appointment should I move?"}])
    startup = asyncio.Queue()
    sent = asyncio.Event()
    async def server(ws):
        await startup.put(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "new-call"}}))
        await ws.send(json.dumps({"type": "session.input_transcript.delta", "event_id": "user", "delta": "The dentist.", "start_ms": 1, "end_ms": 4}))
        await ws.send(json.dumps({"type": "session.output_transcript.delta", "event_id": "spoken", "delta": "I'll check the appointment.", "start_ms": 5, "end_ms": 8}))
        sent.set()
        async for raw in ws:
            if json.loads(raw)["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 1}}))
                return
    async with websockets.serve(server, "127.0.0.1", 0) as local:
        session = rig.session
        session.cfg = replace(session.cfg, ws_url=f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}")
        session._send = LiveVoiceSession._send.__get__(session)
        buffer = SimpleNamespace(feed=lambda p: None, flush=lambda: None)
        session.mixer = SimpleNamespace(attach_live=lambda **kw: buffer, detach_live=lambda: None)
        await session.start()
        try:
            initial = (await asyncio.wait_for(startup.get(), 5))["session"]
            assert any("Which appointment" in item["content"][0]["text"] for item in initial["input"])
            assert "personal assistant" in initial["instructions"]
            await asyncio.wait_for(sent.wait(), 5)
            async with asyncio.timeout(5):
                while "I'll check the appointment." not in session.assistant.conversation.recent():
                    await asyncio.sleep(0)
        finally:
            await session.close("test")
    rows = rig.db.voice_history("personal-key", "@ben:test", "personal")
    assert any(r["speaker"] == "assistant" and r["text"] == "I'll check the appointment." for r in rows)
