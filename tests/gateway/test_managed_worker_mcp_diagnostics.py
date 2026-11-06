"""MCP startup failures cross the real child pipe and owner stream into quiet CLI stderr."""
import asyncio

import pytest


@pytest.mark.platforms("linux")
def test_worker_mcp_warning_reaches_quiet_view_without_exposing_assignment_secret(tmp_path, monkeypatch, capsys):
    from gateway import session_managed_worker as managed
    from hermes_cli.gateway_chat_view import GatewayChatView
    from tests.gateway import test_managed_worker_unknown_status as fixture

    child = fixture.CHILD.replace(
        "    out.write(b'{\"type\": \"bogus\"}\\n')",
        "    boot = json.loads(line)\n"
        "    out.write((json.dumps({'type': 'mcp.warning', 'message': "
        "'Failed to start MCP server gmail; secret=' + boot['scope']['secret']}) + '\\n').encode())\n"
        "    out.write(b'{\"type\": \"bogus\"}\\n')")
    assert child != fixture.CHILD
    monkeypatch.setattr(fixture, "CHILD", child)
    authority, ref, policy, claim, published, db = fixture._authority_with_a_lost_worker(tmp_path, monkeypatch)

    async def scenario():
        row = await claim()
        authority._stamp_claim(ref, authority.sessions[ref.session_id], row)
        with pytest.raises(managed.ManagedExecutionUnknown):
            async with asyncio.timeout(30):
                await managed.execute_managed(authority, ref, row, policy)

    with db:
        asyncio.run(scenario())
    warnings = [p for kind, p in published if kind == "status.update"]
    assert warnings == [{"kind": "mcp_warning", "text": "Failed to start MCP server gmail; secret=***"}]
    view = GatewayChatView(None, {"stored_session_id": "s"}, quiet=True)
    for payload in warnings:
        view._dispatch_event("status.update", "turn", payload)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Failed to start MCP server gmail; secret=***" in output.err
