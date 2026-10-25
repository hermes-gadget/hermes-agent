"""Cooperative host-memory admission for foreground jobs, shared across profiles.

The lock is kernel-owned, never a PID/TTL marker. CLI jobs inherit it through
exec so the admitted process, rather than a killable wrapper, owns the slot.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator, TextIO

from gateway.lifecycle_ledger import sample_memory
from gateway.memory_status import classify_pressure
from gateway.status import _get_lock_dir, _release_file_lock, _try_acquire_file_lock

GIB = 1 << 30
HEAVY_JOB_BYTES = 2 * GIB
logger = logging.getLogger(__name__)


class MemoryAdmissionError(RuntimeError):
    """No job was started; retry after the active job or memory pressure clears."""


def memory_health() -> dict:
    """Fresh local /proc sample using the same pressure tiers as /api/status.

    Unknown measurements stay unknown: an opt-in heavy job must not interpret
    missing telemetry as permission to consume several GiB.
    """
    sample = sample_memory()
    total, available = sample.get("mem_total_kib"), sample.get("mem_available_kib")
    valid = (type(total) is int and type(available) is int
             and total > 0 and 0 <= available <= total)
    total_bytes, available_bytes = (total * 1024, available * 1024) if valid else (None, None)
    return {
        "pressure": classify_pressure(available, total) if valid else "unknown",
        "total_bytes": total_bytes,
        "available_bytes": available_bytes,
        # Reserve space for sessions, the browser and ordinary host activity.
        "reserve_bytes": max(2 * GIB, (total_bytes * 15 + 99) // 100) if valid else None,
    }


def _preflight(estimated_bytes: int) -> None:
    health = memory_health()
    if health["pressure"] == "unknown":
        raise MemoryAdmissionError("Host memory measurement unavailable; job refused")
    if health["available_bytes"] < estimated_bytes + health["reserve_bytes"]:
        raise MemoryAdmissionError(
            "Insufficient memory headroom: "
            f"available={health['available_bytes'] / GIB:.2f} GiB, "
            f"job={estimated_bytes / GIB:.2f} GiB, "
            f"reserve={health['reserve_bytes'] / GIB:.2f} GiB "
            f"(pressure={health['pressure']})"
        )


@contextmanager
def heavy_job(estimated_bytes: int, *, wait_seconds: float = 0) -> Iterator[TextIO | None]:
    """Admit work while keeping >2 GiB jobs mutually exclusive for this OS user.

    Call off the event loop and hold this context until the foreground job
    finishes. ``wait_seconds`` bounds slot contention only; memory is sampled
    AFTER acquiring the slot, immediately before work starts. No FIFO promise.
    The existing machine-local rendezvous directory deliberately ignores
    HERMES_HOME: different profiles still consume the same physical RAM.
    """
    if type(estimated_bytes) is not int or estimated_bytes <= 0:
        raise ValueError("estimated_bytes must be a positive integer")
    if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise ValueError("wait_seconds must be finite and non-negative")
    if estimated_bytes <= HEAVY_JOB_BYTES:
        _preflight(estimated_bytes)
        yield None
        return

    path = _get_lock_dir() / "heavy-job.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never unlink or replace this file: waiters must lock the same inode.
    with path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + wait_seconds
        acquired = _try_acquire_file_lock(handle)
        if not acquired and wait_seconds:
            logger.info("Waiting up to %.1fs for heavy-job slot", wait_seconds)
        while not acquired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryAdmissionError("Heavy-job slot busy or unavailable; job refused")
            time.sleep(min(0.1, remaining))
            acquired = _try_acquire_file_lock(handle)
        try:
            _preflight(estimated_bytes)
            yield handle
        finally:
            _release_file_lock(handle)


def cmd_resources(args) -> int:
    """CLI surface over the same admission context used by Python tools."""
    if args.resources_action == "status":
        health = memory_health()
        print(json.dumps(health, sort_keys=True))
        return {"ok": 0, "elevated": 1, "critical": 1, "unknown": 2}[health["pressure"]]

    command = args.job_command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("resources run requires a command after --", file=sys.stderr)
        return 2
    if os.name == "nt":
        print("resources run requires POSIX exec to transfer the job lock", file=sys.stderr)
        return 2
    try:
        with heavy_job(args.memory_bytes, wait_seconds=args.wait_seconds) as handle:
            if handle is not None:
                os.set_inheritable(handle.fileno(), True)
            # Same PID, exit status and signals as the job; no wrapper can die
            # and release the slot while leaving the foreground job running.
            os.execvp(command[0], command)
    except MemoryAdmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except OSError as exc:
        print(f"Could not run admitted job: {exc}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126


def main(argv: list[str] | None = None) -> int:
    """Lightweight module entry point for terminal tools without agent startup."""
    from hermes_cli.subcommands.resources import build_resources_parser

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.heavy_jobs")
    build_resources_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["resources", *(sys.argv[1:] if argv is None else argv)])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
