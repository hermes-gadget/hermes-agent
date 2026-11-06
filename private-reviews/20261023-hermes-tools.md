# Hermes tools burn handoff — 2026-10-23

Branch: `burn/hermes-tools-20261023`, based on `origin/main` at
`e7cb5bfab461a5842970d084bf1102f410d73082`.
Implementation: `97be87606d40265626e9c44ac6d71a163881dd5b`.
Scope: HermesMind #477 and #471, following the supplied brief.

## #477: dependency installs and foreground-server guidance

The base already contains liuhao1024's fix
`b30862afd30341732f9343d59604fa6075827fba` and subsequent coverage for
`uv pip install --python $TMPDIR/project/.venv fastapi uvicorn`. Those install forms
already pass. The remaining real install command `uv add fastapi uvicorn` was
still rejected by `tools/terminal_tool_guards.py::_has_long_lived_foreground_launch`:
`uvicorn` matched a server pattern, and `uv add` was absent from the install matcher.
Reproduction supplied the actual command text to the production guard; no package
installation was needed or executed.

The same exemption also hid actual launches when an install-like expression
occurred later in a segment: `uvicorn app:app --uds $TMPDIR/pip install` and
`npm run dev -- install` incorrectly passed. The fix recognizes `uv` installs/adds,
stops at the first install/add verb, and exempts only the subsequent dependency
arguments. It still checks the preceding invocation and each later shell segment.
The existing two behavior tests now cover these cases alongside standalone
servers and install-then-launch chains.

This is the foreground/background guidance matcher, not the supervised-gateway
restart/stop guard. The gateway lifecycle protections are unchanged.

## #471: unavailable ddgs must remain an error

On the base, selecting ddgs with its import unavailable reached its existing
missing-package error, then `tools/web_tools.py::_memoized_search` passed that
failure through the one-shot keyless rescue. The real registry dispatch returned
`success: true` with replacement results. The original failure was nested under
`data.backend_error`, so callers could treat the unavailable backend as successful.

Search now applies the existing `_provider_is_ready` predicate before memo lookup
or rescue. An unavailable provider's own search result/error goes directly to the
caller. This also prevents an earlier cached success from hiding a lost dependency.
Ready providers retain their existing cache and annotated runtime-rescue behavior;
keyless-capable providers still count as ready through that predicate.

The ddgs error identifies the missing/unimportable package and uses PM's canonical
`install_hint("ddgs")`: `hermes pm install --extra ddgs`. Its plugin description and
setup comment now agree. No package installation is triggered by this error path.

The existing health surface, `hermes doctor`'s
`_doctor_web_capability_rows`, already uses the same readiness predicate. It reports
`warn` for selected-but-unavailable ddgs and `ok` once importable. No new health API,
provider interface, tool schema, or configuration setting was needed.

The new two regression tests use real configuration files, plugin discovery,
registry dispatch, provider imports, and doctor diagnostics against temporary
`HERMES_HOME` directories. Only the optional import and search I/O boundaries are
faked. Both `web.backend` and `web.search_backend` are exercised through the
unavailable → available → unavailable transition, including a successful cached
query. A separate case proves a ready ddgs runtime failure still receives annotated
rescue. These tests require no search network calls.

## Verification and exact commands

All pytest execution used `scripts/run_tests.sh`, never bare pytest. Logs and
disposable environments are under ignored `.burn/` in this worktree.

Red proof before implementation, using the existing worktree-local Python 3.14.7
test interpreter:

```bash
bash .burn/run-hermes-tools-tests.sh tests/tools/test_blocked_command_guidance.py tests/tools/test_web_backend_unavailable.py -j 2 --file-retries 0 --tb=short
```

Result: **9 passed, 4 failed**, with both guard contracts and both ddgs selection
forms failing for the expected reasons. Log: `.burn/hermes-tools-red.log`.
After implementation, the same command passed **13 tests**
(`.burn/hermes-tools-green.log`).

