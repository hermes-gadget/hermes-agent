"""CLI startup must expose missing MCP servers, including the #607 timeout race."""

import json
import logging
import sys
import time

import pytest

from hermes_cli import mcp_startup


# Real stdio protocol peer. A file gate holds tools/list until the test releases it.
_SERVER = """
import json, pathlib, sys, time
entered, release = map(pathlib.Path, sys.argv[1:])
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        entered.touch()
        while not release.exists():
            time.sleep(0.01)
        result = {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "fixture-ok"}]}
    elif "id" in request:
        result = {}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


@pytest.fixture
def mcp_home(tmp_path, monkeypatch):
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mcp_startup, "_mcp_discovery_started", set())
    monkeypatch.setattr(mcp_startup, "_mcp_discovery_thread", {})
    monkeypatch.setattr(mcp_startup, "_mcp_server_filter", None)
    yield tmp_path
    (tmp_path / "release").touch()
    assert mcp_startup.join_mcp_discovery(timeout=15)
    shutdown_mcp_servers()


def test_slow_server_is_reported_until_real_registration_completes(mcp_home, capsys):
    from tools.mcp_tool_discovery import get_mcp_status
    from tools.registry import registry

    server = mcp_home / "server.py"
    server.write_text(_SERVER)
    entered, release = mcp_home / "entered", mcp_home / "release"
    config = {"mcp_servers": {
        "gmail": {"command": sys.executable, "args": [str(server), str(entered), str(release)],
                  "connect_timeout": 20, "sampling": {"enabled": False}},
        "disabled": {"command": "disabled-fixture", "enabled": False},
        "excluded": {"command": "excluded-fixture"},
    }}
    (mcp_home / "config.yaml").write_text(json.dumps(config))
    mcp_startup.set_mcp_server_filter(["gmail"])
    logger = logging.getLogger(__name__)
    mcp_startup.start_background_mcp_discovery(logger=logger, thread_name="test-gmail")
    deadline = time.monotonic() + 15
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists(), get_mcp_status()

    start = time.monotonic()
    mcp_startup.ensure_mcp_discovery_before_agent_build(logger=logger, timeout=0.05)
    assert time.monotonic() - start < 2
    output = capsys.readouterr()
    assert "gmail" in output.err and "0.05" in output.err
    assert "mcp_discovery_timeout" in output.err and "hermes mcp test gmail" in output.err
    assert "disabled" not in output.err and "excluded" not in output.err
    assert registry.get_entry("mcp__gmail__echo") is None

    release.touch()
    assert mcp_startup.join_mcp_discovery(timeout=15)
    mcp_startup.ensure_mcp_discovery_before_agent_build(logger=logger, timeout=0.05)
    assert capsys.readouterr().err == ""
    tool = registry.get_entry("mcp__gmail__echo")
    assert tool is not None
    assert "fixture-ok" in tool.handler({})


@pytest.mark.parametrize("surface", ["cli", "oneshot"])
def test_failed_server_reason_reaches_session_stderr(mcp_home, monkeypatch, capsys, surface):
    missing = mcp_home / "missing-gmail-server"
    (mcp_home / "config.yaml").write_text(json.dumps({
        "model": {"default": "fixture-model", "provider": "openrouter"},
        "mcp_single_query_discovery_timeout": 10,
        "mcp_servers": {"gmail": {"command": str(missing), "connect_timeout": 5}},
    }))
    if surface == "cli":
        mcp_startup.ensure_mcp_discovery_before_agent_build(logger=logging.getLogger(__name__), timeout=10)
    else:
        from types import SimpleNamespace
        from agent.managed_worker import discover_profile_mcp
        from hermes_cli.gateway_chat_view import GatewayChatView

        view = GatewayChatView(None, {"stored_session_id": "s"}, quiet=True)
        for message in discover_profile_mcp(SimpleNamespace(toolsets=("gmail",))):
            view._dispatch_event("status.update", "turn", {"kind": "mcp_warning", "text": message})

    output = capsys.readouterr()
    assert "Failed to start MCP server 'gmail'" in output.err
    assert "missing-gmail-server" in output.err
    assert "hermes mcp test gmail" in output.err
    assert output.out == ""
