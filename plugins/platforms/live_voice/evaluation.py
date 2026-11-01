"""Local evaluation reports; observed acoustic timings stay distinct from wire timings."""
import math

SCENARIOS = {
    "foreground_followup": "Ask a question, receive a background update, then answer yes/the other one.",
    "native_takeover": "Attach to an existing native job, steer it, and refuse a replaced execution.",
    "quiet_pause": "Pause to think; Hermes leaves space and keeps accepted work running.",
    "interruption": "Interrupt a proposed action, then confirm only after its question is restated.",
    "reconnect": "Drop the voice backend connection; restore context and one accepted task.",
    "hangup": "Hang up during recovery; no reconnect occurs and accepted project work remains visible.",
    "handoff": "Request a real diff in the intended chat, including production while the call ends.",
    "briefing": "Request a role-appropriate briefing; check sources and the absence of a new schedule.",
    "project_names": "Use real project names, abbreviations and a correction to the selected project.",
    "noise": "Try background noise and overlapping speech; check unintended actions and turn taking.",
}


def distribution(values):
    values = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    if not values:
        return {"count": 0, "p50": None, "p90": None}
    def percentile(p):
        position = (len(values) - 1) * p
        low, high = math.floor(position), math.ceil(position)
        return round(values[low] + (values[high] - values[low]) * (position - low), 2)
    return {"count": len(values), "p50": percentile(.5), "p90": percentile(.9)}


def build_report(observations, usage=(), *, require_complete=False, min_repeats=1):
    coverage = {}
    for row in observations:
        if row.get("platform") not in {"matrix", "discord"} or row.get("scenario") not in SCENARIOS or type(row.get("success")) is not bool:
            raise ValueError("Observations require a known scenario, matrix/discord platform and boolean success.")
        for key in ("wrong_target", "duplicate_action"):
            if key in row and type(row[key]) is not bool:
                raise ValueError(key + " must be a boolean")
        for key in ("response_latency_ms", "interruption_ms", "recovery_ms"):
            if key in row and (type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0):
                raise ValueError(key + " must be a finite nonnegative observation")
        name = row["platform"] + ":" + row["scenario"]
        coverage[name] = coverage.get(name, 0) + 1
    latest, calls = {}, {}
    for row in usage:
        ident = row.get("session_id")
        if not ident:
            continue
        latest[ident] = row
        calls[row.get("logical_session_id") or ident] = row
    missing = [f"{p}:{s}" for p in ("matrix", "discord") for s in SCENARIOS if coverage.get(f"{p}:{s}", 0) < min_repeats]
    critical = sum(bool(r.get("wrong_target") or r.get("duplicate_action")) for r in observations)
    successes = sum(r["success"] for r in observations)
    seconds = [r.get("seconds") for r in latest.values()]
    return {
        "passed": bool(observations) and successes == len(observations) and critical == 0 and (not require_complete or not missing),
        "scenario_runs": len(observations), "successful_runs": successes, "critical_failures": critical,
        "coverage": coverage, "missing_repetitions": missing,
        "observed_audio": {key: distribution([r.get(key) for r in observations]) for key in
                           ("response_latency_ms", "interruption_ms", "recovery_ms")},
        "wire_backend_handoff_to_audio_ms": distribution([v for r in calls.values() for v in r.get("backend_handoff_to_audio_ms", [])]),
        "frontend_seconds": sum(v for v in seconds if type(v) in (int, float) and math.isfinite(v) and v >= 0),
        "unconfirmed_usage_sessions": sum(not r.get("usage_confirmed", False) for r in latest.values()),
        "notes": [r["notes"] for r in observations if r.get("notes")],
        "timing_definition": "Observed speech/playback timings come from the listening run; wire handoff timing measures generated audio, not audibility.",
    }
