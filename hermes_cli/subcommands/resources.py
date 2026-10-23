"""``hermes resources`` — local memory health and foreground job admission."""

from __future__ import annotations

import argparse
import math


def _finite_nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return number


def _memory_bytes(value: str) -> int:
    gib = _finite_nonnegative(value)
    if gib <= 0 or not math.isfinite(gib * (1 << 30)):
        raise argparse.ArgumentTypeError("memory GiB must be finite and positive")
    return math.ceil(gib * (1 << 30))


def build_resources_parser(subparsers) -> None:
    from hermes_cli.heavy_jobs import cmd_resources

    parser = subparsers.add_parser("resources", help="Check host memory and serialize heavy foreground jobs")
    sub = parser.add_subparsers(dest="resources_action", required=True)
    sub.add_parser("status", help="Print fresh memory health JSON (0=ok, 1=pressure, 2=unknown)")
    run = sub.add_parser("run", help="Admit a foreground job; refuse a second job over 2 GiB")
    run.add_argument("--memory-gib", dest="memory_bytes", type=_memory_bytes, required=True,
                     help="Estimated peak RAM for the complete job process tree, in GiB")
    run.add_argument("--wait-seconds", type=_finite_nonnegative, default=0,
                     help="Bounded wait for the heavy-job slot (default: refuse immediately)")
    run.add_argument("job_command", nargs=argparse.REMAINDER, help="Command and arguments after --")
    parser.set_defaults(func=cmd_resources)
