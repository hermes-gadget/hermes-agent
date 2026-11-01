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


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken_answer", ["yes", "Yes, do that!"])
async def test_foreground_answer_survives_updates_and_executes_a_proposal_once(rig, spoken_answer):
    question = await rig.service.handle({"action": "prompt", "question": "Fix the parser?",
        "proposed_action": {"action": "dispatch", "project": "radio", "task": "Fix the parser"}}, "proposal")
    await rig.service.quiet("The unrelated docs worker finished.")
    answer = await rig.service.interactions.intercept(spoken_answer, "answer")
    assert '"status": "answered"' in answer
    assert (await rig.started.get()).text == "Fix the parser"
    repeated = await rig.service.handle({"action": "respond", "prompt_id": question["prompt_id"], "response": "yes"}, "again")
    assert repeated["status"] == "answered" and len(rig.channel.sent) == 1 and rig.started.empty()
    other = rig.db.voice_records("interaction", "other-owner")
    assert other == []


@pytest.mark.asyncio
async def test_interrupted_question_is_repeated_and_new_conversation_cannot_answer_old_prompt(rig):
    question = await rig.service.handle({"action": "prompt", "question": "Build it?",
        "proposed_action": {"action": "dispatch", "project": "radio", "task": "Build it"}}, "proposal")
    await rig.service.interactions.interrupted()
    result = await rig.service.interactions.intercept("yes", "heard-half")
    assert "ask this question again" in result and rig.started.empty()
    assert '"status": "answered"' in await rig.service.interactions.intercept("yes", "heard-all")
    rig.db.create_session("fresh-personal", "matrix", user_id="@ben:test")
    rig.session.adapter._session_store.peek_session_id = lambda _: "fresh-personal"
    with pytest.raises(ValueError, match="pending question"):
        await rig.service.handle({"action": "respond", "prompt_id": question["prompt_id"], "response": "yes"}, "stale")


def running_native_job(rig):
    source = rig.target.build_source(chat_id="33", chat_name="Radio", chat_type="group", user_id="22", guild_id="7")
    key = rig.target._source_session_key(source)
    rig.runner.session_store._entries[key] = SessionEntry(key, "coding", datetime.now(), datetime.now(), origin=source)
    state = rig.runner._sessions[key] = SessionState()
    state.turn.agent = SimpleNamespace(get_activity_summary=lambda: {"description": "Running tests"}, cwd="/radio")
    state.turn.event = SimpleNamespace(source=source, text="Fix parser", metadata={})
    state.persistent.run_generation = 7
    return source, key, state


@pytest.mark.asyncio
async def test_attach_existing_native_job_and_refuse_replacement_even_in_same_channel(rig):
    source, key, state = running_native_job(rig)
    attached = await rig.service.handle({"action": "attach", "agent_id": "gateway:" + key}, "attach")
    result = await rig.service.handle({"action": "steer", "attachment_id": attached["attachment_id"],
                                      "task": "Keep packet compatibility"}, "correction")
    assert result["status"] == "requested"
    event = await rig.started.get()
    assert event.text == "/steer Keep packet compatibility" and event._execution_target is not None
    state.persistent.run_generation += 1
    with pytest.raises(ValueError, match="execution ended or changed"):
        await rig.service.handle({"action": "stop", "attachment_id": attached["attachment_id"]}, "stop-replacement")
    assert rig.started.empty()
    with pytest.raises(ValueError, match="execution ended or changed"):
        await rig.target.handle_message(event)


@pytest.mark.asyncio
async def test_attached_clarification_answers_exact_question_without_starting_another_turn(rig):
    from tools.clarify_gateway import register, clear_session, get_pending_for_session
    source, key, state = running_native_job(rig)
    attached = await rig.service.handle({"action": "attach", "agent_id": "gateway:" + key}, "attach")
    entry = register("radio-choice", key, "Which format?", ["JSON", "CSV"])
    try:
        await rig.service.interactions.prompt({"question": entry.question, "choices": entry.choices,
            "native": {"attachment_id": attached["attachment_id"], "clarify_id": entry.clarify_id}}, "native-question")
        result = await rig.service.interactions.intercept("CSV", "answer")
        assert '"status": "answered"' in result and entry.event.is_set() and entry.response == "CSV"
        assert rig.started.empty() and get_pending_for_session(key, include_choice_prompts=True) is entry
    finally:
        clear_session(key)


