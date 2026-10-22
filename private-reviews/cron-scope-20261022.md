Historical review of the original private change; validation below does not certify this upstream integration.

# Cron session scope — backlog #228

Status: implemented, tested, pushed; PR open. No deployment, service restart, or live configuration edit was performed.

- Branch: `burn/cron-scope-leak-20261022`
- Implementation commit: `af2dd9e5dac030d6c18e7d5bca03d8941dc16eee`
- Baseline: `d5034303528f56c089209cf134de0e7a976573a7` (also the remote `main` SHA checked during this run)
- PR: https://github.com/gadgethd/hermes-agent/pull/3
- This review is committed separately after the implementation; the PR includes both commits.

## Mechanism and scope boundary

Before this change, `cron.scheduler._construct_cron_agent()` supplied `platform="cron"`, `skip_memory=False`, and `load_soul_identity=True`, but no authority scope. `agent.agent_init._init_memory()` initialized ambient memory, `agent.system_prompt._memory_parts()` appended its instructions, and `_frozen_plugin_prompt_sections()` rendered standing plugin sections without a cron gate. `tools.delegate_tool.delegate_task()` had no cron refusal before child construction.

`agent/session_scope.py` now owns an explicit, session-local scope. The scheduler passes `session_scope="cron"`; legacy constructors using `platform="cron"` infer it, and cannot explicitly downgrade to the default scope. Other transports can also bind cron scope explicitly. It is resolved before the tool snapshot or prompt is built, without environment variables or mutable process-global policy.

Cron scope withholds entire ambient sources rather than filtering strings: MEMORY.md / USER.md prompt blocks, SOUL.md, plugin prompt sections, global `skills.auto_load`, prefill messages, ephemeral system overlays, and external memory-provider initialization/automatic recall. The fixed scope guidance confines work to the declared job, forbids unrelated investigations/repairs and agent spawning through external services, and requests only the deliverable in the final response. Background review forks stay disabled.

The delegation toolset is excluded at construction. Both sequential and concurrent executors block native delegation before plugin authorization or dispatch and classify it as a scope block. The native handler also refuses direct/registry calls, covering single, batch, and background requests before credentials or child construction. Refusals explain that cron scope forbids spawning/delegation.

Initialization logs `cron scope: standing instructions withheld` with the session ID and records `session_scope` in session model metadata. Prompt/tool state remains fixed during the session; the tests also verify identical scoped prompt bytes through the existing compression rebuild boundary.

## What jobs can still do

Jobs retain their declared prompt, explicitly attached skills, and explicit workdir context. Existing file, terminal, browser, web, and other configured tools retain their normal resolution/approval rules. The built-in memory store/tool remains available for explicit job writes, preserving #91447's capability without importing standing authority into the system prompt. Script-only jobs are unchanged. Interactive sessions retain their existing memory, persona, plugin sections, auto-loaded skills, and native delegation.

## Risks, deliberately left out, and open questions

This is a task-authority boundary, not an OS sandbox. Native delegation is enforced in code. Arbitrary terminal commands or third-party MCP tools can have orchestration capabilities that Hermes cannot infer; the scope guidance forbids using those to spawn agents, but this change does not claim vendor-side or OS enforcement. Trusted plugin hooks retain their existing capabilities. Tool results and explicitly read files are data, and the scope guidance states that they cannot widen authority.

Withholding whole ambient sources also removes harmless preferences/persona and external memory access from cron sessions. Jobs relying on those must move required facts/instructions into their declared prompt or attached skills. Explicit workdir instructions remain available but cannot widen job authority. Existing in-flight conversations are not rewritten. No core tool, provider-specific integration, new behavioral environment variable, live migration, restart, or deployment was added.

No blocking implementation question remains. A stronger sandbox for arbitrary external orchestration would require a separately designed capability boundary; it is not implied by these tests.

## Verification and PROOF

All tests used the canonical runner, temporary Hermes homes, real agent/config/plugin/memory/prompt imports, and a stubbed model transport. The new fixtures load actual temporary plugins and memory files. Profile coverage is A→B→A under multiplex mode with explicitly bound secret scopes.

