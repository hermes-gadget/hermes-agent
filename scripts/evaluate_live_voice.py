#!/usr/bin/env python3
"""Summarize local voice evaluation observations and usage; never opens a paid session."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.platforms.live_voice.evaluation import SCENARIOS, build_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--usage", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--min-repeats", type=int, default=1)
    args = parser.parse_args()
    if args.list_scenarios:
        for name, description in SCENARIOS.items():
            print(f"{name}: {description}")
        return 0
    if args.observations is None or args.min_repeats < 1:
        parser.error("provide --observations and a positive --min-repeats")
    def read(path):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path else []
    report = build_report(read(args.observations), read(args.usage), require_complete=args.require_complete, min_repeats=args.min_repeats)
    content = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(content)
    print(content, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
