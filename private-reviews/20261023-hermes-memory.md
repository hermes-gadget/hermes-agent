# Hermes host-memory governance — 2026-10-23

## Scope and disposition

HermesMind #629, #584 and #372 share a resource-governance failure: expensive
work overlaps, reclaimable applications compete with session infrastructure,
and failed services keep restarting. This branch adds cooperative job admission
and a fresh health command. Host protection and service quarantine below are
**Ben-run operator actions, not executed by this task**.

Basis: the supplied `BRIEF-hermes-memory.md`, on base
`e7cb5bfab461a5842970d084bf1102f410d73082`, branch
`burn/hermes-memory-20261023`. Implementation commit: `8513552ecb`.
No deployment, service start/stop/restart, emulator/model workload, live database
operation, live configuration edit, or additional agent/session was performed.
All generated files and test scratch are inside this worktree. No credential
files or stored authentication tokens were read or copied.

## Root causes and existing defenses

The brief records that earlyoom killed both session managers on 2026-10-10;
systemd then killed 269 user processes, including the gateway, producing a
4h45m outage. It also records approximately 583/578 restarts for hf-film-web /
hf-ggez-web and simultaneous spatial fits causing the 2026-10-09 wedge. These
are supplied incident facts, not newly collected host measurements. Installed
earlyoom configuration, unit definitions and journals were not inspected here.

