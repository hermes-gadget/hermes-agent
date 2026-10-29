#!/usr/bin/env python3
"""Reproduce Hermes's native MatrixRTC transport gates without live credentials.

Uses an isolated, loopback-only Docker lab and the SDK's locked workspace.
Nothing in this script logs in to the production homeserver or restarts Hermes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

SDK_URL = "https://github.com/element-hq/matrix-rust-rtc.git"
SDK_REV = "de417a84c566932192c011d9931403b304b9156b"
MATRIX_SDK_REV = "c6ad52ecc09eaa36601f55effb0a48a533eb87dd"
FIXED_MATRIX_SDK_REV = "f076a9de6f59b43509d0cbbd4b1077c5c58d2c22"
BUILDER = "hermes-matrix-rtc-build:20261029"
PROJECT = "hermes-matrix-rtc-lab"
ASSETS = Path(__file__).resolve().with_name("matrix_rtc")
FORMATS = ("current", "sticky", "state")


def run(args, *, cwd=None, log=None, env=None, timeout=5400):
    if log is None:
        return subprocess.run(args, cwd=cwd, env=env, check=True, timeout=timeout)
    with log.open("w") as stream:
        stream.write("Command: " + " ".join(map(str, args)) + "\n")
        stream.flush()
        return subprocess.run(args, cwd=cwd, env=env, stdout=stream,
                              stderr=subprocess.STDOUT, timeout=timeout)


def prepare(root: Path, *, native_fix=False):
    root.mkdir(parents=True, exist_ok=True)
    sdk = root / "matrix-rust-rtc"
    if not sdk.exists():
        run(["git", "init", str(sdk)])
        run(["git", "fetch", "--depth=1", SDK_URL, SDK_REV], cwd=sdk)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=sdk)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=sdk, text=True).strip()
    if revision != SDK_REV:
        raise ValueError(f"SDK checkout must be at {SDK_REV}; refusing to replace {revision}")
    fix_paths = ("Cargo.toml", "Cargo.lock", "crates/matrix-rtc-matrix-sdk/src/sdk.rs")
    if native_fix:
        patch = ASSETS / "native-delayed-sticky.patch"
        implementation_diff = subprocess.check_output(
            ["git", "diff", "--binary", "HEAD", "--", *fix_paths], cwd=sdk)
        if not implementation_diff:
            run(["git", "apply", str(patch)], cwd=sdk)
        elif implementation_diff != patch.read_bytes():
            raise ValueError("SDK implementation differs from the qualified native fix")
    changed = subprocess.check_output(["git", "diff", "HEAD", "--name-only"], cwd=sdk, text=True).splitlines()
    allowed = {"crates/matrix-rtc-call-sdk/tests/e2e_call/main.rs"}
    if native_fix:
        allowed.update(fix_paths)
    if set(changed) - allowed:
        raise ValueError("SDK has implementation or dependency edits; baseline qualification requires the pinned sources")

    tests = sdk / "crates/matrix-rtc-call-sdk/tests/e2e_call"
    main = tests / "main.rs"
    # These two test files are the only SDK sources this lab owns.
    original = subprocess.check_output(["git", "show", "HEAD:crates/matrix-rtc-call-sdk/tests/e2e_call/main.rs"], cwd=sdk)
    patched = original.replace(b"mod provision;", b"mod hermes_gate;\nmod provision;", 1)
    if main.read_bytes() not in (original, patched):
        raise ValueError("SDK test harness has unrelated edits; refusing to overwrite")
    main.write_bytes(patched)
    shutil.copyfile(ASSETS / "hermes_gate.rs", tests / "hermes_gate.rs")

    lab = root / "matrix-rtc-lab"
    for relative in ("docker-compose.yml", "synapse/homeserver.yaml", "synapse/log.config",
                     "synapse/lk-jwt-appservice.yaml", "livekit/livekit.yaml", "livekit/livekit2.yaml"):
        source = sdk / "demo/backend" / relative
        text = source.read_text()
        text = text.replace("matrix-rtc-backend", PROJECT).replace("6080", "16080").replace("6081", "16081")
        text = text.replace("ghcr.io/element-hq/synapse:v1.161.0", "ghcr.io/element-hq/synapse:v1.164.1")
        text = text.replace("ghcr.io/element-hq/lk-jwt-service:0.8.0", "ghcr.io/element-hq/lk-jwt-service:0.9.0")
        text = text.replace("livekit/livekit-server:v1.10.1", "livekit/livekit-server:v1.13.11")
        if relative == "docker-compose.yml":
            for port in ("8008:8008", "7880:7880", "7881:7881", "16080:16080", "7890:7890",
                         "7891:7891", "16081:16081", "50100-50200:50100-50200", "50300-50400:50300-50400"):
                text = text.replace('"' + port, '"127.0.0.1:' + port)
        target = lab / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return sdk, lab


def compose(root, *args):
    return ["docker", "compose", "-p", PROJECT, "-f", str(root / "matrix-rtc-lab/docker-compose.yml"), *args]


def container(root, *command, format="current"):
    # The user's Rust installation is mounted, with system libraries confined to
    # a disposable builder. userns=host is needed on userns-remapped Docker for
    # host networking; it does not give the process root or the Docker socket.
    home = Path.home()
    return ["docker", "run", "--rm", "--name", "hermes-matrix-rtc-compile", "--userns", "host",
            "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "3", "--memory", "6g",
            "-e", f"HOME={home}", "-e", f"CARGO_HOME={home}/.cargo", "-e", f"RUSTUP_HOME={home}/.rustup",
            "-e", f"PATH={home}/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "-e", "LIVEKIT_SERVICE_URL=http://localhost:16080", "-e", "LIVEKIT_SERVICE_URL_2=http://localhost:16081",
            "-e", f"HERMES_RTC_TEST_FORMAT={format}", "-e", "RUST_LOG=warn",
            "-v", f"{home}/.cargo:{home}/.cargo", "-v", f"{home}/.rustup:{home}/.rustup:ro",
            "-v", f"{root}/matrix-rust-rtc:/src", "-w", "/src", BUILDER, *command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "build", "test", "down"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--native-fix", action="store_true", help="apply the pinned delayed-sticky SDK fix")
    parser.add_argument("--format", choices=FORMATS, action="append", dest="formats")
    args = parser.parse_args()
    root = args.work_dir.expanduser().resolve()
    if args.stage == "down":
        # Removes only the lab's named volumes, never a production volume.
        run(compose(root, "down", "-v"))
        return 0
    if args.stage == "prepare":
        prepare(root, native_fix=args.native_fix)
        return 0
    if args.stage == "build":
        prepare(root, native_fix=args.native_fix)
        run(["docker", "build", "-t", BUILDER, str(ASSETS)])
        build = run(container(root, "cargo", "test", "--locked", "-p", "matrix-rtc-call-sdk",
                              "--features", "matrix-sdk,testing", "--test", "e2e_call", "--no-run"),
                    log=root / "native-build.log")
        print(f"Native build exit={build.returncode}; log={root / 'native-build.log'}")
        return build.returncode

    prepare(root, native_fix=args.native_fix)
    results = []
    try:
        run(compose(root, "up", "-d", "--wait"), timeout=600)
        for format in args.formats or FORMATS:
            for gate in ("encrypted_duplex", "crash_cleanup"):
                started = time.monotonic()
                log = root / f"{format}-{gate}.log"
                result = run(container(root, "cargo", "test", "--locked", "-p", "matrix-rtc-call-sdk",
                                       "--features", "matrix-sdk,testing", "--test", "e2e_call", "--",
                                       "--ignored", "--exact", f"hermes_gate::{gate}", "--nocapture",
                                       "--test-threads=1", format=format), log=log, timeout=360)
                row = {"format": format, "gate": gate, "exit_code": result.returncode,
                       "elapsed_seconds": round(time.monotonic() - started, 2), "log": log.name}
                results.append(row)
                print(json.dumps(row), flush=True)
    finally:
        receipt = {"sdk_revision": SDK_REV, "synapse": "1.164.1", "livekit": "1.13.11",
                   "lk_jwt_service": "0.9.0", "production_ready": False, "results": results,
                   "native_fix": args.native_fix,
                   "matrix_sdk_revision": FIXED_MATRIX_SDK_REV if args.native_fix else MATRIX_SDK_REV,
                   "lockfile_sha256": hashlib.sha256((root / "matrix-rust-rtc/Cargo.lock").read_bytes()).hexdigest()}
        if args.native_fix:
            receipt["source_patch_sha256"] = hashlib.sha256((ASSETS / "native-delayed-sticky.patch").read_bytes()).hexdigest()
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        run(compose(root, "down", "-v"), timeout=120)
    return 0 if results and all(r["exit_code"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
