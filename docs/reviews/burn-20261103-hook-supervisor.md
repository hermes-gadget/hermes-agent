# STATUS

DONE — Lane C, HermesMind #707 part (b). Branch `burn/hook-supervisor-20261103`, based on
`864988b8b6`, contains the reproduction and fix. Delivery is by git push to the existing origin;
no PR, merge, deployment, gateway restart, or live configuration change.

The invocation that times out still fails closed for `pre_tool_call`. Core disables that callback
for its profile's plugin manager, cancels its coroutine when present, retires its running entries,
and discards late results. Later calls skip only the disabled callback, while healthy hooks still
run and enforce their decisions. Neither elapsed time, worker completion, nor changing the budget
re-enables it. Existing registration teardown and explicit discovery/reload reset quarantine.

The shared bounded dispatch path covers `post_tool_call`, `pre_llm_call`, and other already-bounded
hooks as well. Both synchronous and asynchronous dispatch use the same quarantine. Async dispatch
does not wait for cancellation-resistant plugin cleanup; synchronous callbacks run off the event
loop. Unbounded gateway/message hooks retain their existing contract. Registration order, hook
payloads, tool schemas, and cached session prompts are unchanged.

# COMMITS

- `c72bb882ae` — `test(plugins): reproduce hook timeout poisoning across tool calls`
- `8f3ba7af20` — `fix(plugins): disable timed-out hooks until explicit reload`
- This review is committed separately as `docs(reviews): record Lane C hook supervisor proof`.

# PROOF

`PROOF: before={"error":"pre_tool_call plugin callback timed out or is still running"} / after={"ok":true} / test=tests/hermes_cli/test_plugin_hook_timeout_supervisor.py::test_timed_out_plugin_does_not_poison_tool_calls_until_reload`

The synthetic plugins are written into temporary homes and loaded through real plugin discovery.
The tests execute registered tools through `model_tools.handle_function_call`, with distinct tool
names, call IDs, and sessions. The first call exhausts the configured 0.1-second budget (the same
production mechanism whose default remains 30 seconds); the next succeeds. A separate healthy
policy hook continues to deny requests. Async callbacks acknowledge cancellation; synchronous
workers are released during cleanup. Both callback forms stay disabled after late completion and
after the timeout is set to zero, then run again after `discover_and_load(force=True)`.

Additional cases exercise A→B→A with real temporary profile homes under multiplexing, healthy
hooks beside a timed-out hook, sync/async dispatch sharing quarantine, cancellation-resistant
coroutines, no replacement worker leaks, and completion/reload races at the timeout boundary.
The async matrix covers `pre_tool_call`, `post_tool_call`, and `pre_llm_call`.

Commands were run from `/home/ben/worktrees/hermes-agent/hooksup-20261103` with this isolated setup:

```bash
export HERMES_HOME="$PWD/.test-home" HERMES_RUNTIME_DIR="$PWD/.test-runtime"
mkdir -p "$HERMES_HOME" "$HERMES_RUNTIME_DIR"
source ./activate --test-extras '' > .test-runtime/activation.log 2>&1
export HERMES_PYTHON="$PWD/.test-home/installs/576cae54a1d45e7d/test-environment/gen-dad772d597dd448b9ea8c58ac679b584/venv/bin/python"
```

Before the core edit (core still at `864988b8b6`; reproduction preserved in `c72bb882ae`):

```bash
scripts/run_tests.sh tests/hermes_cli/test_plugin_hook_timeout_supervisor.py -q --file-retries 0 > .test-runtime/before.log 2>&1
```

Result: **5 failed in 6.14s**, exit 1. Both real tool-dispatch cases failed on the second call with
the exact incident error above; async dispatch also re-ran callbacks after their timeouts.

Final verification:

```bash
scripts/run_tests.sh tests/hermes_cli/test_plugin_hook_timeout_supervisor.py tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugin_ownership_ledger.py tests/hermes_cli/test_plugin_hook_failure_reporting.py tests/hermes_cli/test_plugin_api_compat.py tests/hermes_cli/test_plugin_historic_compat.py tests/hermes_cli/test_profiles_s6_hooks.py tests/tools/test_model_tools.py -q -j 4 --file-retries 0 > .test-runtime/verification-final.log 2>&1
git diff --check
```

Result: **8 files, 170 tests passed, 0 failed**, exit 0, runner wall time 16.6s; no retries.
The new regression file accounts for 11 passing cases. `git diff --check` produced no errors.
Logs and PM-built environments are retained in the ignored `.test-runtime` and `.test-home`
directories. The canonical runner additionally supplies its own isolated per-file test homes.

Branch delivery:

```bash
git push -u origin burn/hook-supervisor-20261103
git rev-parse HEAD
git ls-remote --heads origin refs/heads/burn/hook-supervisor-20261103
```

The last two commands verify that the remote branch points at the delivered local commit.

# OPEN QUESTIONS

No blocker. Lane D owns the health/CLI-reload surface; none is implemented here. Integration note:
quarantine now lives in manager-local `_hook_disabled_callbacks`, keyed by `(hook_name, id(callback))`
with the callable retained as the value. The old timed suppression/abandoned-worker retry fields
are removed. Existing `unload()`/registration disposal plus discovery reset the state; a late
deadline from a retired invocation cannot disable its replacement. Lane D can build its reporting
surface around this state and the existing reload lifecycle without restoring timed retries.

# RESIDUAL RISK

Python cannot forcibly terminate arbitrary synchronous plugin code. A timed-out synchronous worker
is detached and disabled, so it cannot block subsequent dispatch or spawn automatic replacements,
but it can retain resources or produce side effects until it exits. Async cancellation is also
cooperative; a callback that ignores it may continue running, although dispatch remains bounded.
Repeated explicit reloads of a still-broken plugin can create more such workers. Plugin-side I/O
timeouts remain necessary.

Subsequent tool calls intentionally proceed without the disabled policy callback; healthy policy
hooks still enforce their decisions. This is the requested callback-scoped containment policy.
The test suite verifies the shared budget mechanism with a shortened configured budget, not a
30-second production incident. No live service was probed or restarted, and the full repository
suite was not run; the targeted dispatch, ownership, compatibility, profile-hook, and tool suites
are green.