@pytest.mark.asyncio
async def test_transport_reconnect_preserves_call_questions_and_accepted_work(rig, monkeypatch):
    import websockets
    monkeypatch.setenv("OPENAI_API_KEY", "fake-local-key")
    session = rig.session
    session._send = LiveVoiceSession._send.__get__(session)
    session.RECOVERY_DELAYS = (0,)
    session.RECOVERY_DEADLINE = 10
    closed, connected = asyncio.Event(), asyncio.Event()
    startups = []
    async def server(ws):
        initial = json.loads(await ws.recv())
        startups.append(initial)
        await ws.send(json.dumps({"type": "session.started", "session": {"id": f"vendor-{len(startups)}"}}))
        if len(startups) == 1:
            await closed.wait()
            await ws.close(code=1011)
            return
        connected.set()
        async for raw in ws:
            if json.loads(raw)["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 1}}))
                return
    async with websockets.serve(server, "127.0.0.1", 0) as local:
        session.cfg = replace(session.cfg, ws_url=f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}")
        session.mixer = SimpleNamespace(attach_live=AsyncMock(), detach_live=lambda: None)
        buffer = SimpleNamespace(feed=lambda p: None, flush=lambda: None, active=False)
        session.mixer.attach_live = lambda **kw: buffer
        await session.start()
        try:
            receipt = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Fix parser"}, "same-request")
            await rig.started.get()
            await rig.service.handle({"action": "prompt", "question": "Which fixture?", "choices": ["A", "B"]}, "question")
            original = session.assistant
            closed.set()
            await asyncio.wait_for(connected.wait(), 10)
            async with asyncio.timeout(10):
                while session.recoveries != 1:
                    await asyncio.sleep(0)
            assert session.is_live and session.assistant is original and session.logical_session_id == "vendor-1"
            assert session.session_id == "vendor-2"
            assert any(receipt["request_id"] in item["content"][0]["text"] for item in startups[1]["session"]["input"])
            assert any("Which fixture?" in item["content"][0]["text"] for item in startups[1]["session"]["input"])
            retry = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Fix parser"}, "same-request")
            assert retry["request_id"] == receipt["request_id"] and len(rig.channel.sent) == 1
            rig.release.set()
            await asyncio.gather(*list(rig.target._session_tasks.values()))
            await asyncio.gather(*list(rig.service._result_tasks))
            assert rig.db.voice_dispatches(rig.service.owner_key)[0]["status"] == "reported"
        finally:
            await session.close("test")
    assert len(startups) == 2


def test_recovery_audio_never_replays_a_truncated_request():
    from plugins.platforms.live_voice.recovery import RecoveryAudio
    audio = RecoveryAudio(seconds=1)
    audio.append(b"a" * 24000)
    assert audio.take() == (b"a" * 24000, False)
    audio.append(b"a" * 48000)
    audio.append(b"b")
    audio.append(b"c")
    assert audio.take() == (b"", True)


def personal_delivery(rig):
    from gateway.platforms.base import SendResult
    source = rig.session.backend_source
    rig.runner.session_store._entries["personal-key"] = SessionEntry("personal-key", "personal", datetime.now(), datetime.now(), origin=source)
    origin = rig.session.adapter
    origin.send = AsyncMock(return_value=SendResult(True, "$notice"))
    rig.runner.adapters[Platform.MATRIX] = origin
    return origin.send


@pytest.mark.asyncio
async def test_offcall_blocker_is_mirrored_once_and_resolved_muted_blockers_are_not_sent(rig):
    from tools.clarify_gateway import register, clear_session, respond_to_id
    from plugins.platforms.live_voice.observer import OwnerContext, OwnerObserver
    send = personal_delivery(rig)
    source, key, state = running_native_job(rig)
    config = rig.service.config
    config["voice"]["assistant"]["notifications"] = {"quiet_hours": False}
    observer = OwnerObserver(OwnerContext(rig.runner, rig.home, config, rig.db))
    entry = register("choice-one", key, "Which format?", ["JSON", "CSV"])
    try:
        rig.session._accepting_audio = False
        await observer.tick()
        await observer.tick()
        assert send.await_count == 1 and "Which format?" in send.call_args.args[1]
        respond_to_id(entry.clarify_id, key, "JSON")
        clear_session(key)
        entry = register("choice-two", key, "Which format next?", ["JSON", "CSV"])
        config["voice"]["assistant"]["notifications"]["mode"] = "muted"
        await observer.tick()
        respond_to_id(entry.clarify_id, key, "CSV")
        config["voice"]["assistant"]["notifications"]["mode"] = "watched"
        await observer.tick()
        assert send.await_count == 1
        assert any(r["status"] == "resolved" for r in rig.db.voice_records("notice", observer.context.owner_key))
    finally:
        clear_session(key)


@pytest.mark.asyncio
async def test_voice_observer_binds_native_question_and_answers_without_queue_deadlock(rig):
    import time
    from tools.clarify_gateway import register, clear_session
    from plugins.platforms.live_voice.observer import OwnerContext, OwnerObserver
    source, key, state = running_native_job(rig)
    rig.service.config["voice"]["assistant"]["notifications"] = {"quiet_hours": False}
    observer = OwnerObserver(OwnerContext(rig.runner, rig.home, rig.service.config, rig.db))
    observer.calls.add(rig.service)
    rig.session._last_user_audio_at = time.monotonic() - 4
    entry = register("spoken-choice", key, "Use JSON or CSV?", ["JSON", "CSV"])
    try:
        await observer.tick()
        prompt = next(r for r in rig.service.interactions.cached if r["status"] == "foreground")
        assert prompt["native"]["clarify_id"] == "spoken-choice"
        answer = await rig.service.interactions.intercept("CSV", "voice-answer")
        assert '"status": "answered"' in answer and entry.response == "CSV"
        assert rig.started.empty()
    finally:
        clear_session(key)


def test_notification_policy_honors_dst_quiet_hours_and_temporary_modes():
    from plugins.platforms.live_voice.notifications import NotificationPolicy
    def epoch(value):
        return datetime.fromisoformat(value).timestamp()
    policy = NotificationPolicy({})
    assert policy.quiet(epoch("2026-07-01T21:30:00+00:00"))  # 22:30 British Summer Time.
    assert not policy.quiet(epoch("2026-07-01T20:30:00+00:00"))
    policy.settings["temporary"] = {"mode": "muted", "until": "2026-07-02T12:00:00+00:00"}
    assert not policy.allows("input", epoch("2026-07-02T10:00:00+00:00"))
    assert policy.allows("completion", epoch("2026-07-02T13:00:00+00:00"))
    policy.settings["mode"] = "decisions"
    assert not policy.allows("completion", epoch("2026-07-02T13:00:00+00:00"))


@pytest.mark.asyncio
async def test_notification_preferences_and_handoff_preserve_settings_and_delivery_identity(rig):
    send = personal_delivery(rig)
    before = json.loads((rig.home / "config.yaml").read_text())
    await rig.service.handle({"action": "notifications", "mode": "decisions"}, "preferences")
    from hermes_cli.config import read_user_config_raw
    after = read_user_config_raw(rig.home / "config.yaml")
    assert after["discord"] == before["discord"] and after["matrix"] == before["matrix"]
    result = await rig.service.handle({"action": "handoff", "destination": "matrix", "task": "The requested details."}, "send")
    assert result["status"] == "delivered" and result["delivery"]["url"].endswith("/$notice")
    repeated = await rig.service.handle({"action": "handoff", "destination": "matrix", "task": "The requested details.",
                                       "request_id": result["request_id"]}, "retry")
    assert repeated == result and send.await_count == 1
    with pytest.raises(ValueError, match="verified project result"):
        await rig.service.handle({"action": "handoff", "destination": "matrix", "artifact_id": "invented"}, "bad")


@pytest.mark.asyncio
async def test_observer_registration_is_shared_and_disconnect_drains_it(rig):
    from plugins.platforms.live_voice.observer import start_observer, stop_observer
    raw = rig.service.config
    raw["matrix"]["gpt_live"]["assistant_enabled"] = True
    (rig.home / "config.yaml").write_text(json.dumps(raw))
    try:
        first, second = await asyncio.gather(start_observer(rig.target), start_observer(rig.target))
        assert first is second and len(rig.runner._live_voice_observers) == 1
        assert len(first.adapters) == 1 and not first.task.done()
    finally:
        await stop_observer(rig.target)
    assert first.task.done() and not rig.runner._live_voice_observers


@pytest.mark.asyncio
async def test_native_artifact_is_frozen_and_handed_off_by_verified_id(rig):
    from gateway.platforms.base import SendResult
    personal_delivery(rig)
    rig.session.adapter.send_document = AsyncMock(return_value=SendResult(True, "$file"))
    rig.target.send_document = AsyncMock(return_value=SendResult(True, "project-file"))
    original = rig.home / "changes.diff"
    original.write_text("-old\n+new\n")
    async def project(event):
        return f"The verified diff is attached.\nMEDIA:{original}"
    rig.target.set_message_handler(project)
    receipt = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Produce the diff"}, "diff")
    await asyncio.gather(*list(rig.target._session_tasks.values()))
    await asyncio.gather(*list(rig.service._result_tasks))
    artifacts = rig.db.voice_records("artifact", rig.service.owner_key)
    assert len(artifacts) == 1 and artifacts[0]["payload"]["reference"] == receipt["request_id"]
    original.write_text("different content after generation")
    result = await rig.service.handle({"action": "handoff", "artifact_id": artifacts[0]["id"], "destination": "matrix"}, "copy-diff")
    assert result["status"] == "delivered"
    delivered = __import__("pathlib").Path(rig.session.adapter.send_document.call_args.args[1])
    assert delivered.read_text() == "-old\n+new\n"
    assert not rig.db.voice_records("artifact", "other-owner")


@pytest.mark.asyncio
async def test_requested_artifact_production_and_delivery_finish_after_hangup(rig):
    from gateway.platforms.base import SendResult
    from plugins.platforms.live_voice.fulfillment import complete_handoffs
    personal_delivery(rig)
    rig.session.adapter.send_document = AsyncMock(return_value=SendResult(True, "$requested-file"))
    rig.target.send_document = AsyncMock(return_value=SendResult(True, "project-file"))
    first = await rig.service.handle({"action": "dispatch", "project": "radio", "task": "Fix parser"}, "first")
    await rig.started.get()
    rig.release.set()
    await asyncio.gather(*list(rig.target._session_tasks.values()))
    await asyncio.gather(*list(rig.service._result_tasks))
    produce = asyncio.Event()
    async def project(event):
        await produce.wait()
        artifact = rig.home / "requested.diff"
        artifact.write_text("-before\n+after\n")
        return f"The requested diff is ready.\nMEDIA:{artifact}"
    rig.target.set_message_handler(project)
    request = await rig.service.handle({"action": "handoff", "produce": True, "artifact_id": first["request_id"],
        "destination": "matrix", "task": "Produce a diff of the parser changes"}, "produce")
    assert request["status"] == "awaiting_artifact" and "MEDIA:" in rig.channel.sent[-1]
    rig.session._accepting_audio = False
    produce.set()
    await asyncio.gather(*list(rig.target._session_tasks.values()))
    await asyncio.gather(*list(rig.service._result_tasks))
    await complete_handoffs(rig.service, request["followup"]["request_id"])
    record = rig.db.voice_record("delivery", request["request_id"], rig.service.owner_key)
    assert record["status"] == "delivered" and rig.session.adapter.send_document.await_count == 1
    await complete_handoffs(rig.service, request["followup"]["request_id"])
    assert rig.session.adapter.send_document.await_count == 1


@pytest.mark.asyncio
async def test_attached_owned_child_uses_native_control_and_verified_completion(rig):
    import weakref
    from tools.delegate_tool import _register_subagent, _unregister_subagent
    from plugins.platforms.live_voice.observer import OwnerContext, OwnerObserver
    source, key, state = running_native_job(rig)
    class Parent:
        session_id = "coding"
        cwd = "/radio"
        def get_activity_summary(self):
            return {}
    class Child:
        session_id = "child-session"
        def __init__(self, parent):
            self._delegate_parent_ref = weakref.ref(parent)
            self.steered = []
        def steer(self, text):
            self.steered.append(text)
            return True
    parent = state.turn.agent = Parent()
    child = Child(parent)
    _register_subagent({"subagent_id": "test-owned-child", "agent": child, "goal": "Check radio packets",
        "owner_agent_session_id": "coding", "status": "running"})
    try:
        attached = await rig.service.handle({"action": "attach", "agent_id": "native:test-owned-child"}, "attach-child")
        await rig.service.handle({"action": "steer", "attachment_id": attached["attachment_id"], "task": "Use fixture B"}, "steer-child")
        assert child.steered == ["Use fixture B"] and rig.started.empty()
        observer = OwnerObserver(OwnerContext(rig.runner, rig.home, rig.service.config, rig.db))
        await observer.record_child_result(str(rig.home), "child-session", "Packet checks passed", "completed")
        await observer.record_child_result(str(rig.home), "child-session", "Packet checks passed", "completed")
        assert rig.db.voice_record("attachment", attached["attachment_id"], rig.service.owner_key)["status"] == "reported"
        assert len(rig.db.voice_records("notice", rig.service.owner_key)) == 1
    finally:
        _unregister_subagent("test-owned-child", agent=child)


@pytest.mark.asyncio
async def test_notification_write_scope_alternates_a_b_a_without_touching_b(rig, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    from plugins.platforms.live_voice.assistant_tools import handle
    other = rig.home / "other-profile"
    other.mkdir()
    (other / "config.yaml").write_text('{"owner":"B"}')
    before = (other / "config.yaml").read_bytes()
    token = DELEGATION_CONTEXT.set((rig.session, "preferences"))
    scope = set_session_vars(session_key="personal-key")
    try:
        assert "configured" in await asyncio.to_thread(handle, {"action": "notifications", "mode": "decisions"})
        monkeypatch.setenv("HERMES_HOME", str(other))
        denied = await asyncio.to_thread(handle, {"action": "notifications", "mode": "muted"})
        assert "originating voice coordinator" in denied and (other / "config.yaml").read_bytes() == before
        monkeypatch.setenv("HERMES_HOME", str(rig.home))
        assert "configured" in await asyncio.to_thread(handle, {"action": "notifications", "mode": "watched"})
    finally:
        clear_session_vars(scope)
        DELEGATION_CONTEXT.reset(token)


def test_local_evaluation_deduplicates_usage_and_rejects_wrong_target_successes():
    from plugins.platforms.live_voice.evaluation import build_report
    observed = [{"scenario": "native_takeover", "platform": "discord", "success": True,
                 "wrong_target": True, "response_latency_ms": 300}]
    usage = [
        {"session_id": "one", "logical_session_id": "call", "seconds": 3, "backend_handoff_to_audio_ms": [100]},
        {"session_id": "one", "logical_session_id": "call", "seconds": 5, "usage_confirmed": True,
         "backend_handoff_to_audio_ms": [100]},
        {"session_id": "two", "logical_session_id": "call", "seconds": 7, "usage_confirmed": True,
         "backend_handoff_to_audio_ms": [100, 200]},
    ]
    report = build_report(observed, usage)
    assert not report["passed"] and report["critical_failures"] == 1
    assert report["frontend_seconds"] == 12 and report["unconfirmed_usage_sessions"] == 0
    assert report["wire_backend_handoff_to_audio_ms"]["count"] == 2
    observed[0]["wrong_target"] = False
    assert build_report(observed, usage)["passed"]
    assert not build_report(observed, usage, require_complete=True)["passed"]


def test_playback_metrics_distinguish_played_and_discarded_audio():
    from plugins.platforms.live_voice.playback import LivePlaybackBuffer
    buffer = LivePlaybackBuffer(prime_ms=0)
    buffer.feed(b"a" * 1920)
    assert len(buffer.read_frame()) == 960
    buffer.flush()
    assert buffer.read_frame() is None
    metrics = buffer.snapshot()
    assert metrics["played_frames"] == 1 and metrics["played_bytes"] == 960
    assert metrics["discarded_bytes"] == 960 and metrics["buffered_bytes"] == 0


@pytest.mark.asyncio
async def test_project_role_redacts_persisted_personal_results_and_files(rig):
    personal_delivery(rig)
    source = rig.session.backend_source
    key = rig.runner._session_key_for_source(source)
    rig.runner.session_store._entries[key] = SessionEntry(key, "personal", datetime.now(), datetime.now(), origin=source)
    state = rig.runner._sessions[key] = SessionState()
    state.turn.agent = SimpleNamespace(get_activity_summary=lambda: {"description": "Reading private calendar"})
    state.turn.event = SimpleNamespace(source=source, text="Private appointment details", metadata={})
    state.persistent.run_generation = 5
    attached = await rig.service.handle({"action": "attach", "agent_id": "gateway:" + key}, "personal-attach")
    record = rig.db.voice_record("attachment", attached["attachment_id"], rig.service.owner_key)
    rig.db.voice_change_record("attachment", record["id"], rig.service.owner_key, {"watching"}, "reported",
        {**record["payload"], "result": "Private appointment details"})
    rig.db.voice_create_record("artifact", "private-file", rig.service.owner_key,
        {"filename": "private-calendar.txt", "reference": record["id"], "source_url": "matrix-link", "platform": "matrix"})
    rig.session.cfg = replace(rig.session.cfg, assistant_role="project")
    snapshot = await rig.service.handle({"action": "agents"}, "status")
    assert "Private appointment" not in json.dumps(snapshot) and "private-calendar" not in json.dumps(snapshot)
    assert snapshot["attachments"][0]["project"] == "Personal assistant"
    with pytest.raises(ValueError, match="personal Matrix"):
        await rig.service.handle({"action": "handoff", "artifact_id": "private-file", "destination": "current"}, "copy-private")


@pytest.mark.asyncio
async def test_hangup_cancels_transport_recovery_without_another_connection(rig, monkeypatch):
    import websockets
    monkeypatch.setenv("OPENAI_API_KEY", "fake-local-key")
    session = rig.session
    session._send = LiveVoiceSession._send.__get__(session)
    session.RECOVERY_DELAYS = (10,)
    drop = asyncio.Event()
    connections = []
    async def server(ws):
        await ws.recv()
        connections.append(ws)
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "before-hangup"}}))
        await drop.wait()
        await ws.close(code=1011)
    async with websockets.serve(server, "127.0.0.1", 0) as local:
        session.cfg = replace(session.cfg, ws_url=f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}")
        session.mixer = SimpleNamespace(attach_live=lambda **kw: SimpleNamespace(flush=lambda: None, active=False), detach_live=lambda: None)
        await session.start()
        drop.set()
        async with asyncio.timeout(5):
            while not session._recovering:
                await asyncio.sleep(0)
        session.close_requested = True
        await session.close("user_left")
        assert session._recovery_task.done() and len(connections) == 1


