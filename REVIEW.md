# Review: state.db turn-lease contention

## Change and rationale

Implementation commit: `73a6060c79595bc20e7e3467f45e8b63b3334574`.
Base: `d5034303528f56c089209cf134de0e7a976573a7` (also the remote main verified at task start).
Branch: `burn/state-db-lease-20261022`.
PR: https://github.com/gadgethd/hermes-agent/pull/1
Validation report commit: `7dcc635b519c855dd8471da2f6ee7a43346f852b`.

The brief's original immediate-abort path was already partly fixed on this base by
`3cfaae9b7b89d483d2889cc194afc384b36c486b` (October 12): refresh errors get a bounded
90-second grace with 5-second retries; a holder-qualified UPDATE miss still stops
immediately. This change preserves that policy and proves it with real SQLite
transactions instead of mocked refresh results. Timeout, grace and TTL settings,
model prompts, tool schemas, profile resolvers, live configuration and services
are unchanged.

Two remaining problems are fixed:

1. `sweep_orphaned_sessions()` previously called its full-store discovery SELECT
   and validated every candidate after `_execute_write()` had acquired
   `BEGIN IMMEDIATE`. Even that read-only discovery held the WAL writer. It now
   discovers through `_read_all()` without reserving the WAL writer, then handles
   at most 100 candidates per committed write transaction, yielding 50ms between
   batches. Each batch rechecks source, age/activity, pin, gateway heartbeats,
   ended state and current turn/compression ownership under the same transaction
   that closes its rows. A shortlist never authorizes a stale mutation.
2. Session turn acquisition and refresh computed `expires_at` before waiting for
   the write lock. A successful call therefore granted a TTL shortened by its
   lock wait; with a short TTL it could return an already-reclaimable lease. The
   clock now runs inside the acquired transaction, including a fresh reclaim
   decision on every acquisition retry. Holder-qualified refresh and release,
   lineage resolution and transcript fencing are preserved.

The sweep remains non-destructive: it closes orphan rows and retains messages.
Batching intentionally allows partial progress on an error; a subsequent sweep
can resume remaining open candidates. It never reopens or rewrites a completed
row. The developer storage guide documents this contract.

## Lock-holder analysis

`SessionDB._execute_write()` in `hermes_state.py` acquires the WAL writer before
calling its callback, rolls back on callback failure, and commits before periodic
maintenance. Its 20-second routine / 60-second transcript patience budgets govern
lock acquisition retries, **not callback duration**. The SQLite connection has a
1-second busy timeout plus jittered Python retries. Raising those budgets alone
would leave the cause intact.

