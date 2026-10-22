# Hermes source-update health gate — backlog #457

## Delivery and scope

- Branch: `burn/update-hardening-20261022`
- Base: `d5034303528f56c089209cf134de0e7a976573a7`
- Implementation commit: c00fd6683d13443e74b9609f3d3a00d8d1db0553
- PR: https://github.com/gadgethd/hermes-agent/pull/2 (draft)
- This review is committed in the following handoff commit; its own hash is available from `git rev-parse HEAD`.
- Execution status: **PARTIAL because of the test-environment scope mistake below**, even if the code deliverables and final checks are complete. No deployment or live gateway restart was performed.

## Change and failure window

The existing post-pull import guard imported `run_agent` but missed the facade's lazy tool executor. A syntactically valid tree lacking `agent.display.tool_row_emoji` could pass that guard, start the gateway, and fail every subsequent tool call. A probe at the end alone also leaves the entire Git/stash/branch or multi-file ZIP mutation interval exposed to old processes with cached modules.

The new `hermes_cli/source_health.py` adds a checkout-wide maintenance gate and reuses the existing PM kernel-lock and runtime lease primitives. Before the first source mutation, one writer publishes a durable `updating` generation, closes admission, and drains startup, agent-construction and whole-turn leases. If readers remain after 30 seconds, it restores the exact prior admission state and changes no source. Crashes after mutation leave the gate closed; process death and marker age never imply readiness. Independent completion-child leases also prevent a successor updater from mutating beneath an orphaned preparation/selected-Python child.

Bootstrap captures the source generation before application or PM imports. Gateway startup, agent construction and each conversation turn refuse an incomplete generation or one published after their process began. Existing conversations retain their cached modules and prompt prefixes; nothing is reloaded midway through a conversation. The guard is shared by every profile using the checkout, rather than tied to one profile's home or PID marker.

Fresh selected-Python probes resolve the real CLI, agent facade, model-tools, toolsets, executor, conversation loop and gateway graph before maintenance and again before readiness publication. Mandatory checks fail closed on missing third-party dependencies, an unexecutable probe or imports from another checkout. Gateway startup and first agent construction eagerly import the lazy execution graph once per process. The probe sets update mode before bootstrap so it cannot recursively launch update completion or external secret-source helpers.

Only the correlated completion owner publishes readiness, before the existing fleet restart. An unrelated partial SQLite-runtime maintenance verdict still fails its receipt under the existing policy, but healthy source is admitted. The historical finish path receives the same mandatory probes. Existing syntax rollback now includes bootstrap and the guard itself. Git status/stash excludes only the checkout's gate; ZIP preserves its enclosing runtime directory. Existing rescue refs, stash recovery, ZIP rollback and fleet verification remain.

This is the least invasive maintenance gate, not a staged/versioned checkout deployment: it preserves the current editable source path, PM ownership and update/rollback pipeline. The intentional tradeoff is a bounded maintenance pause instead of serving old code throughout an update.

## Operator behavior, migration and rollback

- During update, new turns and new agent/gateway startup refuse work. Active turns can finish before mutation. Starting an update from inside an active tool turn therefore times out without changing source; retry from a separate terminal once idle.
- Broken graph startup logs `Gateway refused to start`, names the missing module/symbol and tells the operator to finish/retry `hermes update`, then restart affected processes. Failed post-update health prevents builds, readiness publication and normal fleet restart.
- Gate files live under `<checkout>/.hermes/source-health/`; there is no user config migration or new environment setting. Immutable package trees without an existing gate can serve without writing there; their deployment manager owns replacement.
- `python -m hermes_cli.source_health` checks disk through the selected runtime; it never opens a closed gate. Do not remove the gate to bypass a failed check. Retry the updater to finish the normal dependency/build/restart flow.
- Already-current retry can repair a held gate. Existing syntax/ZIP rollback remains available; no automatic code rollback is added once maintenance has begun. An unchanged tree following a drain timeout keeps its original generation.
- First adoption requires the normal, separately authorized fleet restart: an already-running release without these guards cannot participate. A deliberate downgrade to such a release also loses this protection. `--no-gateway-restart` remains supported, but old guarded processes refuse subsequent turns until restarted.

## Verification

