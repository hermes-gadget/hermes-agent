"""Local transport checks for the explicitly invoked, audio-free wire spike."""

import asyncio
import json

import pytest

websockets = pytest.importorskip("websockets")

from plugins.platforms.discord.gpt_live_probe import ProbeError, probe


@pytest.mark.asyncio
async def test_probe_waits_for_start_and_final_usage():
    observed = []

    async def serve(ws):
        assert ws.request.headers["Authorization"] == "Bearer test-key"
        start = json.loads(await ws.recv())
        observed.append(start["type"])
        assert start["session"]["delegation"] == {"type": "client"}
        assert start["session"]["store"] is False
        assert start["session"]["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
        session = {**start["session"], "id": "live_fake"}
        await ws.send(json.dumps({"type": "session.started", "session": session}))
        # The next command must close, with no microphone or delegation traffic.
        observed.append(json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type": "session.usage.updated", "usage": {"seconds": 12}}))
        await ws.send(json.dumps({"type": "session.usage.updated", "usage": {"seconds": 15}}))
        await asyncio.sleep(0)
        await ws.send(json.dumps({
            "type": "session.closed", "usage": {"seconds": 16}, "reason": "close_requested",
        }))
        await ws.wait_closed()

    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await probe("test-key", ws_url=f"ws://127.0.0.1:{port}")

    assert observed == ["session.start", "session.close"]
    assert result["verified"] is True
    assert result["session_id"] == "live_fake"
    assert result["seconds"] == 16
    assert result["finalized_ms"] >= result["session_ready_ms"] >= result["connect_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "missing_key", "rejected", "closed_early", "missing_usage", "wrong_config", "timeout",
])
async def test_probe_never_reports_unverified_session_as_success(failure):
    async def serve(ws):
        start = json.loads(await ws.recv())
        if failure == "rejected":
            await ws.send(json.dumps({
                "type": "error",
                "error": {"code": "model_not_found", "message": "Access denied for test-key"},
            }))
            return
        resolved = {**start["session"], "id": "live_fake"}
        if failure == "wrong_config":
            resolved["store"] = True
        await ws.send(json.dumps({"type": "session.started", "session": resolved}))
        await ws.recv()
        if failure == "missing_usage":
            await ws.send(json.dumps({"type": "session.closed"}))
        elif failure == "wrong_config":
            await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 0}}))
        elif failure == "timeout":
            await ws.wait_closed()

    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises((ProbeError, TimeoutError)) as exc:
            await probe(
                "" if failure == "missing_key" else "test-key",
                ws_url=f"ws://127.0.0.1:{port}", timeout=2,
            )
    assert "test-key" not in str(exc.value)
