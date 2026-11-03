# STATUS

DONE — Lane F integrates Lane C and Lane D on `burn/gateway-wire-20261103`,
based on `864988b8b6`. Delivery is a branch push to origin; no PR, deployment,
main merge, live configuration edit, or live gateway restart.

Normal gateway startup now composes `reload-plugin` and `status.plugin_health`
into the existing control socket, using the same pinned process home for the
server and plugin handlers. Existing control handlers remain available. The
full CLI entry point can reload one plugin in that running gateway, with hooks
and adapter handlers available immediately. Existing conversation prompts and
model-facing tool schemas remain deferred until the next session.

C's manager-local `_hook_disabled_callbacks` remains the sole quarantine state.
No timed retry, suppression deadline, or abandoned-worker retry fields were
restored. D's targeted unload/re-registration resets that plugin's quarantine
through C's registration-disposal path. Health exposes `quarantined` and
`disabled_hooks` by reading C's state under its lock, and separately tracks actual
worker completion. A finished worker does not re-enable its callback. Structured
`plugin_hook_timeout` warnings originate from C's shared sync/async timeout path.

Operator recovery documentation now covers health → fix blocking operation →
reload one plugin → confirm quarantine cleared. CLI `--json` success/failure and
control-socket JSON health are exercised by the combined tests.

# COMMITS

- `278cbd9a13` — merge Lane C (`b674ba9350`), preserving its original commits.
- `3d940bfcbc` — merge Lane D (`9bb3e13f07`); reconcile diagnostics and stable
  callback snapshots with C's quarantine and cancellation semantics.
- `d263adbd25` — compose normal gateway startup, add the gateway recovery
  regression, and document operator recovery.
- This review is committed separately as `docs(reviews): record Lane F gateway integration proof`.

# PROOF

`PROOF: merge=3d940bfcbcb8a99fce9330850b742817ed0d00ac / test=tests/gateway/test_plugin_reload_composition.py::test_gateway_startup_serves_quarantine_health_and_targeted_reload / test=tests/hermes_cli/test_plugin_reload.py::test_timeout_health_names_plugin_and_retains_abandoned_generation / suite=13 files,250 passed,0 failed; gateway_regressions=3 files,30 passed,0 failed`

The new composition regression has sync and async cases. It calls the normal
`_start_gateway_start_control_socket` startup function with a real Unix server;
it does not supply plugin handlers from the test. A fixture plugin is discovered
from a temporary home, exhausts a configured 0.1-second budget, fails its first
policy invocation closed, appears by name in structured warnings and JSON health,
and stays quarantined after its worker finishes. Later calls skip that callback.
The test changes the fixture's source and runs the complete CLI entry point:

```text
python -m hermes_cli.main plugins reload fixture --json
```

It asserts a successful result, replacement in the same `PluginManager`, a
completed adapter rewire through the existing loaded-event subscriber, recovered
hook output, empty quarantine/timeout health, and unchanged answering PID. It
also exercises the existing `identify`, `rescan-profiles`, and `reload-plugins`
handlers. D's regression additionally reloads while an abandoned worker remains
alive, preserves sibling registrations and cached prompt/schema bytes, and checks
that the retired worker remains visible until completion. D's second regression
proves A→B→A profile scope, unserved-home rejection, JSON errors, and cleanup of
failed registration.

Before proof: temporarily reverse only `gateway/run.py`'s composition patch,
run the new file, and restore the patch via a shell EXIT trap. Result:
**2 failed in 3.08s**, both at `KeyError: 'plugin_health'` from the ordinary
startup socket. Log: `.test-runtime/composition-before.log`.

All verification ran in `/home/ben/worktrees/hermes-agent/gateway-wire-20261103`:

```bash
export HERMES_HOME="$PWD/.test-home" HERMES_RUNTIME_DIR="$PWD/.test-runtime"
export HERMES_GATEWAY_LOCK_DIR="$PWD/.test-runtime/locks"
mkdir -p "$HERMES_HOME" "$HERMES_RUNTIME_DIR"
source ./activate --test-extras '' > .test-runtime/activation.log 2>&1
export HERMES_PYTHON="$PWD/.test-home/installs/0d1f859e85424457/test-environment/gen-5f234362be834043ab35c695022d1b19/venv/bin/python"

scripts/run_tests.sh \
  tests/hermes_cli/test_plugin_hook_timeout_supervisor.py \
  tests/hermes_cli/test_plugins.py \
  tests/hermes_cli/test_plugin_ownership_ledger.py \
  tests/hermes_cli/test_plugin_hook_failure_reporting.py \
  tests/hermes_cli/test_plugin_api_compat.py \
  tests/hermes_cli/test_plugin_historic_compat.py \
  tests/hermes_cli/test_profiles_s6_hooks.py \
  tests/tools/test_model_tools.py \
  tests/hermes_cli/test_plugin_reload.py \
  tests/hermes_cli/test_plugins_cmd.py \
  tests/hermes_cli/test_plugin_event_bus.py \
  tests/gateway/test_late_plugin_rewire.py \
  tests/gateway/test_plugin_reload_composition.py \
  -q -j 4 --file-retries 0 > .test-runtime/combined-verification.log 2>&1

scripts/run_tests.sh tests/gateway/test_control_socket.py \
  tests/gateway/test_control_socket_pause.py \
  tests/gateway/test_multiplex_hot_serve.py \
  -q -j 3 --file-retries 0 > .test-runtime/gateway-regressions.log 2>&1
git diff --check 864988b8b6
git ls-files -u
```

Combined result: **250 passed, 0 failed, 13 files**, runner wall time **39.6s**,
no retries. This is C's complete eight-file set plus D's complete seven-file set
plus the new two-case file, with shared files counted once. C's set totals 170;
D's set totals 199 on the merged tree (C adds a case in their shared plugin test
file). Additional existing gateway regressions: **30 passed, 0 failed, 3 files**,
runner wall time **6.2s**, no retries. Total distinct verification: **280 passed
across 16 files**. Whitespace check is clean and the unmerged-file index is empty.
Logs and the PM-built interpreter remain in locally ignored test directories.

Branch delivery and final head equality are checked with:

```bash
git merge-base --is-ancestor burn/hook-supervisor-20261103 HEAD
git merge-base --is-ancestor burn/plugin-reload-20261103 HEAD
git push -u origin burn/gateway-wire-20261103
git rev-parse HEAD
git ls-remote --heads origin refs/heads/burn/gateway-wire-20261103
git status --short --branch
```

# OPEN QUESTIONS

None blocking. Both input branches are ancestors of the delivered branch. No PR
was attempted, as requested for this private repository.

# RESIDUAL RISK

Python cannot forcibly terminate arbitrary blocked threads or undo their side
effects. Async cancellation is cooperative too. Such workers remain observable;
explicit reload of a still-broken plugin can create another worker. A blocking
plugin-owned unload callback can still delay reload. Later invocations skip a
quarantined policy callback, while healthy policy callbacks keep enforcing their
decisions; this preserves C's requested containment policy.

Tests use shortened configured budgets rather than waiting for the production
30-second default. Unix IPC was verified on Linux; native Windows IPC and the
full repository suite were not run. Pip entry points re-register the installed
module; replacing an installed distribution can require a new process. This is
source delivery only: an already running older gateway does not automatically
acquire new core code. No live gateway, service, or live Hermes home was changed.