@pytest.mark.asyncio
async def test_simultaneous_watched_completions_are_grouped_without_replacing_question(rig):
    import time
    from plugins.platforms.live_voice.observer import OwnerContext, OwnerObserver
    rig.service.config["voice"]["assistant"]["notifications"] = {"quiet_hours": False}
    observer = OwnerObserver(OwnerContext(rig.runner, rig.home, rig.service.config, rig.db))
    observer.calls.add(rig.service)
    rig.session._last_user_audio_at = time.monotonic() - 4
    question = await rig.service.handle({"action": "prompt", "question": "Which project next?", "choices": ["Radio", "Web"]}, "question")
    rig.session._commentary = AsyncMock()
    for ident, project in [("one", "Radio"), ("two", "Web")]:
        await observer.project_result(ident, {"project": project, "result": "Verified checks passed", "monitor_url": "https://discord.com/channels/7/33"})
    await observer.tick()
    assert rig.session._commentary.await_count == 1
    assert "Radio" in rig.session._commentary.call_args.args[1] and "Web" in rig.session._commentary.call_args.args[1]
    assert all(r["status"] == "delivered" for r in rig.db.voice_records("notice", rig.service.owner_key))
    assert next(r for r in await rig.service.interactions.snapshot() if r["status"] == "foreground")["prompt_id"] == question["prompt_id"]


