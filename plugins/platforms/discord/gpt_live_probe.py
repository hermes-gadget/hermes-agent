"""Explicit, audio-free GPT-Live wire probe; never run by adapter startup.

Run with the checkout's Python and a temporary HERMES_HOME. Supply the project
key in that process's environment or temporary home's secret scope. A successful
probe verifies real API access before the Discord bridge is implemented.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from typing import Any

WS_URL = "wss://api.openai.com/v1/live/sessions"


class ProbeError(RuntimeError):
    """A failed prerequisite; implementation must wait for a successful probe."""


async def probe(api_key: str, *, ws_url: str = WS_URL, timeout: float = 15) -> dict:
    """Start and immediately finalize a session, without sending any audio."""
    if not api_key.strip():
        raise ProbeError("Set OPENAI_API_KEY or VOICE_TOOLS_OPENAI_KEY in the gateway secret scope.")

    import websockets

    start_event = {
        "type": "session.start",
        "event_id": "discord_live_probe",
        "session": {
            "model": "gpt-live-1",
            "audio": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "output": {"voice": "marin"},
            },
            "delegation": {"type": "client"},
            "store": False,
        },
    }

    async def receive(ws: Any, expected: str) -> dict:
        async with asyncio.timeout(timeout):
            async for raw in ws:
                event = json.loads(raw)
                if event.get("type") == "error":
                    error = event.get("error", {})
                    message = f"{error.get('code', 'error')}: {error.get('message', 'command rejected')}"
                    raise ProbeError(message.replace(api_key, "[REDACTED]"))
                if event.get("type") == expected:
                    return event
                if event.get("type") == "session.closed":
                    raise ProbeError(f"Session ended before {expected}.")
        raise ProbeError(f"Transport closed before {expected}; final usage unconfirmed.")

    started_at = time.monotonic()
    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ping_interval=20,
        ping_timeout=20,
        open_timeout=timeout,
        close_timeout=timeout,
        max_size=8 * 1024 * 1024,
    ) as ws:
        connected_at = time.monotonic()
        await ws.send(json.dumps(start_event))
        started = await receive(ws, "session.started")
        ready_at = time.monotonic()
        # Finalization takes priority over inspecting the resolved configuration.
        await ws.send(json.dumps({"type": "session.close"}))
        closed = await receive(ws, "session.closed")

    seconds = closed.get("usage", {}).get("seconds")
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds < 0
    ):
        raise ProbeError("session.closed did not contain a valid final usage.seconds.")
    resolved = started.get("session", {})
    if not resolved.get("id"):
        raise ProbeError("session.started did not contain a session ID.")
    for field in ("model", "store"):
        if resolved.get(field) != start_event["session"][field]:
            raise ProbeError(f"Resolved session.{field} differs from the requested configuration.")
    if resolved.get("delegation", {}).get("type") != "client":
        raise ProbeError("Resolved session does not use client delegation.")
    audio = resolved.get("audio", {})
    if (
        audio.get("format") != start_event["session"]["audio"]["format"]
        or audio.get("output", {}).get("voice") != "marin"
    ):
        raise ProbeError("Resolved session does not use the requested audio format and voice.")
    return {
        "verified": True,
        "session_id": resolved["id"],
        "resolved_session": resolved,
        "start_event": start_event,
        "seconds": seconds,
        "close_reason": closed.get("reason"),
        "connect_ms": round((connected_at - started_at) * 1000, 1),
        "session_ready_ms": round((ready_at - started_at) * 1000, 1),
        "finalized_ms": round((time.monotonic() - started_at) * 1000, 1),
    }


def main() -> int:
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from gateway.platforms._shared import get_scoped_secret
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    hydrate_profile_secret_sources(home)
    token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    api_key = ""
    try:
        api_key = get_scoped_secret("OPENAI_API_KEY") or get_scoped_secret("VOICE_TOOLS_OPENAI_KEY") or ""
        result = asyncio.run(probe(api_key))
        print(json.dumps(result, indent=2).replace(api_key, "[REDACTED]"))
        return 0
    except ProbeError as exc:
        print(f"GPT-Live probe failed: {exc}", file=sys.stderr)
    except TimeoutError:
        print("GPT-Live probe timed out; startup or final usage is unconfirmed.", file=sys.stderr)
    except Exception as exc:
        # WebSocket exception reprs may include authentication headers.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status is not None else type(exc).__name__
        print(f"GPT-Live probe failed: {detail}", file=sys.stderr)
    finally:
        reset_secret_scope(token)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