The first broader run passed 309 tests, failed two Parallel-provider assertions,
and skipped eight host-specific cases. The existing disposable interpreter lacked
the optional Parallel SDK, so the assertions saw its missing-SDK error rather than
the expected missing-key error. That run included the full doctor suite: **60 passed,
8 skipped**. Log: `.burn/hermes-tools-regression.log`.

A fresh environment with the declared web extras resolved the test-environment gap:

```bash
env -i PATH=/usr/bin:/bin HERMES_HOME="$PWD/.burn/runner-home" HERMES_RUNTIME_DIR="$PWD/.burn/runtime" TMPDIR="$PWD/.burn/tmp" .burn/runtime/python-3.14.7+20260901-linux-x64/bin/python3.14 -m pm.build_env --source . --out .burn/hermes-tools-test-env --group dev --group test --extra parallel-web --extra firecrawl --extra exa --cache .burn/hermes-tools-cache
```

Result: exit 0; `.burn/hermes-tools-test-env/bin/python` was built and validated.
Log: `.burn/hermes-tools-build-env.log`. No live environment was modified.

The wrapper used these exports for the final commands below:

```bash
export HERMES_HOME="$PWD/.burn/tools-home"
export HERMES_RUNTIME_DIR="$PWD/.burn/tools-runtime"
export TMPDIR="$PWD/.burn/tmp"
export HERMES_PYTHON="$PWD/.burn/hermes-tools-test-env/bin/python"
```

```bash
scripts/run_tests.sh tests/tools/test_blocked_command_guidance.py tests/tools/test_terminal_heredoc_background_guard.py tests/tools/test_terminal_pre_guard_deadline.py tests/tools/test_terminal_tool.py tests/tools/test_web_backend_unavailable.py tests/tools/test_web_providers_ddgs.py tests/tools/test_web_providers.py tests/tools/test_web_tools_config.py tests/tools/test_web_keyless_fallback.py tests/tools/test_web_keyless_rescue.py tests/tools/test_web_result_cache.py tests/tools/test_web_backend_capability_isolation.py tests/tools/test_web_managed_routing.py tests/plugins/web/test_web_search_provider_plugins.py -j 4 --file-retries 0 --tb=short
scripts/run_tests.sh tests/hermes_cli/test_doctor.py -k web_capability_rows -j 1 --file-retries 0 --tb=short
```

Results: **271 passed, 0 failed across 14 files** in 62.7 seconds, then
**2 passed, 0 failed** for doctor readiness. Retries were disabled. Logs:
`.burn/hermes-tools-final.log` and `.burn/hermes-tools-doctor.log`.

```bash
env -i PATH=/usr/bin:/bin HERMES_HOME="$PWD/.burn/tools-home" HERMES_RUNTIME_DIR="$PWD/.burn/tools-runtime" TMPDIR="$PWD/.burn/tmp" .burn/hermes-tools-test-env/bin/python -m ruff check tools/terminal_tool_guards.py tools/web_tools.py plugins/web/ddgs/provider.py tests/tools/test_blocked_command_guidance.py tests/tools/test_web_backend_unavailable.py
git diff --check
```

Results: **All checks passed**, and no whitespace errors.

## Deliberately left out, open questions, and remaining work

- No live config changes, gateway restarts/stops, live venv installs, deployments,
  live database operations, or delegated agents. Work stayed in this worktree.
- No attempt to repair or probe the production ddgs installation. Ben can enable
  the declared extra with `hermes pm install --extra ddgs` in the intended Hermes
  installation, then check `hermes doctor`. That command is documented here, not run.
- No redesign of shell parsing, extract-backend rescue, or provider selection.
  The changes use the existing guard and readiness mechanisms.
- No code blockers or unresolved test failures remain. Linux validation does not
  claim execution of macOS-only doctor cases.
- Changes and this handoff are committed locally for review; no push, PR, merge,
  or publication was required or performed.
- TokenSave context lookup timed out after 300 seconds, and this worktree has no
  `.tokensave/tokensave.db`; source and history inspection supplied the evidence.
  Shared OpenViking memory was consulted, including the prior explicit ddgs choice.