@pytest.mark.asyncio
async def test_startup_history_cannot_speak_and_missing_model_handoff_is_recovered_once(rig, monkeypatch):
    import base64
    import websockets
    from unittest.mock import Mock
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-local-only")
    session = rig.session
    session._send = LiveVoiceSession._send.__get__(session)
    pcm = b"\x00\x10" * 480
    encoded = base64.b64encode(pcm).decode()
    query, ack, verified, done = (asyncio.Event() for _ in range(4))
    requests = []
    async def dispatch(text):
        requests.append(text)
        _, did = DELEGATION_CONTEXT.get()
        result = await rig.service.handle({"action": "projects"}, did)
        session.complete_pending(json.dumps(result), did)
    session._dispatch = dispatch
    async def server(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "session.started", "session": {"id": "loop-regression"}}))
        phase = 0
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 4}}))
                return
            if phase == 0 and event["type"] == "session.input_audio.append":
                await ws.send(json.dumps({"type": "session.output_transcript.delta", "event_id": "old", "delta": "Three old workers are still running."}))
                await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": encoded}))
                phase = 1
            elif phase == 1 and query.is_set():
                await ws.send(json.dumps({"type": "session.input_transcript.delta", "event_id": "user1", "delta": "What projects", "start_ms": 1000, "end_ms": 1200}))
                await ws.send(json.dumps({"type": "session.input_transcript.delta", "event_id": "user2", "delta": " have we got?", "start_ms": 1400, "end_ms": 1800}))
                # No session.delegation.created: emulate the observed missing handoff.
                await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": encoded}))
                phase = 2
            elif phase == 2 and event["type"] == "session.commentary.append" and "radio" in event["content"]:
                await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": encoded}))
                await ws.send(json.dumps({"type": "session.delegation.created", "delegation": {"target": "client", "id": "late"}}))
                verified.set()
                phase = 3
            elif phase == 3 and ack.is_set():
                await ws.send(json.dumps({"type": "session.input_transcript.delta", "event_id": "ack", "delta": "Okay", "start_ms": 4000, "end_ms": 4200}))
                await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": encoded}))
                phase = 4
            elif phase == 4 and event["type"] == "session.instructions.append":
                done.set()
    async with websockets.serve(server, "127.0.0.1", 0) as local:
        session.cfg = replace(session.cfg, ws_url=f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}")
        buffer = SimpleNamespace(active=False, feed=Mock(), flush=Mock())
        session.mixer = SimpleNamespace(attach_live=lambda **kw: buffer, detach_live=lambda: None)
        await session.start()
        try:
            async with asyncio.timeout(5):
                while session.output_audio_bytes < len(pcm):
                    await asyncio.sleep(0)
            assert buffer.feed.call_count == 0
            query.set()
            await asyncio.wait_for(verified.wait(), 8)
            async with asyncio.timeout(5):
                while buffer.feed.call_count < 1:
                    await asyncio.sleep(0)
            ack.set()
            await asyncio.wait_for(done.wait(), 5)
            assert len(requests) == 1 and "What projects have we got?" in requests[0]
            assert buffer.feed.call_count == 1
        finally:
            await session.close("test")


