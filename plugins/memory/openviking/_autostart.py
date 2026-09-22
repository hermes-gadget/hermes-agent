"""Local OpenViking autostart policy and systemd user-unit detection."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("plugins.memory.openviking")

AUTOSTART_MODES = ("auto", "never", "spawn")
LOCAL_SERVER_STARTED = "started"
LOCAL_SERVER_OCCUPIED = "occupied"
LOCAL_SERVER_MANAGED = "managed"
LOCAL_SERVER_DISABLED = "disabled"
LOCAL_SERVER_FAILED = "failed"

_SYSTEMD_UNIT_DETECT_TIMEOUT = 3.0
_SYSTEMD_UNIT_START_TIMEOUT = 10.0
_INVALID_MODE_WARNINGS: set[tuple[str, str]] = set()
_INVALID_MODE_WARNINGS_LOCK = threading.Lock()


def autostart_mode(provider_config: dict) -> str:
    value = provider_config.get("autostart", "auto") if isinstance(provider_config, dict) else "auto"
    mode = value.strip().lower() if isinstance(value, str) else ""
    if mode in AUTOSTART_MODES:
        return mode
    warning_key = ("memory.openviking.autostart", repr(value))
    with _INVALID_MODE_WARNINGS_LOCK:
        first = warning_key not in _INVALID_MODE_WARNINGS
        _INVALID_MODE_WARNINGS.add(warning_key)
    if first:
        logger.warning("Invalid %s value %r; automatic server startup is disabled.", warning_key[0], value)
    return "never"


def _unit_file_exists() -> bool:
    """Check the normal systemd user-unit search paths when the user manager is unavailable."""
    home = Path.home()
    unit_dirs = {
        home / ".config" / "systemd" / "user",
        home / ".local" / "share" / "systemd" / "user",
        Path("/etc/systemd/user"),
        Path("/run/systemd/user"),
        Path("/run/systemd/transient"),
        Path("/run/systemd/generator"),
        Path("/usr/local/lib/systemd/user"),
        Path("/usr/lib/systemd/user"),
        Path("/lib/systemd/user"),
    }
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        value = os.environ.get(env_name, "").strip()
        if value and Path(value).is_absolute():
            unit_dirs.add(Path(value) / "systemd" / "user")
    for value in os.environ.get("SYSTEMD_USER_UNIT_PATH", "").split(os.pathsep):
        value = value.strip()
        if value and Path(value).is_absolute():
            unit_dirs.add(Path(value))
    return any((unit_dir / "openviking.service").is_file() for unit_dir in unit_dirs)


def systemd_user_unit_exists() -> bool:
    """Detect a loaded user unit, with a file-path fallback if systemctl cannot connect."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "cat", "openviking.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SYSTEMD_UNIT_DETECT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    return (result is not None and result.returncode == 0) or _unit_file_exists()


def start_systemd_user_unit() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "openviking.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_SYSTEMD_UNIT_START_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return LOCAL_SERVER_FAILED, f"Could not request systemd user unit openviking.service to start: {e}"
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:240]
        suffix = f": {detail}" if detail else ""
        return LOCAL_SERVER_FAILED, f"Could not request systemd user unit openviking.service to start{suffix}"
    return LOCAL_SERVER_STARTED, (
        "Requested systemd user unit openviking.service to start; Hermes will not spawn a separate process."
    )