All pytest runs used `scripts/run_tests.sh`, not bare pytest. The final interpreter was built through PM in `.build/burn/testenv`, with temporary home/runtime/cache directories inside this worktree. A local interpreter shim retained the canonical runner's credential scrub, UTC locale and per-file isolation, moved its normally external scratch root under `tmp/burn-tests`, and saved worker output under `.build/burn`. The local Git safety adapter permitted only standalone scratch repositories (including linked Git dirs contained there), with a Git discovery ceiling; live service, config and process guards remained enabled. These local adapters and generated environments are ignored, not product changes.

The acceptance manifest is the union of `tests/hermes_cli/test_update_*.py`, `test_source_*.py`, and `test_cmd_update*.py`, excluding the separate native-socket file, plus:

```text
tests/test_hermes_bootstrap.py
tests/pm/test_bootstrap_import_closure.py
tests/gateway/test_runner_startup_failures.py
tests/gateway/test_startup_connect_parallel.py
tests/agent/test_turn_facade_lease.py
tests/agent/test_tool_executor_checkpoint_paths.py
tests/test_old_updater_compat_surface.py
tests/test_audit_old_updater_imports.py
```

Exact final acceptance command (112 files):

```bash
mapfile -t burn_tests < .build/burn/acceptance-paths.txt
HERMES_HOME="$PWD/.build/burn/home" \
HERMES_RUNTIME_DIR="$PWD/.build/burn/runtime" \
HERMES_PYTHON="$PWD/.build/burn/test-python" \
scripts/run_tests.sh "${burn_tests[@]}" \
  -k 'not container_backend_task_leaves_host_store_untouched' -v -s --tb=short -j6
```

Final acceptance output: **1781 passed, 0 failed, 67 skipped** in 319.9s; one named baseline test excluded. Together with the separate native-socket run: **1812 passed** across 113 files. No pass-on-retry/flaky files were reported.

PROOF: the acceptance command above passed 1781 tests; the broken-symbol output below refuses gateway startup and completion, while the consistent tree passes.

One explicitly excluded checkpoint assertion fails unchanged on both this branch and archived base `d5034303`: `tests/agent/test_tool_executor_checkpoint_paths.py::test_container_backend_task_leaves_host_store_untouched` expects one host checkpoint and observes zero. The base control ran through the canonical runner with the same environment/scratch policy: **21 passed, 1 failed** across that file and `test_update_launch_completion.py`. It is not an update regression and remains out of scope.

Other exact verification commands and results:

```bash
HERMES_HOME="$PWD/.build/burn/home" HERMES_RUNTIME_DIR="$PWD/.build/burn/runtime" \
HERMES_PYTHON="$PWD/.build/burn/test-python" scripts/run_tests.sh \
  tests/hermes_cli/test_source_health.py \
  tests/hermes_cli/test_update_completion_process.py -v -s --tb=short -j2
# 26 passed, 0 failed, 3 Windows tests skipped; 71.4s.

HERMES_HOME="$PWD/.build/burn/home" HERMES_RUNTIME_DIR="$PWD/.build/burn/runtime" \
HERMES_PYTHON="$PWD/.build/burn/test-python" scripts/run_tests.sh \
  tests/hermes_cli/test_source_health.py tests/hermes_cli/test_update_autostash.py \
  tests/hermes_cli/test_update_target_identity.py \
  tests/hermes_cli/test_update_post_pull_syntax_guard.py -v -s --tb=short -j4
# 78 passed, 0 failed, 4 Windows tests skipped; 44.2s.

HERMES_HOME="$PWD/.build/burn/home" HERMES_RUNTIME_DIR="$PWD/.build/burn/runtime" \
HERMES_PYTHON="$PWD/.build/burn/test-short-socket-python" scripts/run_tests.sh \
  tests/hermes_cli/test_update_wedged_gateway.py -v --tb=short -j1
# 31 passed, 0 failed; 9.7s. Real AF_UNIX sockets under temporary checkout-local q/.

git diff --check
# clean
```

Ruff 0.15.10 checked every changed Python file, including both new files: `All checks passed!` The native-socket run used a separate short checkout-local scratch prefix because the full worktree plus normal pytest scratch suffix exceeds AF_UNIX's path limit. No host OS was faked.