The anchored Hermes alternative cannot match an unrelated `mcp-server-*`
prefix; omitting `systemd` leaves session managers without the intended bias.
earlyoom matches the truncated 15-byte **comm**, not argv or a service name.
`--avoid` subtracts 300 from its selection score; it is a preference, not
immunity. Kernel cgroup protection is a separate mechanism. See the
[upstream earlyoom manual](https://github.com/rfjakob/earlyoom/blob/master/MANPAGE.md).

Repository audit:

| Existing implementation | Contract and remaining gap |
| --- | --- |
| `gateway/lifecycle_ledger.py::sample_memory` | Fresh Linux memory/swap measurements used in heartbeat and unclean-exit diagnosis. |
| `gateway/memory_status.py::classify_pressure` / `collect_memory_status` | Existing ok/elevated/critical/unknown tiers and dashboard health. Commit `e11d1ddc7fa352b781d3d4699749831905d40090` explicitly intended these as user-visible signals. |
| `gateway/memory_monitor.py` | Periodic process RSS logging; observation rather than job admission. |
| `hermes_cli/kanban_db_dispatch.py` | Memory-derived worker cap and live dispatch-pressure throttling. Does not reserve a slot for arbitrary tool jobs or serialize 3–6 GiB allocations. |
| `hermes_cli/local_runtime/hardware.py` | Model/GPU fitting budget, rather than host-wide job serialization. |
| `gateway/status.py` | Existing cross-profile, OS-user-local rendezvous directory and cross-platform file-lock primitives, reused here. |
| `hermes_cli/resource_limits.py` | File-descriptor limit management, not host RAM enforcement. |
| `tools/browser_tool_session.py` / `tools/browser_supervisor.py` | Existing dead/wedged browser eviction and bounded supervisor recovery. No browser-code defect was reproduced by this lane. |

There is no checked-in earlyoom installation or definition of the named
host units to patch. Neither earlyoom kill-selection policy nor crash-loop
quarantine can be repaired by changing a gateway process's Python settings.

## Implemented governance

`hermes_cli/heavy_jobs.py::heavy_job(estimated_bytes, wait_seconds=0)` is a
context manager for tools and agents. `hermes resources run` and the lightweight
`python -m hermes_cli.heavy_jobs run` entry point use that same admission path.
The bundled `heavy-job-admission` skill explains agent use through `terminal`.

- A declared peak **strictly greater than 2 GiB** takes one exclusive OS-user
  slot across profiles and checkouts using the same lock directory. Smaller
  jobs still undergo memory preflight.
- The default refuses contention immediately with exit 75. `--wait-seconds`
  provides a finite polling wait, without a persistent queue or FIFO guarantee.
- A fresh sample is taken **after** the slot is obtained. Admission requires
  `MemAvailable >= estimated_peak + max(2 GiB, 15% of MemTotal)`. Swap does not
  count as allocation capacity. Unknown or malformed measurements refuse work.
- The lock reuses `gateway.status` primitives and its stable rendezvous path:
  an absolute `XDG_STATE_HOME`, otherwise the OS user's state directory;
  the existing `HERMES_GATEWAY_LOCK_DIR` override remains usable. Profile homes
  do not create independent slots. The file is never removed or replaced.
- The foreground CLI uses POSIX exec and transfers the locked descriptor to
  the actual job. The job keeps its PID, signals and exit status. Kernel
  ownership disappears on exit or SIGKILL; no stale PID/TTL recovery is needed.
- `hermes resources status` returns fresh JSON with byte counts, reserve and
  the existing pressure classifier: exit 0=ok, 1=elevated/critical, 2=unknown.
  Existing gateway/dashboard heartbeat health remains available as before.

Example:

```sh
hermes resources status
hermes resources run --memory-gib 6 --wait-seconds 300 -- python fit.py
```

This is cooperative admission, not a hard memory limit. Declare the peak for
the entire process tree; keep the foreground owner alive until all children
finish. Do not detach/daemonize, close the inherited lock descriptor, nest heavy
guards, or dispatch remote work from inside the guard. Python API consumers
must wait for child work before leaving the context and run blocking waits off
the event loop. Unguarded work and several small jobs can still overcommit RAM.
Separate OS users or filesystem/container namespaces do not share this lock.
Linux host telemetry does not account for a tighter private cgroup limit.
CLI execution refuses on Windows; unavailable /proc telemetry reports unknown.
Native Windows/macOS admission is not claimed or simulated.

For #372, an illustrative 15 GiB host reserves 2.25 GiB: a 3 GiB job needs
5.25 GiB available; a 6 GiB job needs 8.25 GiB. The actual command uses measured
MemTotal. Run the emulator and model rounds serially, including model loading
and teardown in each slot. If they must coexist inside one round, declare
their combined peak plus worker overhead. Do not claim a 6 GiB combined round
fits until its actual component footprints support that estimate.

For #584, the policy continues to sacrifice browsers before session
infrastructure under severe exhaustion. Admission reduces competing allocations;
it cannot preserve an in-flight browser action after the browser is killed.
Re-establish browser state explicitly before retrying an interrupted action,
especially one with external side effects. No browser stretch commit was
warranted by the repository audit; no emulator was run.

## Ben-run only: earlyoom policy correction — NOT EXECUTED

First inspect the installed service/version and recover incident evidence:

```bash
earlyoom -v
sudo systemctl cat earlyoom.service
sudo systemctl show earlyoom.service -p ExecStart -p FragmentPath -p DropInPaths
sudo journalctl -u earlyoom.service --since '2026-10-10 00:00:00' --until '2026-10-11 00:00:00'
sudo journalctl -u user@$(id -u).service --since '2026-10-10 00:00:00' --until '2026-10-11 00:00:00'
ps -eo pid,ppid,pgid,comm,rss --sort=-rss
```

Use these replacement regex values, retaining unrelated installed flags and
memory/swap thresholds. Prefix matching deliberately covers truncated names.
The broad runtime prefixes preserve the previously documented protection policy;
review actual comm values, since a service implemented as `node` or `python`
does not acquire its unit's name automatically.

```bash
avoid='^(systemd|hermes|mcp-server-|opencode|forge|missiondeck|codex|openviking|node|python|uvx|Xvfb|dockerd|postgres)'
prefer='^(gradle|java|qemu|emulator|chrome|chromium|firefox)'
printf '%s\n' systemd systemd-logind hermes-gateway mcp-server-time python3.14 node | LC_ALL=C grep -E "$avoid"
sudo timeout 5s earlyoom --dryrun -r 1 --avoid "$avoid" --prefer "$prefer"
```

The dry run must report no option/regex errors; timeout exit 124 is expected.
`--prefer` raises kill priority. Check for `-g`: group-wide kills can include
desktop/session peers, so do not add it as part of this fix. These options are
documented in the [earlyoom manual](https://github.com/rfjakob/earlyoom/blob/master/MANPAGE.md).

If the inspected service reads Debian/Ubuntu's `/etc/default/earlyoom`, back it
up and edit **only** the avoid/prefer values in its existing `EARLYOOM_ARGS`:

```bash
sudo cp -a /etc/default/earlyoom /etc/default/earlyoom.pre-hermes-memory-20261023
sudoedit /etc/default/earlyoom
```

Exact replacement flag fragment inside `EARLYOOM_ARGS`:

```text
--avoid '^(systemd|hermes|mcp-server-|opencode|forge|missiondeck|codex|openviking|node|python|uvx|Xvfb|dockerd|postgres)' --prefer '^(gradle|java|qemu|emulator|chrome|chromium|firefox)'
```

If ExecStart uses another configuration mechanism, edit that inspected owner
instead; do not create an unused defaults file. Ben then activates and checks
the earlyoom change in his maintenance window:

```bash
sudo systemctl restart earlyoom.service
sudo systemctl status earlyoom.service --no-pager
sudo journalctl -u earlyoom.service -n 40 --no-pager
```

Rollback for the defaults-file case is the saved file followed by the same
earlyoom restart. No Hermes gateway restart is part of this procedure.

## Ben-run only: diagnose and quarantine crash loops — NOT EXECUTED

Inspect both manager scopes before selecting actions. Save output privately;
unit definitions/journals may contain environment values and must not be
published with this report. Do not reset restart/failure counters beforehand.

```bash
units=(hf-film-web.service hf-ggez-web.service x11vnc.service hermes-gui.service freecad.service streamd-vm-dashboard.service)
for unit in "${units[@]}"; do
  systemctl --user show "$unit" -p LoadState -p ActiveState -p NRestarts -p Result -p ExecMainStatus -p FragmentPath -p Triggers -p TriggeredBy
  systemctl show "$unit" -p LoadState -p ActiveState -p NRestarts -p Result -p ExecMainStatus -p FragmentPath -p Triggers -p TriggeredBy
  systemctl --user cat "$unit"
  journalctl --user -u "$unit" -n 80 --no-pager
done
```

The next block assumes **all listed units are confirmed user services**. For
any system service, operate on that specific service using the system manager
and its `/etc/systemd/system/<unit>.d` directory instead. Check activation
timers/sockets and dependencies first. `hermes-gui` owns a GUI stack, so Ben
must use an independent SSH session before quarantining it; do not separately
kill embedded Xvfb/x11vnc processes.

Use a persistent condition plus `Restart=no`. This also handles locally
authored unit files, where masking may fail because a file already occupies
the mask path. Conditions and restart controls follow
[systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html);
the local-file masking caveat is in
[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html).

```bash
quarantine_root="$HOME/.local/state/hermes/unit-quarantine"
mkdir -p "$quarantine_root"
for unit in "${units[@]}"; do
  mkdir -p "$HOME/.config/systemd/user/$unit.d"
  touch "$quarantine_root/$unit.blocked"
  cat > "$HOME/.config/systemd/user/$unit.d/90-hermes-memory-quarantine.conf" <<EOF
[Unit]
ConditionPathExists=!%h/.local/state/hermes/unit-quarantine/$unit.blocked
[Service]
Restart=no
EOF
done
systemctl --user daemon-reload
systemctl --user disable --now "${units[@]}"
for unit in "${units[@]}"; do
  systemctl --user show "$unit" -p ActiveState -p Restart -p Conditions
done
```

This stops/disables the listed units and prevents subsequent starts while
their markers exist. Diagnose each exit code separately (missing executable,
bad working directory, missing display, port conflict, resource cap, or app
error); the restart counts alone do not identify the failure. Confirm counters
stop increasing and memory recovers. Do not restart the whole list to test it.

After repairing **one** unit, install bounded restart behavior and remove only
that unit's quarantine. Restore enablement only if it was previously enabled.

```bash
unit=hf-film-web.service
cat > "$HOME/.config/systemd/user/$unit.d/80-hermes-memory-restart-limit.conf" <<'EOF'
[Unit]
StartLimitIntervalSec=600
StartLimitBurst=3
[Service]
Restart=on-failure
RestartSec=60s
EOF
rm "$HOME/.config/systemd/user/$unit.d/90-hermes-memory-quarantine.conf"
rm "$HOME/.local/state/hermes/unit-quarantine/$unit.blocked"
systemctl --user daemon-reload
systemctl --user reset-failed "$unit"
systemctl --user start "$unit"
systemctl --user show "$unit" -p NRestarts -p Result -p ActiveState
journalctl --user -u "$unit" -n 40 --no-pager
```

## Verification evidence

Every pytest invocation used `scripts/run_tests.sh`, the existing checkout-local
Python 3.14.7 environment, credential-scrubbed subprocesses, and checkout-local
temporary homes/runtime/scratch. No dependency installation was necessary.

The new test file initially failed collection on the unchanged base because
`hermes_cli.heavy_jobs` did not exist. This proves absence of the requested
capability, not a reproduction of the historical host outage.

Final targeted regression command:

```bash
HERMES_PYTHON="$PWD/.venv/bin/python" \
HERMES_HOME="$PWD/.cache/hermes-memory/home" \
HERMES_RUNTIME_DIR="$PWD/.cache/hermes-memory/runtime" \
HERMES_GATEWAY_LOCK_DIR="$PWD/.cache/hermes-memory/locks" \
TMPDIR="$PWD/.cache/hermes-memory/scratch" HERMES_TEST_FILE_RETRIES=0 \
scripts/run_tests.sh -j 2 \
  tests/hermes_cli/test_heavy_jobs.py \
  tests/skills/test_heavy_job_admission_skill.py \
  tests/gateway/test_memory_status.py \
  tests/gateway/test_host_gateway_lock_refusal.py \
  tests/hermes_cli/test_subcommands_final_batch.py \
  tests/hermes_cli/test_kanban_memory_guard.py
```

Result: **6 files, 37 tests passed, 0 failed in 10.7s**, no retries. Saved output:
`.cache/hermes-memory/targeted.log`. The new invariants exercise real tmpdirs,
cross-process exclusion, A→B→A homes sharing the default XDG lock namespace,
bounded waits, queued success, fresh post-wait refusal, exec ownership,
SIGKILL/exception/normal-exit release, child exit-code propagation, unknown and
low memory, the >2 GiB boundary, the actual top-level parser and native skill
reader. Only memory measurements are substituted; locks are not mocked.

Additional checks:

```bash
.venv/bin/ruff check hermes_cli/main.py hermes_cli/heavy_jobs.py hermes_cli/subcommands/resources.py tests/hermes_cli/test_heavy_jobs.py tests/skills/test_heavy_job_admission_skill.py
.venv/bin/python -m compileall -q hermes_cli/main.py hermes_cli/heavy_jobs.py hermes_cli/subcommands/resources.py tests/hermes_cli/test_heavy_jobs.py tests/skills/test_heavy_job_admission_skill.py
git diff --check
```

All exited 0; Ruff printed `All checks passed!`. The repository's skill
authoring standards are the schema authority. The generic Codex skill-validator
attempt could not import `yaml` in Hermes's environment and additionally uses a
different frontmatter schema; no dependency was installed to work around it.

With the same environment prefix as the regression command:

```bash
scripts/run_tests.sh -j 2 tests/skills/test_authoring_standards.py -k heavy-job-admission
```

Result: **7 tests passed, 0 failed in 1.9s**, no retries. Saved output:
`.cache/hermes-memory/skill-standards.log`.

The lightweight module status/run smoke used the same checkout-local
HERMES_HOME/runtime/lock/scratch variables above:

```bash
.venv/bin/python -m hermes_cli.heavy_jobs status
.venv/bin/python -m hermes_cli.heavy_jobs run --memory-gib 3 -- .venv/bin/python -c 'print("admitted smoke job")'
```

Both exited 0. The real sample reported total=16767909888,
available=7251632128, reserve=2515186484 bytes, pressure=ok; the foreground
command printed `admitted smoke job`. No multi-GiB allocation was performed.

## Deliberately left out, open questions and remaining work

- No automatic rewrite of commands, added core model tools, prompt/cache
  mutation, persistent scheduler, new dependency, or generic process killer.
- No host flags, cgroup settings, swap configuration or unit changes were
  applied. Ben must validate current comm names, manager scopes, dependencies,
  baseline usage and the installed earlyoom flags before using the runbook.
- Tool/agent workflows must adopt the guarded command; this cannot retrofit
  arbitrary already-running jobs. Measure actual emulator/model/browser peaks
  before tightening budgets or adding per-service hard caps.
- Browser continuity remains an operator/workflow concern after a real kill;
  no live browser OOM experiment or host-wide outage reproduction was attempted.
- Private-origin Git access succeeds, but `gh` is authenticated as
  `hermes-gadget`; `gh repo view gadgethd/hermes-agent` and the GitHub connector
  both return 404. Browser connection reports that a trusted browser service
  is unavailable. PR creation requires an authorized account connection;
  reading stored credentials to bridge accounts is outside this envelope.
- Implementation and report were committed and pushed with
  `git push -u origin burn/hermes-memory-20261023`; Git confirmed the new branch
  at report commit `a485f333f896ff84ae6e278ac0ba0eba1db1c3de`.
- A concrete PR creation attempt through the GitHub connector, targeting
  `gadgethd/hermes-agent:main` from this exact branch, returned HTTP 404 from
  `/repos/gadgethd/hermes-agent/pulls`. No PR was created by that attempt.
  Ben was asked to connect an authorized account or open the private compare
  link; no stored credential workaround was attempted.
- Remaining handoff work: open/verify the private PR, then have Ben execute
  the operator actions in a maintenance window. Do not merge, deploy or
  restart services from this lane. This is a PR-access blocker, not a failing
  implementation/test or a claim that host remediation has already happened.

PROOF: `pytest` targeted suites pass + `docs/reviews/20261023-hermes-memory.md` committed at sha a485f333f896ff84ae6e278ac0ba0eba1db1c3de
