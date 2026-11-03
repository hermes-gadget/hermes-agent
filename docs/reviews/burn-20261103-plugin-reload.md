# STATUS

BLOCKED on gateway integration; Lane D implementation is committed and pushed on
`burn/plugin-reload-20261103`, based on `864988b8b6`. This is an integration handoff,
not a claim that the default gateway now exposes single-plugin reload.

Implemented within Lane D's boundary:

- Named, structured `plugin_hook_timeout` warnings and profile-local hook health.
- `PluginManager.reload_plugin(name)`: canonical-key resolution, targeted ledger
  teardown, fresh directory module registration, activation notification, and
  cleanup of partial registration failures. Other plugins keep their registrations.
- A stable hook snapshot during replacement, without changing timeout budgets,
  suppression rules, cancellation, or existing conversations' prompts/tool schemas.
- `hermes plugins reload <name> [--json]`, with non-zero failure and no reload-all
  or gateway-restart fallback.
- Additive handlers for the existing gateway control socket: `reload-plugin` and
  `status.plugin_health`. The normal gateway startup has not composed these yet.
- CLI reference and plugin user documentation.

No `gateway/` files were edited. No live Hermes configuration was edited,
and no gateway or other live service was restarted. All fixtures and probes used
temporary Hermes homes. The repo test runner supplied its isolated test homes.

# COMMITS

- `2df30ed1ab` — `feat(plugins): attribute stuck hooks and reload one plugin`
- `7ac3d26636` — `feat(cli): add targeted plugin reload control client and contract`
- This handoff is recorded in the following documentation commit; use
  `git log 864988b8b6..HEAD --oneline` for its SHA.

# PROOF

All commands ran from `/home/ben/worktrees/hermes-agent/reload-20261103`.
The worktree had no local `.venv`, so tests used the existing interpreter without
installing or mutating its dependencies:

```bash
export HERMES_HOME="$PWD/.test-home"
export HERMES_RUNTIME_DIR="$PWD/.test-runtime"
export HERMES_GATEWAY_LOCK_DIR="$PWD/.test-runtime/locks"
export HERMES_PYTHON=/home/ben/hermes-agent-private/.venv/bin/python
export HERMES_TEST_FILE_RETRIES=0

scripts/run_tests.sh tests/hermes_cli/test_plugin_reload.py \
  tests/hermes_cli/test_plugin_ownership_ledger.py \
  tests/hermes_cli/test_plugin_hook_failure_reporting.py \
  tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugins_cmd.py \
  tests/hermes_cli/test_plugin_event_bus.py tests/gateway/test_late_plugin_rewire.py -q
```

Result: **198 passed, 0 failed**, seven files, no retries. After adding explicit
partial-registration cleanup and its regression assertions, the affected subset
was checked again:

```bash
scripts/run_tests.sh tests/hermes_cli/test_plugin_reload.py \
  tests/hermes_cli/test_plugin_ownership_ledger.py tests/hermes_cli/test_plugins.py -q
```

Final result: **116 passed, 0 failed**, three files, no retries.

The two new regressions were also run with the tracked implementation diff
temporarily reversed to base `864988b8b6`, then restored. Both failed as expected:
base has no hook-health state and argparse rejects `plugins reload`.

Permanent regression contracts:

- `tests/hermes_cli/test_plugin_reload.py::test_timeout_health_names_plugin_and_retains_abandoned_generation`
  loads a real fixture from a temporary home, exceeds the configured hook budget,
  verifies structured plugin/hook attribution, reloads while an old worker remains
  alive, exercises dispatch during teardown, and verifies new hooks/tools plus
  untouched sibling registrations and cached prompt/schema values. Old workers
  remain visible as unregistered until they finish.
- `tests/hermes_cli/test_plugin_reload.py::test_cli_reload_over_control_socket_is_single_plugin_and_profile_scoped`
  uses the real Unix control server/client, parser and command dispatcher, with
  the proposed handler composition supplied explicitly. It proves A→B→A under
  multiplex scope, served-home rejection, named activation events, JSON output,
  non-zero unknown-plugin failure, and cleanup after a failed registration.

The bounded standalone probe started only an isolated fixture control server,
explicitly composing `plugin_control_handlers`, and invoked the full
`python -m hermes_cli.main` entry point through a temporary `hermes` launcher:

