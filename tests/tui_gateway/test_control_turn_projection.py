"""A scheduled job's report to a Bot Chat is model input, never a rendered transcript row.

``_history_to_messages`` is what every client (Desktop, Ink, the dashboard) is handed as the
session transcript; a ``control_turn`` row (cron bot-chat delivery) must be dropped there, exactly
like ``hidden`` scaffolding, so the payload never paints regardless of what any renderer does.
"""
# The shaping bodies are rebound onto server.py's globals (``method_ctx.bind_module``); importing
# them from the sibling alone leaves that module's own names unbound.
from tui_gateway.server import _history_to_messages


def test_control_turn_row_is_not_projected():
    history = [
        {'role': 'user', 'content': 'morning'},
        {'role': 'user', 'content': '[Scheduled task "ukmesh-packet-flow-watch" — automatic report]',
         'display_kind': 'control_turn'},
        {'role': 'assistant', 'content': 'Stall cleared at 19:15Z.'},
    ]

    projected = _history_to_messages(history, image_urls=False)

    texts = [m.get('text') for m in projected]
    assert 'morning' in texts
    assert any('Stall cleared' in str(t) for t in texts)
    assert not any('Scheduled task' in str(t) for t in texts)


def test_hidden_scaffolding_is_still_dropped():
    """The pre-existing kind keeps its treatment: this widening must not become a general filter."""
    history = [{'role': 'user', 'content': 'scaffold', 'display_kind': 'hidden'},
               {'role': 'assistant', 'content': 'reply'}]

    assert [m.get('text') for m in _history_to_messages(history, image_urls=False)] == ['reply']


def test_an_ordinary_user_row_still_projects():
    history = [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'hi'}]

    assert [m.get('role') for m in _history_to_messages(history, image_urls=False)] == ['user', 'assistant']


def test_a_bare_silence_marker_reply_is_not_projected():
    """A control turn's healthy verdict is stored as the marker text; a resumed transcript must not
    paint it (the live lane already withholds it)."""
    history = [
        {'role': 'user', 'content': '[Scheduled task "w"]', 'display_kind': 'control_turn'},
        {'role': 'assistant', 'content': '[SILENT]'},
        {'role': 'user', 'content': 'next'},
        {'role': 'assistant', 'content': 'Disk at 91% on ukmesh-vps.'},
    ]

    assert [m.get('text') for m in _history_to_messages(history, image_urls=False)] == [
        'next', 'Disk at 91% on ukmesh-vps.']


def test_prose_mentioning_the_marker_still_projects():
    history = [{'role': 'assistant', 'content': 'I replied [SILENT] because nothing changed.'}]

    assert [m.get('role') for m in _history_to_messages(history, image_urls=False)] == ['assistant']
