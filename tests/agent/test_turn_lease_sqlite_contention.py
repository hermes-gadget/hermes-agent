"""Real WAL contention and ownership fencing for the durable turn refresher."""

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent.turn_facade_lease import DurableTurnLease
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


@pytest.mark.parametrize("takeover", [False, True], ids=["transient-lock", "lease-taken-over"])
def test_turn_lease_rechecks_ownership_after_a_real_22_second_writer_stall(
    tmp_path, monkeypatch, takeover, caplog,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with closing(SessionDB()) as db, closing(sqlite3.connect(db.db_path, isolation_level=None)) as writer:
        db.create_session("live", "cli")
        holder = f"pid={os.getpid()}:turn=original"
        successor = f"pid={os.getpid()}:turn=successor"
        assert db.try_acquire_session_turn_lease("live", holder)
        interrupts = []
        agent = SimpleNamespace(session_id="live", interrupt=lambda *a, **kw: interrupts.append((a, kw)))
        lease = DurableTurnLease(agent, db, "live", holder)
        lease.turn_active = True
        attempted = threading.Event()
        refresh = db.refresh_session_turn_lease

        def actual_refresh(*args, **kwargs):
            attempted.set()
            return refresh(*args, **kwargs)

        monkeypatch.setattr(db, "refresh_session_turn_lease", actual_refresh)
        writer.execute("BEGIN IMMEDIATE")
        if takeover:
            writer.execute("UPDATE session_turn_leases SET holder = ? WHERE conversation_id = 'live'",
                           (successor,))
        else:
            writer.execute("INSERT INTO state_meta (key, value) VALUES ('unrelated-writer', 'busy')")
        with ThreadPoolExecutor(max_workers=1) as workers:
            renewal = workers.submit(lease.refresh_tick)
            try:
                assert attempted.wait(10)
                # Production's routine 20s write patience expires before this transaction commits.
                threading.Event().wait(22)
            finally:
                writer.commit()
            result = renewal.result(timeout=40)
        assert "retrying within" in caplog.text, "the real write-patience exhaustion was not exercised"
        if takeover:
            assert result is False
            assert len(interrupts) == 1
            assert interrupts[0][1] == {"hard_cancel": True, "tool_reason": "session turn lease lost"}
            with pytest.raises(SessionTurnLeaseLostError):
                db.append_message("live", "tool", "stale result", turn_lease_holder=holder)
            assert db.get_messages("live") == []
            db.release_session_turn_lease("live", holder)
            assert db.refresh_session_turn_lease("live", successor)
        else:
            assert result is None and not interrupts
            db.append_message("live", "tool", "completed result", turn_lease_holder=holder)
            assert db.get_messages("live")[-1]["content"] == "completed result"
        db.release_session_turn_lease("live", successor if takeover else holder)


@pytest.mark.parametrize("operation", ["acquire", "refresh"])
def test_turn_lease_ttl_starts_after_the_write_lock_wait(tmp_path, monkeypatch, operation):
    """Waiting for the WAL writer must not consume the TTL a successful call grants."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with closing(SessionDB()) as db, closing(sqlite3.connect(db.db_path, isolation_level=None)) as writer:
        db.create_session("live", "cli")
        holder = f"pid={os.getpid()}:turn=original"
        if operation == "refresh":
            assert db.try_acquire_session_turn_lease("live", holder)
        entered_write = threading.Event()
        execute_write = db._execute_write

        def actual_write(*args, **kwargs):
            entered_write.set()
            return execute_write(*args, **kwargs)

        monkeypatch.setattr(db, "_execute_write", actual_write)
        attempt = (db.try_acquire_session_turn_lease if operation == "acquire"
                   else db.refresh_session_turn_lease)
        ttl = 10
        writer.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(attempt, "live", holder, ttl_seconds=ttl)
            try:
                assert entered_write.wait(10)
                threading.Event().wait(3)
            finally:
                released_at = time.time()
                writer.commit()
            assert pending.result(timeout=15)
        expires_at = db._read_one(
            "SELECT expires_at FROM session_turn_leases WHERE conversation_id = 'live'")[0]
        assert expires_at >= released_at + ttl - 1, "the successful lease was backdated by its lock wait"
        db.release_session_turn_lease("live", holder)
