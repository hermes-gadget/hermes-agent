# HANDOFF — D7 / #228 cron prompt scope

**Status:** Done; implementation, targeted tests, documentation, and this handoff are committed and published.

**Branch:** `fix/cron-scope-leak`

**Commits:**
- Starting branch HEAD: `1ba01e6e45dc3fbb019ef75db8c8404d12b68147` (pre-existing; retained).
- Implementation, tests, and docs: `e3ff340d0dfabcb4184149613db54ff6b28aebf3` (`fix(cron): isolate scheduled prompts from profile context`).
- This handoff is committed separately after the implementation commit. The final branch tip is reported in the task completion receipt.

## DESIGN

Cron sessions are identified by the already session-scoped `platform="cron"` value. Prompt assembly uses that value at agent initialization to select the default Hermes identity and omit profile-wide `SOUL.md`, built-in `MEMORY.md`/`USER.md`, external memory-provider prompt blocks, and memory-policy guidance. Cron turn setup returns before `MemoryManager.on_turn_start` and `prefetch_all`; providers such as Mem0 can begin asynchronous recall from the callback itself, so gating only the later prefetch would be too late.

Cron keeps its run-specific job/system prompt, default Hermes identity, attached skills and tools, and configured workdir/project context. `skip_memory=False` remains unchanged: the built-in memory store and configured memory tool remain available under the existing #91447 contract. External providers still receive cron context and must follow the existing no-write rule for non-primary contexts. Interactive CLI/gateway prompt assembly is unchanged. All scope decisions happen before the session prompt is built; no mid-conversation prompt mutation is introduced.

Alternatives considered:
- Keeping profile instructions and appending a job boundary would leave the standing instructions in the higher-priority system prompt, so the offending directives could still be followed.
- Parsing only selected sections from `SOUL.md` is unsafe because it is free-form and has no reliable delimiter separating persona from standing policy (including Full Auto Mode directives).
- Setting `skip_memory=True` or denylisting the memory tool would also change the established #91447 store/tool-availability contract. The fix suppresses automatic prompt loading and recall while preserving explicit configured access.

## Files changed

- Runtime: `agent/agent_init.py`, `agent/system_prompt.py`, `agent/turn_context.py`, `cron/scheduler.py`.
- Area guidance: `agent/AGENTS.md`, `cron/AGENTS.md`.
- Tests: `tests/agent/test_skip_memory_store.py`, `tests/agent/test_system_prompt.py`, `tests/agent/test_turn_context.py`, `tests/cron/test_cron_memory_contract.py`, `tests/cron/test_cron_workdir.py`, `tests/cron/test_scheduler.py`.
- Skill reference: `skills/autonomous-ai-agents/hermes-agent/references/background-systems.md`.
- English docs: `website/docs/developer-guide/cron-internals.md`, `website/docs/developer-guide/prompt-assembly.md`, `website/docs/guides/automate-with-cron.md`, `website/docs/guides/daily-briefing-bot.md`, `website/docs/guides/use-soul-with-hermes.md`, `website/docs/reference/tools-reference.md`, `website/docs/user-guide/configuration.md`, `website/docs/user-guide/features/context-files.md`, `website/docs/user-guide/features/cron.md`, `website/docs/user-guide/features/memory.md`, `website/docs/user-guide/features/spotify.md`, `website/docs/user-guide/which-file-does-what.md`.
- Chinese docs: `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/guides/daily-briefing-bot.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/tools-reference.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/configuration.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/context-files.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/cron.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/memory.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/spotify.md`, `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent.md`.

## Verification

Command:

```text
scripts/run_tests.sh tests/agent/test_system_prompt.py tests/agent/test_turn_context.py tests/cron/test_cron_memory_contract.py tests/cron/test_cron_workdir.py
```

Result: **4 files, 120 tests passed, 0 failed**. `git diff --check` also passed before the implementation commit.

The tests cover cron prompt exclusion and retained job/project context, normal CLI prompt retention, real temporary-home A→B→A SOUL/memory isolation, skipping external provider callbacks/prefetch for cron, and unchanged memory store/tool availability.

## Push and Ben-gated items

**Push state:** The final branch, including this handoff, is published to `fork/fix/cron-scope-leak`; its remote ref was checked against the local branch tip after the push. The fork branch did not exist before this work. `origin` / NousResearch was not written.

No service was started, stopped, or restarted. Applying the change to a live deployment is Ben-gated; the running gateway must be restarted to load the updated code. No configuration change is required.

## Open questions and evidence

No open design questions. Explicit memory-tool use remains available when configured; profile-wide memory preload and automatic external recall are suppressed for cron.

Evidence: `/home/ben/.review-notes/normal-queue-20260922/d7-cron-scope/BRIEF.md`; implementation at `agent/system_prompt.py`, `agent/turn_context.py`, and `cron/scheduler.py`; tests listed above and their recorded test-run summary.