@pytest.mark.asyncio
async def test_recorded_answer_reaches_backend_without_model_token_or_old_greeting(rig):
    import time
    session = rig.session
    await rig.service.handle({"action": "prompt", "question": "Fix the parser?",
        "proposed_action": {"action": "dispatch", "project": "radio", "task": "Fix parser"}}, "proposal")
    session._on_transcript({"event_id": "greeting", "delta": "Hello Hermes", "start_ms": 0, "end_ms": 200})
    session._on_transcript({"event_id": "answer", "delta": "Yes", "start_ms": 1500, "end_ms": 1700})
    session._maybe_backend_fallback(time.monotonic() + 2)
    worker = asyncio.create_task(session._delegation_worker())
    try:
        event = await asyncio.wait_for(rig.started.get(), 5)
        assert event.text == "Fix parser" and len(rig.channel.sent) == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_archived_word_fragments_are_context_data_not_unfinished_live_turns(rig):
    rig.db.open_voice_call("old-call", "personal-key", "@ben:test", "personal")
    rig.db.append_voice_segments("old-call", [
        {"event_id": "a1", "speaker": "assistant", "text": "Three old"},
        {"event_id": "a2", "speaker": "assistant", "text": " workers"},
        {"event_id": "a3", "speaker": "assistant", "text": " were running."},
        {"event_id": "u1", "speaker": "user", "text": "Which"},
        {"event_id": "u2", "speaker": "user", "text": " project?"},
    ])
    history = await rig.service.prepare()
    assert all(r["role"] == "developer" for r in history)
    archive = history[0]["content"][0]["text"]
    assert "Three old workers were running." in archive and "Which project?" in archive
    assert "not current requests or verified live status" in archive