The triggering base lines are [writer reservation at 1022](https://github.com/gadgethd/hermes-agent/blob/d5034303528f56c089209cf134de0e7a976573a7/hermes_state.py#L1022),
[sweep discovery at 182–197](https://github.com/gadgethd/hermes-agent/blob/d5034303528f56c089209cf134de0e7a976573a7/hermes_state_maintenance.py#L182),
and [pre-wait acquisition time at 559](https://github.com/gadgethd/hermes-agent/blob/d5034303528f56c089209cf134de0e7a976573a7/hermes_state_compression.py#L559).

SQLite's [checkpoint API documentation](https://www.sqlite.org/c3ref/wal_checkpoint_v2.html)
distinguishes the checkpoint lock from the writer lock: FULL, RESTART and
TRUNCATE also obtain writer ownership; PASSIVE does not. Its
[WAL concurrency documentation](https://www.sqlite.org/wal.html#concurrency)
explains why only one transaction can own the WAL writer at a time.

| Writer / path | Work done while holding writer authority | Finding / treatment |
| --- | --- | --- |
| `hermes_state_maintenance.sweep_orphaned_sessions` | Full discovery scan, per-row lineage/guard checks and every close in one transaction | Reproduced a real 21s lock hold on base by pausing at the discovery SQL boundary; fixed with read discovery and independent bounded batches. |
| `prune_sessions`, `prune_empty_ghost_sessions`, bulk session deletion | Candidate/guard selection, parent detachment, FTS-triggered message deletes and prompt cleanup | Can be long for large backlogs. Existing `_id_chunks()` limits SQL variable counts, not transaction lifetime. Atomic deletion/lineage semantics need a separate design before segmenting these operations. |
| `append_messages_batch`, `_insert_message_rows`, replacement/compaction and `publish_compression_child` | Transcript reconciliation, serialization, row snapshots, per-row FTS triggers and lineage publication | Can be long with large payloads. Normal single-message encoding already happens outside its transaction; batch copies can opt into `chunk_rows`. Publication/fencing must remain atomic. |
| `import_sessions` | Up to 50,000 messages / 25MB in one callback, including FTS triggers and parent attachment | A bounded payload is not a wall-clock bound; slow disks or large indexes can exceed routine lease-write patience. Unchanged atomic import contract. |
| `optimize_fts`, `rebuild_fts`, `_recover_stale_fts_locked` | Whole-index optimize/rebuild; stale recovery combines index rebuilds under `BEGIN IMMEDIATE` | Intrinsically potentially long writers. The existing incremental merge/backfill paths already use bounded commands/chunks and a duty cycle. No structural-index changes in this patch. |
| Schema `_rebuild_table` and data migrations | Table copies and schema/data surgery | Must preserve transactional schema visibility. These can hold the writer until the whole operation commits. |
| VACUUM / repair promotion | Whole-file rewrite, or transactional backup promotion under repair ownership | Potentially long; auto-VACUUM already skips other known holders, and repair works on scratch copies before exclusive promotion. No repair/admission changes. |
| Queued token accounting / metadata / lease writes | One `_execute_write` callback per claimed batch or mutation | Usually short but inherit writer contention; SQLite triggers and batch size determine actual duration. Turn-lease expiry is now based on time after lock acquisition. |
| PASSIVE checkpoints (periodic and close) | Checkpoint work and the instance's Python connection lock | A PASSIVE checkpoint does **not** hold the WAL writer lock (checkpoint byte 121 differs from write byte 120). It can delay a refresher sharing the instance's Python lock; do not infer a WAL writer from checkpoint activity alone. TRUNCATE checkpoints and VACUUM are different. |

The named October cron/gateway PIDs establish which process held the lock, not
which callback it was executing. No historical stack traces or live database
queries were taken in this task. The sweep is a demonstrated remaining lock-holder
class; this report does not claim it was the exact callback behind those October
incidents. The original 20–60s transient-error abort is already addressed by the
October 12 grace patch, and is now exercised against actual contention here.

## Verification

All tests run through `scripts/run_tests.sh`, with temporary Hermes homes and
scratch roots inside this worktree. The fixture databases use real imports,
real WAL connections and real writer transactions; refresh results are not mocked.
The paused discovery fixture holds its barrier for 21s. Both contention cases
hold an independent SQLite write transaction for 22s, exceeding production's
unchanged 20s patience, and observe the real grace/retry warning.

### Environment and exact commands

PM built an independent Python 3.14 test environment from the committed lock:

```bash
HERMES_HOME="$PWD/.cache/burn-lease/home" \
HERMES_RUNTIME_DIR="$PWD/.cache/burn-lease/runtime" \
TMPDIR="$PWD/.cache/burn-lease/test-scratch" \
XDG_CACHE_HOME="$PWD/.cache/burn-lease/xdg" \
/usr/bin/python3 -B -m pm.build_env --source . --out .venv \
  --python "$PWD/.cache/burn-lease/runtime/python-3.14.7+20260901-linux-x64/bin/python3.14" \
  --cache "$PWD/.cache/burn-lease/pm-cache" --group dev --group test
```

`.cache/burn-lease/python` is a local launcher for `.venv/bin/python`. For the
canonical runner it imports `scripts.run_tests_parallel`, redirects only
`_runner_scratch_root()` to `.cache/burn-lease/test-scratch`, and invokes the
unchanged `main()`. It also selects the temporary development Hermes home/runtime
and local caches. The test runner still scrubs credentials and isolates each
file. This launcher is necessary because the canonical runner otherwise writes
its per-file temporary roots under `/var/tmp`, outside this task's write envelope.
No production test guard was bypassed.

Final base/fix regression command:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/test_orphan_sweep_contention.py \
  tests/agent/test_turn_lease_sqlite_contention.py --file-retries 0 -j 2
```

The base proof substitutes only the two changed production Python files with
`git show d5034303528f56c089209cf134de0e7a976573a7:<file>` and verifies that **all
tracked production Python then equals that base**; only the new regression tests
differ. A `finally` block restores the committed implementation. Receipt:
`.cache/burn-lease/base-proof-driver.log` and `base-final-tests.log`.

- Final base receipt: **2 passed, 4 failed**, 2 files, 136.5s runner wall,
  exit 1, no retries. Both sweep regressions and both acquisition/refresh TTL
  regressions failed at their intended behavioral assertions. The 22s transient
  stall and genuine-takeover controls passed, confirming the existing grace and
  owner fence on the base.
- Final fix receipt: **6 passed, 0 failed**, 2 files, 99.1s runner wall,
  exit 0, no retries. Receipt: `.cache/burn-lease/fix-final-tests.log`.
- The takeover negative control must pass on base as well: it asserts a real
  committed owner replacement stops the turn, rejects the stale transcript flush,
  and cannot be deleted by the stale holder's release.

Focused compatibility command:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/test_orphan_sweep_contention.py \
  tests/agent/test_turn_lease_sqlite_contention.py \
  tests/agent/test_turn_facade_lease.py tests/agent/test_cross_process_turn_lease.py \
  tests/hermes_state/test_sweep_orphaned_sessions.py \
  tests/hermes_state/test_session_turn_lease.py --file-retries 0 -j 4
```

Result: **78 passed, 0 failed**, 6 files, 72.9s runner wall, no retries.
Receipt: `.cache/burn-lease/fix-focused-tests.log`.

Full state-directory compatibility command:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/ tests/agent/test_turn_lease_sqlite_contention.py \
  tests/agent/test_turn_facade_lease.py tests/agent/test_cross_process_turn_lease.py \
  --file-retries 0 -j 4
```

Result: **1,123 passed, 52 failed, 16 skipped**, 143 files, 852.3s runner wall,
exit 1, no retries. `tests/hermes_state/test_hermes_state.py` additionally hit its
300-second file timeout; the runner does not count that killed file's partial
test results in the totals. All six new regressions passed within this run too.
Receipt: `.cache/burn-lease/fix-full-state-tests.log`.

The failures span 15 files, largely structural-maintenance admission and recovery.
A separate temporary-database probe using both the fixed and original sweep
returned the same `vacuum_skipped_holders: 53`: all 53 were classified as
uninspectable foreign processes, with zero other in-process holders. The
fail-closed production gate is unchanged. Exact-base comparison of every
failing/timeout file completed with **407 passed, 60 failed**, 15 files, 167.5s
runner wall, exit 1, no retries. Comparing exact failing test IDs found all 52
fixed broad-run failures on the base, with **zero fixed-only failures**. The
eight additional base failures are from the large file killed by the fixed
broad run. These failures are not being hidden or skipped, and their individual
root causes are not all diagnosed by this task.

For the base comparison, the same verified two-source-file substitution and
`finally` restoration were used. The canonical command was:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/test_auto_vacuum_holder_gate.py \
  tests/hermes_state/test_auto_vacuum_in_process_holder_gate.py \
  tests/hermes_state/test_fts_fresh_bootstrap_admission.py \
  tests/hermes_state/test_fts_rebuild_admission.py \
  tests/hermes_state/test_fts_runtime_rebuild.py \
  tests/hermes_state/test_hermes_state.py \
  tests/hermes_state/test_startup_maintenance_lease.py \
  tests/hermes_state/test_state_db_fts_segment_collision_probe.py \
  tests/hermes_state/test_state_db_maintenance_conformance.py \
  tests/hermes_state/test_state_db_malformed_repair.py \
  tests/hermes_state/test_state_db_repair_live_writer_guard.py \
  tests/hermes_state/test_state_db_repair_loop_cap.py \
  tests/hermes_state/test_state_db_repair_loop_mtime.py \
  tests/hermes_state/test_state_db_repair_non_destructive.py \
  tests/hermes_state/test_state_db_wal_unlink_race.py --file-retries 0 -j 4
```

Receipts: `.cache/burn-lease/base-failure-driver.log`,
`base-broader-failures.log` and `failure-comparison.json`.

The formerly timed-out file is also rerun alone on the fixed code:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/test_hermes_state.py --file-retries 0 --file-timeout 600 -j 1
```

Result: **273 passed, 8 failed**, 178.1s runner wall, exit 1, no retries.
The eight failing test IDs exactly match the base large-file result. Across the
broad fixed run and this completed large-file run, all **60 distinct failing
IDs match the exact base**, with no fixed-only or base-only IDs. The original
300s timeout under the larger concurrent run remains disclosed above. The 600s
limit is only this diagnostic test-file wall cap; production SQLite and lease
timeouts are unchanged. Receipt: `.cache/burn-lease/fix-large-file-tests.log`.

```bash
.venv/bin/ruff check --no-cache hermes_state_maintenance.py hermes_state_compression.py \
  tests/hermes_state/test_orphan_sweep_contention.py \
  tests/agent/test_turn_lease_sqlite_contention.py
git diff --check
```

Result: Ruff **all checks passed**; whitespace check clean.

## Continuation completion audit (22 October 2026)

Resumed from `a14984dfd1c453a6da461998e930e9e674008efc` after reading this
report and the git history. The previous turn made concrete progress: it
committed the implementation and verification, pushed the branch and opened
PR #1. Current source inspection confirmed the reported transaction boundaries,
read-pool fallback, owner-qualified refresh/release and transcript fencing.
No further production changes were needed.

Fresh verification extended the focused suite to cross-backend orphan ownership
and compression publication:

```bash
HERMES_PYTHON="$PWD/.cache/burn-lease/python" scripts/run_tests.sh \
  tests/hermes_state/test_orphan_sweep_contention.py \
  tests/agent/test_turn_lease_sqlite_contention.py \
  tests/agent/test_turn_facade_lease.py tests/agent/test_cross_process_turn_lease.py \
  tests/hermes_state/test_sweep_orphaned_sessions.py \
  tests/hermes_state/test_session_turn_lease.py \
  tests/hermes_state/test_orphan_sweep_cross_backend.py \
  tests/hermes_state/test_compression_lease_refresh_before_publish.py \
  --file-retries 0 -j 4
```

Result: **94 passed, 0 failed**, eight files, 79.3s runner wall, exit 0,
no retries. Receipt: `.cache/burn-lease/continuation-audit-tests.log`.
Ruff passed again and `git diff --check d5034303528f56c089209cf134de0e7a976573a7`
was clean. The saved base-proof driver and the actual failure assertions were
inspected; an independent extraction from the broad base/fixed logs again found
60 failing IDs on each side, with no fixed-only or base-only IDs.

| Brief requirement | Completion evidence |
| --- | --- |
| Identify long writers and implement a justified fix | Lock-holder analysis above; discovery moved outside the WAL write transaction, mutation bounded and revalidated; no timeout increase. |
| Real 20s+ stall survival and genuine lease-loss protection | Fresh passing 21s discovery and 22s writer-stall cases, including committed takeover, rejected stale flush and owner-qualified release. |
| Regressions red on base, green on fix | Saved exact-base production proof: four intended failures and two passing controls; all six regressions pass in the fresh run. |
| Branch, PR and committed handoff | Remote branch matched the audited head; PR #1 was OPEN against private-fork `main`, MERGEABLE, with no status checks reported. This report is committed on the branch. |
| Scope and invariants | Only this report changes during the continuation; isolated development/test homes retained. No live writes, restarts, deployment, merge or delegated agents. |

Remote `main` still matched the documented base. The existing automated PR review
reported no findings on `7dcc635b519c855dd8471da2f6ee7a43346f852b`; this is
additional review evidence, not a substitute for tests. The GitHub connector
returned 404 for this private repository, so the existing repository credential
helper was used for readback without exposing or changing credentials.

## Deliberately left out / residual risks / open questions

- No live configuration/database edits, deployment, service restart, system
  package installation, other agents, branch rewriting, force push or merge.
- No separate lease database: it would break same-transaction transcript fencing.
- No timeout increases or indefinite renewal grace. Persisting errors still fail
  closed; a confirmed owner mismatch still hard-interrupts immediately.
- A batch limits candidate count rather than promising a maximum elapsed time.
  One unusually deep lineage, slow SQLite statement, commit/fsync, or same-instance
  Python lock can still stall. The existing bounded grace handles transient errors;
  this change does not promise survival of arbitrary disk hangs or TTL-length stalls.
- Discovery can fall back to the instance's locked writer connection when a WAL
  reader is unavailable. It still opens no write transaction for discovery, but
  another thread sharing that instance may wait for its Python lock.
- The discovery shortlist is materialized in memory. Candidates added after it
  was read wait for the next sweep; existing candidates are always revalidated.
- Bulk destructive retention/import and whole-index/schema operations are
  documented above, not broadly reworked. Their atomicity needs separate treatment
  before further segmentation. Historical callback attribution remains an open
  operational question; this task did not request or perform a live deployment.
- TokenSave returned no symbols for the requested lease/write context, and this
  worktree had no local `.tokensave/tokensave.db`; direct source/history inspection
  supplied the evidence. No extractor/schema defect was established.

## Honest mistake inventory

- I initially tried the existing shared Python 3.11 test environment. Its path
  under the real Hermes root tripped the repository's I/O guard during fixture
  setup (4 setup errors); no guard was disabled and no live config was changed.
- I then explicitly selected system Python 3.12 for the first PM build; the lock
  only supports Python >=3.14. That build failed safely, and the isolated PM runtime
  supplied Python 3.14 for the successful fresh build.
- Plain `gh` selected a different saved credential and could not resolve the
  private repository. Using the existing Git credential helper for that repository
  verified API access without printing or changing credentials.
