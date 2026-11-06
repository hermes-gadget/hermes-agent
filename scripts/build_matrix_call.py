#!/usr/bin/env python3
"""Build the Matrix call helper against the qualified, pinned native SDK."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import matrix_rtc_lab as lab


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path)
    parser.add_argument("--resolve", action="store_true", help="generate a new lockfile for review")
    args = parser.parse_args()
    work = args.work_dir.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    target = (args.target_dir or work / "target").expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "plugins/platforms/matrix/native"
    subprocess.run(["docker", "build", "-t", lab.BUILDER, str(lab.ASSETS)], check=True, timeout=1800)
    with tempfile.TemporaryDirectory(prefix="matrix-call-src-", dir=work) as temporary:
        root = Path(temporary)
        sdk, _ = lab.prepare(root, native_fix=True)
        shutil.copytree(source, sdk / "hermes-call-helper")
        manifest = sdk / "Cargo.toml"
        manifest_text = manifest.read_text(encoding="utf-8-sig")
        manifest.write_text(manifest_text.replace('members = [', 'members = [\n    "hermes-call-helper",', 1), encoding="utf-8")
        if not args.resolve:
            shutil.copyfile(source / "Cargo.lock", sdk / "Cargo.lock")
        command = ["cargo", "build", "-p", "hermes-matrix-call"]
        if not args.resolve:
            command.append("--locked")
        invocation = lab.container(root, *command)
        container_name = "hermes-matrix-call-build-" + uuid.uuid4().hex[:10]
        invocation[invocation.index("--name") + 1] = container_name
        image = invocation.index(lab.BUILDER)
        invocation[image:image] = ["-v", f"{target}:/src/target"]
        try:
            subprocess.run(invocation, check=True, timeout=5400)
        except BaseException:
            subprocess.run(["docker", "rm", "-f", container_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30)
            raise
        shutil.copyfile(sdk / "Cargo.lock", work / "Cargo.lock")
        binary = work / "hermes-matrix-call"
        shutil.copyfile(target / "debug/hermes-matrix-call", binary)
        binary.chmod(0o700)
        print(f"Built {binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