### Broken-tree proof

The real E2E copies first-party source, appends `del tool_row_emoji` to the otherwise valid display module and imports the production graph in fresh Python with a temp `HERMES_HOME`. It exercises real `GatewayRunner.start`, the full gateway preflight, `AIAgent()` and update completion. Only transport/build/fleet side effects are replaced; the failing import and admission protocol are real.

Archived base output with the same missing export:

```text
{"probe": [true, null, null], "gateway_started": true, "served": [true]}
AssertionError: BROKEN TREE WAS ALLOWED TO SERVE
```

Fixed-tree output excerpt (temporary absolute paths shortened to `<checkout>`; the broken JSON row omits repeated error fields):

```text
RESULT={"probe": [false, "agent.tool_executor", "cannot import name 'tool_row_emoji' from 'agent.display' (<checkout>/agent/display.py)"], "started": false, "served": [], "events": [], "status": "updating", "full_gateway_started": false, "completion_exit": null, ...}
ERROR:gateway.run:Gateway refused to start: Hermes tool-execution self-test failed: ImportError: cannot import name 'tool_row_emoji' from 'agent.display' (<checkout>/agent/display.py). Finish or retry `hermes update`, then restart the affected Hermes processes.
RESULT={"probe": [true, null, null], "started": true, "served": [true], "events": ["build", "restart", "verify"], "completion_error": null, "status": "ready", "constructor_error": null, "full_gateway_started": null, "completion_exit": null}
```

The broken constructor and post-update errors also name `tool_row_emoji`; neither builds nor restarts run. A separate healthy-source/partial-maintenance row publishes ready while retaining exit 1. Other real-process assertions cover admission during mutation, active-reader drain refusal, wrong-generation publication, concurrent writers, writer death, surviving completion children, stale-process refusal, a fresh process accepting repaired source, two profile homes sharing one gate, and ignored/unignored gate preservation.

## Deliberately left out and open questions

- No live deployment/restart, zero-downtime versioned trees, dependency-policy changes, core model tools, prompt reloads or process-identity changes.
- Arbitrary external Git/file edits and a deployment manager replacing immutable package trees do not participate in this updater protocol.
- Native Windows/macOS execution and actual supervisor restart acceptance still need their normal CI/operator lanes. Existing native-specific tests were skipped, not simulated.
- First-rollout scheduling and mixed fleets containing older unguarded processes require the maintainer's operational decision. The gate cannot retrofit code already loaded in an older process.
- The unrelated baseline checkpoint assertion remains open. The out-of-envelope PM artifacts described below require owner disposition; this task did not authorize modifying or removing them.

## Honest mistake inventory

1. **Scope breach during test setup:** an initial local Python shim resolved its root one directory too high. The runner's pytest probe failed and it fell back to default user PM activation before the temporary-home wrapper was corrected. That created/updated PM runtime artifacts under `/home/ben/.hermes/installs/a563178d71521a99/pm-runtime` and an install lock outside this worktree. I stopped the retained setup process and its observed children, corrected the shim, and moved all later setup/test scratch inside the worktree. I did not delete the outside artifacts or claim a complete cache-write inventory. The live config's modification/change timestamps remained `2026-10-22 03:11:13.600094072 +0100` / `03:20:06.763001667 +0100`, predating the session; no live service restart was invoked. This violates the task envelope despite the config remaining untouched, so the handoff is PARTIAL.
2. An initial literal negative Git pathspec caused `git stash`'s internal add to fail on ignored `.hermes`, exercising an unwanted reset path in disposable test repositories. Native reproduction and the existing rescue-before-reset tests caught it. The final root-anchored glob brackets the leading dot and passes ignored/unignored and all autostash cases.
3. Review caught two incomplete recovery designs before commit: holding healthy source closed after an unrelated partial-maintenance verdict, and relying only on the parent's writer lock while a completion child could survive it. Both were corrected and have behavioral coverage.
4. Some first test fixtures lacked modules now required by the full import graph, used unrealistic ignore rules, or inspected host desktop processes. Those seams were corrected or isolated explicitly. Earlier failing/exploratory runs are not presented as final validation; the known baseline checkpoint failure is disclosed rather than repaired outside scope.
