"""Regression for backlog #457: mixed source must never reach agent work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from hermes_constants import FIRST_PARTY_MODULE_ROOTS

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    for path in REPO.glob("*.py"):
        shutil.copy2(path, root / path.name)
    for name in FIRST_PARTY_MODULE_ROOTS | {"hermes_cli", "hermes_platform", "pm"}:
        path = REPO / name
        if path.is_dir() and name != "tests":
            shutil.copytree(path, root / name, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    for name in ("locales",):
        if not (root / name).exists():
            shutil.copytree(REPO / name, root / name)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home),
           "HERMES_RUNTIME_DIR": str(tmp_path / "tools"), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    return root, env


@pytest.mark.parametrize("broken,complete", [(True, True), (False, True), (False, False)],
                         ids=["missing-tool-row-emoji", "consistent-tree", "healthy-source-partial-maintenance"])
def test_real_tool_graph_gates_startup_and_update_completion(source_tree, broken, complete):
    root, env = source_tree
    if broken:
        # Both modules remain syntactically valid. Only resolving the real lazy
        # tool-execution imports reveals the missing display export.
        with (root / "agent/display.py").open("a") as stream:
            stream.write("\ndel tool_row_emoji\n")
    driver = r'''
import asyncio, json, logging
from pathlib import Path
logging.basicConfig(level=logging.ERROR)
from hermes_cli.update_cmd_validation import _validate_critical_modules_import
root = Path.cwd()
probe = _validate_critical_modules_import(root, report_runtime_errors=True)
from gateway.run import GatewayRunner
runner = object.__new__(GatewayRunner)
served = []
async def serve():
    served.append(True)
    return True
async def flush():
    pass
runner._start_impl = serve
runner._start_flush_runtime_status = flush
started = asyncio.run(runner.start())
constructor_error = None
full_gateway_started = None
if not probe[0]:
    from hermes_cli.source_health import SourceHealthError
    from run_agent import AIAgent
    try:
        AIAgent()
    except SourceHealthError as exc:
        constructor_error = str(exc)
    from gateway.run import start_gateway
    from gateway.config import GatewayConfig
    full_gateway_started = asyncio.run(start_gateway(
        config=GatewayConfig(multiplex_profiles=False), verbosity=None))

from hermes_cli import source_health, source_completion, update_cmd, venv_sync
from hermes_cli.update_completion import _complete_selected
events = []
def build(*args, **kwargs):
    events.append('build')
    return completion_verdict
def restart(*args, **kwargs):
    status = json.loads((root / '.hermes/source-health/status.json').read_bytes())
    assert status['status'] == 'ready', 'restart happened before health publication'
    events.append('restart')
source_completion.complete_source_checkout = build
venv_sync.clear_completion = lambda root: None
update_cmd._sweep_bytecode_after_update = lambda branch: None
update_cmd._fleet_restart_skip_reason = lambda plan: None
update_cmd._restart_gateway_fleet_after_update = restart
update_cmd._resume_windows_gateways_and_merge_outcome = lambda *args: None
def verify(*args, **kwargs):
    events.append('verify')
    if not kwargs['update_complete']:
        raise SystemExit(1)
update_cmd._verify_fleet_after_update = verify
completion_error = None
completion_exit = None
with source_health.source_update(root) as generation:
    try:
        _complete_selected(dict(source=str(root), source_update_id=generation,
            sibling_snapshots={}, plan=None, branch='main', desktop=False,
            assume_yes=True, gateway_mode=False, snapshot_id=None,
            pre_update_version=None, windows_resume=None))
    except source_health.SourceHealthError as exc:
        completion_error = str(exc)
    except SystemExit as exc:
        completion_exit = exc.code
status = json.loads((root / '.hermes/source-health/status.json').read_bytes())
print('RESULT=' + json.dumps(dict(probe=probe, started=started, served=served,
    events=events, completion_error=completion_error, status=status['status'],
    constructor_error=constructor_error, full_gateway_started=full_gateway_started,
    completion_exit=completion_exit)))
'''
    result = subprocess.run([sys.executable, "-I", "-c",
                             f"completion_verdict={complete!r};import sys; sys.path.insert(0, '.');" + driver],
                            cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(next(line.removeprefix("RESULT=") for line in result.stdout.splitlines()
                           if line.startswith("RESULT=")))
    print(result.stdout + result.stderr)
    assert data["probe"][0] is (not broken)
    assert data["started"] is (not broken)
    assert bool(data["served"]) is (not broken)
    assert data["events"] == ([] if broken else ["build", "restart", "verify"])
    assert data["status"] == ("updating" if broken else "ready")
    assert data["completion_exit"] == (1 if not complete else None)
    if broken:
        assert "tool_row_emoji" in data["probe"][2]
        assert "tool_row_emoji" in data["completion_error"]
        assert "tool_row_emoji" in data["constructor_error"]
        assert data["full_gateway_started"] is False
        assert "Gateway refused to start" in result.stderr
    else:
        assert data["completion_error"] is None


@pytest.mark.parametrize("ignored", [False, True], ids=["gate-unignored", "gate-ignored"])
def test_source_admission_excludes_mutation_and_survives_interrupted_updates(tmp_path, monkeypatch, ignored):
    from hermes_cli import source_health

    root = tmp_path / "checkout"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=Health test", "-c", "user.email=health@example.invalid",
                               "-c", "commit.gpgsign=false", *args], cwd=root,
                              capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    (root / ".gitignore").write_text(".hermes/\n" if ignored else "")
    (root / "payload.txt").write_text("original")
    git("add", "payload.txt", ".gitignore")
    git("commit", "-qm", "baseline")
    monkeypatch.setattr(source_health, "_PROJECT_ROOT", root)
    monkeypatch.setattr(source_health, "_BOOT_STATE", {"status": "ready", "generation": None})
    driver = r'''
import sys
from pathlib import Path
from hermes_cli.source_health import source_session, SourceHealthError
root = Path(sys.argv[1])
with source_session(root):
    print('reader admitted', flush=True)
    input()
print('reader released', flush=True)
input()
try:
    with source_session(root):
        raise AssertionError('stale process admitted')
except SourceHealthError as exc:
    print(str(exc), flush=True)
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", driver, str(root)], cwd=REPO,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "reader admitted"
        with pytest.raises(source_health.SourceHealthError, match="active agent work"):
            with source_health.source_update(root, drain_timeout=0):
                pytest.fail("mutated source with an active reader")
        # A refused update must return the unchanged tree to its old readers.
        with source_health.source_session():
            pass
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "reader released"
        from hermes_cli.update_cmd_stash import _stash_local_changes_if_needed
        assert _stash_local_changes_if_needed(["git"], root) is None
        with source_health.source_update(root) as generation:
            (root / "payload.txt").write_text("local edit")
            stash = _stash_local_changes_if_needed(["git"], root)
            assert stash and git("show", f"{stash}:payload.txt") == "local edit"
            assert json.loads((root / ".hermes/source-health/status.json").read_bytes())["status"] == "updating"
            with pytest.raises(source_health.SourceHealthError, match="incomplete"):
                with source_health.source_session():
                    pytest.fail("admitted a session during mutation")
            # A second profile cannot start a competing writer for this checkout.
            with pytest.raises(source_health.SourceHealthError, match="busy"):
                with source_health.source_update(root):
                    pytest.fail("admitted a concurrent writer")
            with pytest.raises(source_health.SourceHealthError, match="does not own"):
                source_health.publish_source_ready(root, "unrelated-update")
            # An ordinary failure must not silently reopen admission either.
        with pytest.raises(source_health.SourceHealthError, match="incomplete"):
            with source_health.source_session():
                    pytest.fail("admitted a session after an interrupted update")
        other_home = tmp_path / "other-profile"
        other_home.mkdir()
        writer = subprocess.Popen([sys.executable, "-u", "-c", r'''
import sys
from pathlib import Path
from hermes_cli.source_health import source_update
with source_update(Path(sys.argv[1])) as generation:
    print('writer admitted ' + generation, flush=True)
    input()
''', str(root)], cwd=REPO, env={**os.environ, "HERMES_HOME": str(other_home)},
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        worker = None
        try:
            generation = next(line.strip().removeprefix("writer admitted ") for line in writer.stdout
                              if line.startswith("writer admitted "))
            worker = subprocess.Popen([sys.executable, "-u", "-c", r'''
import sys
from pathlib import Path
from hermes_cli.source_health import source_completion
with source_completion(Path(sys.argv[1]), sys.argv[2]):
    print('completion pinned', flush=True)
    input()
''', str(root), generation], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            assert worker.stdout.readline().strip() == "completion pinned"
            writer.kill()
            writer.communicate(timeout=20)
            # A surviving child must still pin the tree after its parent's
            # writer lock disappears. A successor cannot mutate beneath it.
            with pytest.raises(source_health.SourceHealthError, match="active agent work"):
                with source_health.source_update(root, drain_timeout=0):
                    pytest.fail("mutated source beneath a surviving completion child")
            worker.communicate("release\n", timeout=20)
            assert worker.returncode == 0
        finally:
            for process in (writer, worker):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=20)
        with pytest.raises(source_health.SourceHealthError, match="incomplete"):
            with source_health.source_session():
                pytest.fail("admitted a session after the updater was killed")
        with source_health.source_update(root) as repaired:
            source_health.publish_source_ready(root, repaired)
        child.stdin.write("retry\n")
        child.stdin.flush()
        output, error = child.communicate(timeout=20)
        assert child.returncode == 0, error
        assert "changed since this process started" in output
        # Only a freshly booted process can serve the published source generation.
        monkeypatch.setattr(source_health, "_BOOT_STATE", {"status": "ready", "generation": repaired})
        with source_health.source_session():
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=20)


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: -1)() == 0, reason="root can write read-only directories")
def test_immutable_source_can_serve_but_cannot_begin_an_in_place_update(tmp_path):
    from hermes_cli.source_health import source_session, source_update

    root = tmp_path / "immutable"
    root.mkdir(mode=0o555)
    try:
        with source_session(root):
            pass
        with pytest.raises(PermissionError):
            with source_update(root):
                pytest.fail("began an in-place update without its health gate")
        assert not (root / ".hermes/source-health").exists()
    finally:
        root.chmod(0o755)