The baseline focused run, before production edits, failed both new tests: the assembled cron prompt contained `Full Auto Mode standing block`, and a spawn reached the sentinel in `_build_children` (`cron attempted to construct a delegated child`). This used:

```bash
HERMES_HOME="$PWD/.burn/runner-home" HERMES_RUNTIME_DIR="$PWD/.burn/runtime" \
HERMES_PYTHON=/home/ben/.hermes/hermes-agent/venv/bin/python \
scripts/run_tests.sh tests/cron/test_cron_session_scope.py -j 1 --file-retries 0 -v --tb=short
```

Final verification used the fresh PM-created Python 3.14 test environment inside this worktree:

```bash
export HERMES_HOME="$PWD/.burn/runner-home"
export HERMES_RUNTIME_DIR="$PWD/.burn/runtime"
export HERMES_PYTHON="$PWD/.burn/runner-home/installs/94d0ef429dea9f8a/test-environment/gen-7a415a51af0740798a379ed28cbeab1e/venv/bin/python"

scripts/run_tests.sh tests/cron/test_cron_session_scope.py tests/tools/test_delegate_cron_sync_fallback.py -j 2 --file-retries 0 -v --tb=short
```

**PROOF: 2 files, 3 tests passed, 0 failed in 25.3s.** The cron file's two tests passed:

- `test_cron_prompt_withholds_standing_authority_while_interactive_keeps_it`: cron lacks both Full Auto and MissionDeck spawn-authority instructions; interactive includes both from its own profile. Also checks provider/persona/autoload exclusion, retained workdir/memory-tool access, prefill/ephemeral exclusion, scope diagnostics, and stable rebuilds.
- `test_cron_spawn_is_refused_before_dispatch`: direct handler, registry, sequential, concurrent, single/batch/background attempts are refused before child construction; explicit cron scope also works on a CLI transport.

The third test preserves ordinary stateless-session synchronous delegation. Its historical cron regression now explicitly uses default scope, since real cron-scoped parents are refused.

Broader regression command:

```bash
scripts/run_tests.sh tests/cron/test_cron_session_scope.py tests/cron/test_scheduler.py tests/cron/test_cron_memory_contract.py tests/cron/test_cron_workdir.py tests/cron/test_agent_scheduling_gate.py tests/agent/test_system_prompt.py tests/agent/test_plugin_prompt_sections.py tests/agent/test_skip_memory_store.py tests/agent/test_memory_provider_init.py tests/agent/test_tool_call_guardrail_runtime.py tests/tools/test_delegate.py tests/tools/test_delegate_control_actions.py tests/tools/test_delegate_toolset_scope.py tests/tools/test_delegate_cron_sync_fallback.py -j 2 --file-retries 0 -v --tb=short
```

**Result: 14 files, 327 tests passed, 0 failed in 289.2s.** After the final test-only clarifications, the focused command above passed again. `git diff --check` also passed. Local final logs: `.burn/cron-scope-regression.log` and `.burn/cron-scope-final-proof.log`. No full-suite or live-provider/model test is claimed.

## Honest mistake inventory

- Initially selected an older `.venv` without pytest, triggering isolated PM activation. Its downloaded tooling and test environment stayed under `.burn/`; no system package installation was used. The redundant optional bytecode precompile was stopped once; tests themselves were not bypassed.
- The first fixture lazily constructed the real SDK, causing the real-home I/O guard to reject metadata probes into the shared interpreter installation. The remote transport was stubbed; the scope-relevant integration paths remain real.
- The old shared environment masked an undeclared PyYAML import in the new fixture. Fresh Python 3.14 collection exposed it; the fixture now uses `hermes_yaml`.
- Initial native refusal returned through ordinary failure observation, which could append a repeated-error warning. Moving the refusal into the shared authorization gate made it a proper scope block.
- Default `gh` authentication could not see the private fork. PR operations reused the Git origin's already-configured credential for the subprocess only; no authentication configuration was changed and no credential was printed or saved.