```text
PROOF: cmd='hermes plugins reload fixture' -> exit=0
Reloaded plugin 'fixture' in the running gateway. Hooks are active; tool schemas and system prompt changes apply next session.

cmd='hermes plugins reload missing --json' -> exit=1
{"error": "Unknown plugin: missing", "plugin": "missing", "reloaded": false, "home": "<temporary home>"}

hook after=["recovered generation"]
```

Before reload, the real `status` response named `fixture`, `pre_llm_call`, and
`hermes_plugins.fixture.callback`, with `running_workers: 1, registered: true`.
After reload, the same abandoned worker remained visible with `registered: false`.
The matching log line contained JSON fields `plugin`, `hook`, `callback`, `home`,
`timeout_seconds`, and `timed_out_at`.

```bash
/home/ben/hermes-agent-private/.venv/bin/python -m hermes_cli.main plugins reload --help
git diff --check
git diff 864988b8b6..HEAD --name-only -- gateway
git push -u origin burn/plugin-reload-20261103
git ls-remote --exit-code origin refs/heads/burn/plugin-reload-20261103
```

Help exposes `reload [-h] [--json] name`; whitespace check passed; the gateway
diff is empty. The remote branch is pushed; no PR was attempted.

Docs updated:

- `website/docs/reference/cli-commands.md`
- `website/docs/user-guide/features/plugins.md`

# OPEN QUESTIONS

**Who will compose Lane D's handlers in gateway startup and connect Lane C's
budget-exceeded reporting?** The brief explicitly prohibits Lane D from editing
those files, and this integration is required before the objective is complete.

Concrete additive contract for the coordinator/Lane C:

1. In `gateway/run.py` at the existing `GatewayControlServer(verb_handlers=...)`
   construction, compose
   `hermes_cli.plugins_control.plugin_control_handlers(runner, _main_loop, socket_home)`.
   Pass the same pinned process home that `GatewayControlServer` uses, obtained
   through `gateway.status._get_process_hermes_home()`. This supplies `reload-plugin`
   and enriches the existing `status` verb; keep the existing reload-all verb and
   other control handlers. There is no second listener or transport.
2. `reload-plugin` accepts `{"name": "canonical-key", "home": "served-profile-home"}`.
   It rejects unserved homes before binding the complete `_profile_runtime_scope`,
   calls `manager.reload_plugin(name)`, and uses the existing loaded-event listener
   for adapter rewiring. Success reports `reloaded`, `plugin`, `home`, `activation`,
   and `handlers_rewired`; failure reports `reloaded: false` and `error`.
3. `manager.hook_health` exposes `owner(hook, callback)`,
   `timed_out(hook, callback, timeout, plugin=..., worker=optional_token)`,
   `finished(hook, callback, token)`, and `snapshot()`. Lane D already instruments
   the general plugin dispatcher's existing sync/async timeout sites without
   changing their decisions. Lane C must reconcile its gateway hook timeout site
   with this attribution contract. `gateway/hooks.py::HookRegistry` (HOOK.yaml
   handlers) is a separate registry; this branch does not claim to reload those
   directory hooks or to instrument Lane C's unpublished timeout implementation.

Inspection of current `gateway/run.py:5675` confirms only `reload-plugins` is wired.
The explicit handler composition in the fixture tests is integration evidence,
not evidence that the default runtime has already adopted it. Production wiring
and end-to-end verification of that wiring remain outstanding.

# RESIDUAL RISK

- Until gateway composition lands, ordinary running gateways reject `reload-plugin`
  and do not expose the new health field. The CLI fails clearly and never silently
  reloads all plugins or restarts the gateway.
- Reload cannot terminate abandoned Python threads or undo arbitrary plugin side
  effects. Old workers remain observable. A blocking `on_unload` callback can still
  hold up reload; cancellation belongs to Lane C and was not changed here.
- Python tool registrations change globally for this profile; cached model-facing
  schemas and system prompts remain unchanged. New sessions receive new schemas
  and prompt sections. In-flight callbacks may finish on their original generation.
- Pip entry-point reload re-registers the installed module; distribution replacement
  requires a fresh process. Category-owned providers and deferred platforms are
  refused rather than falsely reported as reloaded.
- Unix IPC was exercised on Linux. The shared client retains the existing Windows
  pipe transport, but this lane did not run native Windows IPC tests.
