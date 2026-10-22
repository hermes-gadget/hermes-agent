"""The startup orphan sweep must leave the WAL writer available to active turns."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from types import SimpleNamespace

from agent.turn_facade_lease import DurableTurnLease
from hermes_state import SessionDB


def test_slow_orphan_discovery_does_not_hold_the_turn_lease_writer(tmp_path, monkeypatch):
    """A 21s discovery stall used to own BEGIN IMMEDIATE for its entire duration."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    scan_started, release_scan, renewed = (threading.Event() for _ in range(3))
    interrupts = []
    with ExitStack() as stack:
        maintenance = stack.enter_context(closing(SessionDB()))
        turns = stack.enter_context(closing(SessionDB(maintenance.db_path)))
        maintenance.create_session("orphan", "cron")
        maintenance._write_sql("UPDATE sessions SET started_at = ? WHERE id = 'orphan'",
                               (time.time() - 3600,))
        turns.create_session("live", "cli")
        holder = f"pid={os.getpid()}:turn=live"
        assert turns.try_acquire_session_turn_lease("live", holder)
        agent = SimpleNamespace(session_id="live", interrupt=lambda *a, **kw: interrupts.append((a, kw)))
        lease = DurableTurnLease(agent, turns, "live", holder)
        lease.turn_active = True

        class SlowScanConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                if sql.startswith("SELECT id FROM sessions WHERE ended_at IS NULL"):
                    scan_started.set()
                    assert release_scan.wait(45), "discovery barrier was not released"
                return self.conn.execute(sql, *args)

        # Pause the same real SELECT whether production runs it on the writer (base) or reader.
        maintenance._conn = SlowScanConnection(maintenance._conn)
        connect_reader = maintenance._connect_read_only
        monkeypatch.setattr(maintenance, "_connect_read_only",
                            lambda **kw: SlowScanConnection(connect_reader(**kw)))

        def refresh():
            result = lease.refresh_tick()
            renewed.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as workers:
            sweep = workers.submit(maintenance.sweep_orphaned_sessions, max_idle_seconds=60,
                                   sources=("cron",), respect_gateway_heartbeats=False)
            try:
                assert scan_started.wait(10)
                started = time.monotonic()
                renewal = workers.submit(refresh)
                renewed_during_scan = renewed.wait(5)
                # Keep discovery parked for a real >20s even when renewal succeeds immediately.
                release_scan.wait(max(0, 21 - (time.monotonic() - started)))
            finally:
                release_scan.set()
            assert sweep.result(timeout=15) == ["orphan"]
            assert renewal.result(timeout=15) is None
        assert renewed_during_scan, "orphan discovery monopolized the WAL writer"
        assert not interrupts
        turns.append_message("live", "tool", "in-flight result completed", turn_lease_holder=holder)
        assert turns.get_messages("live")[-1]["content"] == "in-flight result completed"
        turns.release_session_turn_lease("live", holder)


def test_sweep_releases_between_batches_and_rechecks_new_activity_and_owners(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with closing(SessionDB()) as maintenance, closing(SessionDB(maintenance.db_path)) as peer:
        stale = time.time() - 3600
        maintenance._execute_write(lambda conn: conn.executemany(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cron', ?)",
            [(f"orphan-{i:03}", stale) for i in range(250)]))
        execute_write = maintenance._execute_write
        spared = []
        holder = f"pid={os.getpid()}:turn=interleaved"

        def interleave(fn, **kwargs):
            result = execute_write(fn, **kwargs)
            remaining = peer._read_all("SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id")
            if result and len(remaining) >= 3 and not spared:
                spared.extend(row["id"] for row in remaining[:3])
                # These real writes can happen only after a committed batch releases the WAL lock.
                peer.touch_session_activity(spared[0], ts=time.time())
                assert peer.try_acquire_session_turn_lease(spared[1], holder)
                peer._write_sql("UPDATE sessions SET pinned = 1 WHERE id = ?", (spared[2],))
            return result

        monkeypatch.setattr(maintenance, "_execute_write", interleave)
        swept = maintenance.sweep_orphaned_sessions(
            max_idle_seconds=60, sources=("cron",), exclude_pinned=True,
            respect_gateway_heartbeats=False)
        assert len(spared) == 3, "the whole sweep was committed as one write transaction"
        assert set(swept) == {row["id"] for row in peer._read_all(
            "SELECT id FROM sessions WHERE ended_at IS NOT NULL")}
        assert len(swept) == 250 - len(spared)
        assert all(peer.get_session(sid)["ended_at"] is None for sid in spared)
        peer.release_session_turn_lease(spared[1], holder)
