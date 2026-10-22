"""Source-update admission and the tool-execution import self-test.

The checkout, unlike dependency generations, is updated in place. A durable
maintenance gate and kernel-held reader leases keep that mutation out of live
agent work. State lives beside the code so every profile sees the same gate.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import cache, wraps
import errno
import importlib
import json
import os
from pathlib import Path
import time
import uuid

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_HEALTH_DIRECTORY = ".hermes/source-health"
_RECOVERY = "Finish or retry `hermes update`, then restart the affected Hermes processes."


class SourceHealthError(RuntimeError):
    """This checkout cannot safely admit agent work."""


def _read_state(root: Path) -> dict:
    path = root / SOURCE_HEALTH_DIRECTORY / "status.json"
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {"schema": 1, "status": "ready", "generation": None}
    except (OSError, ValueError) as exc:
        raise SourceHealthError(f"Cannot read the Hermes source health gate at {path}. {_RECOVERY}") from exc
    if (not isinstance(data, dict) or data.get("schema") != 1
            or data.get("status") not in ("ready", "updating")
            or not isinstance(data.get("generation"), str) or not data["generation"]):
        raise SourceHealthError(f"Invalid Hermes source health gate at {path}. {_RECOVERY}")
    return data


try:
    _BOOT_STATE = _read_state(_PROJECT_ROOT)
except SourceHealthError:
    # The updater must remain importable to repair an interrupted publication.
    _BOOT_STATE = {"status": "unhealthy", "generation": None}


@contextmanager
def _claim(path: Path, *, wait: bool = True):
    from pm.filesystem import lock_fd

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not lock_fd(fd, wait=wait, timeout=10 if wait else 0):
            raise SourceHealthError("Hermes source update is busy. Wait for it to finish and retry.")
        yield
    finally:
        os.close(fd)


def _state_directory(root: Path) -> Path:
    directory = root / SOURCE_HEALTH_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".lease-managed").touch(exist_ok=True)
    return directory


@contextmanager
def source_session(root: Path | None = None):
    """Pin source during startup, construction or a turn; refuse unsafe admission."""
    from hermes_cli.runtime_state import lease_directory

    root = Path(root or _PROJECT_ROOT)
    # Immutable package trees cannot be updated in place. Do not require writes
    # there; an existing gate, however, must never be bypassed by permissions.
    try:
        directory = _state_directory(root)
    except OSError as exc:
        if (exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS)
                or _BOOT_STATE["status"] != "ready" or _BOOT_STATE["generation"] is not None
                or (root / SOURCE_HEALTH_DIRECTORY).exists()):
            raise
        # SourceUpdate cannot acquire its mandatory gate here either, so this
        # account cannot update this immutable install in place.
        yield
        return
    with _claim(directory / "admission.lock"):
        current = _read_state(root)
        if current["status"] != "ready":
            raise SourceHealthError(f"Hermes source update is incomplete; sessions are held. {_RECOVERY}")
        if (_BOOT_STATE["status"] != "ready"
                or current["generation"] != _BOOT_STATE["generation"]):
            raise SourceHealthError("Hermes source changed since this process started; restart it before serving sessions.")
        release = lease_directory(directory)
    try:
        yield
    finally:
        release()


def source_session_guard(function):
    """Keep construction and synchronous agent turns inside a source lease."""
    @wraps(function)
    def guarded(*args, **kwargs):
        with source_session():
            return function(*args, **kwargs)
    return guarded


@contextmanager
def source_completion(root: Path, generation: str):
    """Pin a completion child independently of the transport parent's lifetime."""
    from hermes_cli.runtime_state import lease_directory

    root = Path(root)
    directory = _state_directory(root)
    with _claim(directory / "admission.lock"):
        current = _read_state(root)
        if current["status"] != "updating" or current["generation"] != generation:
            raise SourceHealthError("Source update completion no longer owns the health gate; refusing to run.")
        release = lease_directory(directory)
    try:
        yield
    finally:
        release()


@contextmanager
def source_update(root: Path, *, drain_timeout: float = 30):
    """Close admission, drain readers, then allow mutation under one writer.

    An exception/crash after yielding leaves admission closed. Only the fresh
    completion interpreter, after its import self-test, can publish readiness.
    """
    from hermes_cli.runtime_state import leases_held
    from pm.filesystem import durable_write_bytes

    root = Path(root)
    directory = _state_directory(root)
    with _claim(directory / "writer.lock", wait=False):
        status = directory / "status.json"
        with _claim(directory / "admission.lock"):
            previous = status.read_bytes() if status.exists() else None
            generation = uuid.uuid4().hex
            durable_write_bytes(status, json.dumps({
                "schema": 1, "status": "updating", "generation": generation,
            }).encode())
        deadline = time.monotonic() + drain_timeout
        print("→ Holding new sessions while the source update completes...")
        while leases_held(directory):
            if time.monotonic() >= deadline:
                # No source mutation has occurred: give the old runtime back
                # its exact admission state, rather than stranding a busy turn.
                with _claim(directory / "admission.lock"):
                    if previous is None:
                        status.unlink()
                    else:
                        durable_write_bytes(status, previous)
                raise SourceHealthError("Hermes still has active agent work; no source was changed. Retry the update when it is idle.")
            time.sleep(0.05)
        try:
            yield generation
        finally:
            if _read_state(root)["status"] != "ready":
                print(f"⚠ Sessions remain held by the source health gate. {_RECOVERY}")


def publish_source_ready(root: Path, generation: str) -> None:
    """Publish only the generation whose completion just passed the self-test."""
    from pm.filesystem import durable_write_bytes

    root = Path(root)
    directory = _state_directory(root)
    with _claim(directory / "admission.lock"):
        current = _read_state(root)
        if current["status"] != "updating" or current["generation"] != generation:
            raise SourceHealthError("Source update completion does not own the health gate; refusing to reopen sessions.")
        durable_write_bytes(directory / "status.json", json.dumps({
            "schema": 1, "status": "ready", "generation": generation,
        }).encode())


@cache
def require_tool_execution_imports() -> None:
    """Eagerly load the lazy execution graph before constructing the first agent."""
    try:
        importlib.import_module("agent.tool_executor")
        importlib.import_module("agent.conversation_loop")
    except Exception as exc:
        raise SourceHealthError(f"Hermes tool-execution self-test failed: {type(exc).__name__}: {exc}. {_RECOVERY}") from exc


def require_source_health(root: Path | None = None) -> None:
    """Check disk in a fresh selected interpreter, never against cached modules."""
    from hermes_cli.update_cmd_validation import _critical_module_import_failures

    failures = _critical_module_import_failures(root or _PROJECT_ROOT, report_runtime_errors=True)
    if failures:
        detail = "; ".join(f"{name}: {kind}: {error}" for name, (kind, error) in failures.items())
        raise SourceHealthError(f"Hermes source self-test failed; sessions remain held. {detail}. {_RECOVERY}")


if __name__ == "__main__":
    try:
        require_source_health()
    except SourceHealthError as exc:
        raise SystemExit(str(exc)) from exc
    print("Hermes source self-test passed (including the tool-execution graph).")
