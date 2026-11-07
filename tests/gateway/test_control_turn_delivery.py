"""A scheduled job's Bot Chat delivery is a CONTROL turn, not a message to the person.

``cron.scheduler_delivery`` delivers every bot-chat output with ``cron_report``; the target must
then persist that inbound row as ``control_turn`` — machinery (a bare silence marker is its healthy
verdict) and unpainted (no renderer draws the payload). Without the flag the same delivery stays an
ordinary ``internal_notification`` turn, which is what regressed into bubble-plus-ack spam.
"""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_contract import SessionActor
from hermes_state_runtime import RuntimeStoreError, get_session_admission


async def _bot_chat_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setattr('gateway.session_bot._watch_reply', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='control-turn-fixture',
                                                  db=db)
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    actor = SessionActor('test-owner', 'default',
                         frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
    ref = create_local_session(authority, actor, dict(request_id='bot', source='gui', model='fixture', toolsets=[]))
    title_new_session(authority, ref, 'Bot Chat')
    return authority, SimpleNamespace(authority=authority, actor=actor), ref


def _params(**extra):
    return dict(id='a' * 32, profile='default', message='scheduled report body', **extra)


@pytest.mark.asyncio
async def test_cron_report_persists_the_turn_as_a_control_turn(tmp_path, monkeypatch):
    """End to end through the FIFO: the committed descriptor carries the flag, and the restored
    event (what the drain actually executes) resolves to the unpainted machinery kind."""
    from gateway.session_bot import deliver
    from gateway.session_automation import restore_local_automation
    from gateway.response_filters import display_kind_for_event

    authority, connection, ref = await _bot_chat_authority(tmp_path, monkeypatch)
    try:
        receipt = await deliver(connection, _params(cron_report=True))
        row = get_session_admission(authority.db, admission_id=receipt['admission_id'])
        assert row is not None
        assert row['payload']['local_automation_v1']['cron_report'] is True
        restored = restore_local_automation(authority, ref, row)
        assert restored.internal is True
        assert display_kind_for_event(restored) == 'control_turn'
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_without_the_flag_the_turn_is_an_ordinary_notification(tmp_path, monkeypatch):
    """The flag is what selects the kind; a plain automation wake keeps its current treatment."""
    from gateway.session_bot import deliver
    from gateway.session_automation import restore_local_automation
    from gateway.response_filters import (
        CONTROL_TURN_DISPLAY_KIND, INTERNAL_NOTIFICATION_DISPLAY_KIND, display_kind_for_event,
    )

    authority, connection, ref = await _bot_chat_authority(tmp_path, monkeypatch)
    try:
        receipt = await deliver(connection, _params(cron_report=False))
        row = get_session_admission(authority.db, admission_id=receipt['admission_id'])
        assert 'cron_report' not in row['payload']['local_automation_v1']
        restored = restore_local_automation(authority, ref, row)
        assert display_kind_for_event(restored) == INTERNAL_NOTIFICATION_DISPLAY_KIND
        assert display_kind_for_event(restored) != CONTROL_TURN_DISPLAY_KIND
    finally:
        authority.db.close()


def test_control_turn_may_answer_with_a_bare_silence_marker():
    """Machinery, like a synthetic wake: silence is the healthy verdict. A client-authored
    ``hidden`` turn stays excluded, so this widening never relaxes the human-turn guard."""
    from gateway.response_filters import is_machinery_display_kind, silence_allowed

    assert is_machinery_display_kind('control_turn')
    assert silence_allowed('control_turn')
    assert not is_machinery_display_kind('hidden')
    assert not is_machinery_display_kind(None)


def test_cron_report_requires_an_internal_event():
    """Inbound text can never type its own row: the flag only counts on a self-injected turn."""
    from gateway.response_filters import display_kind_for_event

    assert display_kind_for_event(MessageEvent(text='chat', metadata={'cron_report': True})) is None


@pytest.mark.asyncio
async def test_cron_report_is_part_of_the_delivery_identity(tmp_path, monkeypatch):
    """A same-id replay that changes the flag is a different delivery, never an overwrite."""
    from gateway.session_bot import deliver

    authority, connection, _ref = await _bot_chat_authority(tmp_path, monkeypatch)
    try:
        await deliver(connection, _params(cron_report=True))
        with pytest.raises(RuntimeStoreError, match='admission_conflict'):
            await deliver(connection, _params(cron_report=False))
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_cron_report_must_be_a_boolean(tmp_path, monkeypatch):
    from gateway.session_bot import deliver

    authority, connection, _ref = await _bot_chat_authority(tmp_path, monkeypatch)
    try:
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await deliver(connection, _params(cron_report='yes'))
    finally:
        authority.db.close()
