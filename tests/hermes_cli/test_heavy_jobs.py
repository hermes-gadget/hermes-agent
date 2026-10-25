"""Heavy-job admission must survive contention, profile changes and process death."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import heavy_jobs as jobs


GIB = 1 << 30
HEALTHY = {"mem_total_kib": 16 * GIB // 1024, "mem_available_kib": 12 * GIB // 1024}


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_LOCK_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(jobs, "sample_memory", lambda: dict(HEALTHY))
    return tmp_path


def _cli(env, *args, memory_file=None):
    # Inject only the measurement. The CLI, preflight, file lock and exec are real.
    measurement = (f"__import__('json').loads(__import__('pathlib').Path({str(memory_file)!r}).read_text())"
                   if memory_file else repr(HEALTHY))
    source = (
        "from hermes_cli import heavy_jobs as j; "
        f"j.sample_memory = lambda: {measurement}; "
        "raise SystemExit(j.main())"
    )
    return [sys.executable, "-c", source, *args], env


@pytest.mark.platforms("posix")
def test_process_lock_spans_profiles_queue_exec_and_crash(job_env, monkeypatch):
    profile_a, profile_b = job_env / "a", job_env / "b"
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    command, env = _cli(
        dict(os.environ), "run", "--memory-gib", "3", "--",
        sys.executable, "-u", "-c",
        "import os,sys; print(os.getpid(), flush=True); sys.stdin.readline()",
    )
    with subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as holder:
        try:
            # communicate cannot be used until release; a crashing startup closes stdout.
            assert int(holder.stdout.readline()) == holder.pid
            monkeypatch.setenv("HERMES_HOME", str(profile_b))
            with pytest.raises(jobs.MemoryAdmissionError, match="busy"):
                with jobs.heavy_job(3 * GIB):
                    pytest.fail("second heavy job entered while first was alive")

            refused_cmd, refused_env = _cli(dict(os.environ), "run", "--memory-gib", "3", "--",
                                            sys.executable, "-c", "raise SystemExit(42)")
            refused = subprocess.run(refused_cmd, env=refused_env, capture_output=True, text=True, timeout=10)
            assert refused.returncode == 75 and "busy" in refused.stderr

            queued_cmd, queued_env = _cli(dict(os.environ), "run", "--memory-gib", "6", "--wait-seconds", "10",
                                          "--", sys.executable, "-c", "print('queued job completed')")
            with subprocess.Popen(queued_cmd, env=queued_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as queued:
                try:
                    assert "Waiting" in queued.stderr.readline()
                    holder.stdin.write("release\n")
                    holder.stdin.flush()
                    holder.communicate(timeout=10)
                    out, err = queued.communicate(timeout=15)
                    assert queued.returncode == 0, err
                    assert out.strip() == "queued job completed"
                finally:
                    if queued.poll() is None:
                        queued.kill()
                        queued.communicate(timeout=10)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate(timeout=10)

    # A queued job must resample AFTER waiting, even if the prior owner crashed.
    memory_file = job_env / "memory.json"
    memory_file.write_text(json.dumps(HEALTHY))
    with subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as holder:
        try:
            assert int(holder.stdout.readline()) == holder.pid
            queued_cmd, queued_env = _cli(dict(os.environ), "run", "--memory-gib", "3", "--wait-seconds", "10",
                                          "--", sys.executable, "-c", "raise SystemExit(42)", memory_file=memory_file)
            with subprocess.Popen(queued_cmd, env=queued_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as queued:
                try:
                    assert "Waiting" in queued.stderr.readline()
                    memory_file.write_text(json.dumps(dict(HEALTHY, mem_available_kib=GIB // 1024)))
                    # The exec'd job holds the descriptor; SIGKILL cannot strand the slot.
                    holder.kill()
                    holder.communicate(timeout=10)
                    _, err = queued.communicate(timeout=15)
                    assert queued.returncode == 75 and "headroom" in err
                finally:
                    if queued.poll() is None:
                        queued.kill()
                        queued.communicate(timeout=10)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate(timeout=10)

    # A -> B -> A, same host lock, and a normal release leaves the inode reusable.
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    with jobs.heavy_job(3 * GIB):
        with pytest.raises(jobs.MemoryAdmissionError, match="busy"):
            with jobs.heavy_job(3 * GIB, wait_seconds=0.05):
                pytest.fail("bounded wait admitted a concurrent job")
    with jobs.heavy_job(3 * GIB):
        pass

    failed_cmd, failed_env = _cli(dict(os.environ), "run", "--memory-gib", "3", "--",
                                sys.executable, "-c", "raise SystemExit(23)")
    failed = subprocess.run(failed_cmd, env=failed_env, capture_output=True, text=True, timeout=10)
    assert failed.returncode == 23
    with jobs.heavy_job(3 * GIB):
        pass


@pytest.mark.platforms("posix")
def test_preflight_and_health_share_pressure_and_never_launch_when_refused(job_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hermes", "resources", "status"])
    from hermes_cli.main import _build_cli_parser

    parser, _ = _build_cli_parser()

    for available_gib, pressure in ((12, "ok"), (2, "elevated"), (0.5, "critical")):
        sample = dict(HEALTHY, mem_available_kib=int(available_gib * GIB // 1024))
        monkeypatch.setattr(jobs, "sample_memory", lambda: sample)
        status = parser.parse_args(["resources", "status"])
        assert status.func(status) == (0 if pressure == "ok" else 1)
        health = json.loads(capsys.readouterr().out)
        assert health["pressure"] == pressure
        if pressure != "ok":
            run = parser.parse_args(["resources", "run", "--memory-gib", "3", "--",
                                     sys.executable, "-c", "raise SystemExit(42)"])
            assert run.func(run) == 75
            assert "headroom" in capsys.readouterr().err

    monkeypatch.setattr(jobs, "sample_memory", lambda: {})
    assert jobs.main(["status"]) == 2
    assert json.loads(capsys.readouterr().out)["pressure"] == "unknown"
    with pytest.raises(jobs.MemoryAdmissionError, match="unavailable"):
        with jobs.heavy_job(3 * GIB):
            pytest.fail("unknown memory must not admit work")

    # Ample *current* RAM is insufficient if the new job would consume the reserve.
    monkeypatch.setattr(jobs, "sample_memory", lambda: dict(HEALTHY, mem_available_kib=4 * GIB // 1024))
    with pytest.raises(jobs.MemoryAdmissionError, match="headroom"):
        with jobs.heavy_job(3 * GIB):
            pytest.fail("projected pressure was ignored")

    monkeypatch.setattr(jobs, "sample_memory", lambda: dict(HEALTHY))
    with jobs.heavy_job(3 * GIB):
        # The documented boundary is strictly >2 GiB; small work still runs preflight.
        with jobs.heavy_job(2 * GIB):
            pass
    with pytest.raises(RuntimeError, match="job failed"):
        with jobs.heavy_job(3 * GIB):
            raise RuntimeError("job failed")
    with jobs.heavy_job(3 * GIB):
        pass

    for invalid in (0, -1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            with jobs.heavy_job(invalid):
                pytest.fail("invalid estimate admitted")
