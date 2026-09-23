# Runtime Batch Handoff

Branch: `fix/runtime-batch-20260923` (base `d4611ac837`, `fork/main`). The five requested fixes span seven focused fix/test commits, listed below; this handoff is recorded separately. No push, deployment, or service action was performed, and no other worktree was touched.

## Changes

- `a2b9871364` — #256: skip common data-file references in cron lifecycle script checks while still detecting executable script references.
- `a57b744bd9` — #327: read the private-URL browser setting from the existing config cache at runtime instead of process-lifetime caches.
- `57932ca4c1` — #317: report worker OOM only when systemd explicitly records `oom-kill`; a `-9` exit alone is not treated as proof.
- `8450b2cb1b` — #317 follow-up: retain failed worker-scope results until inspected, then reset the completed failure state so `--collect` cannot erase the evidence first.
- `5f57ed97f9` — #323: classify quota, rate-limit, and server-side vision errors as provider degradation in error results.
- `71e3a71c8a` — #348: canonicalize the temp-path test fixture to match the approval detector's intended lexical path contract.
- `a8fa97a683` — restore the approval config resolver after an interrupt test so its patch does not leak into later tests.

## Validation

Focused regressions passed after the final source change: 796 passed, 4 skipped across cron lifecycle, browser URL safety/SSRF/browser CLI/kanban, process registry, vision, and approval tests. The process-registry file alone passed 99 with 4 skipped.

The #256 before/after reproduction used a non-executable `gw_docs.json` larger than the referenced-script scan budget, read by a Python heredoc after a `-f` check: the base guard blocked it and the current guard allows it. A shell-referenced script containing `systemctl restart hermes-gateway` remained blocked on both versions.

A segmented run of the complete `tests/tools` file set reported 8,232 passed, 86 skipped, and 61 failed out of 8,379 collected tests. This sweep predates the final #317 result-retention follow-up, and the three approval/command-guard cases it failed were rerun after the approval fixture fix and passed. The remaining failures were concentrated in unavailable optional runtimes/SDKs and order/environment-sensitive tests:

- Missing optional dependencies: 15 Daytona tests; 2 Modal tests; 31 fal-client image/video tests; and 2 parallel-web tests. Lazy installs are disabled in this environment.
- Missing browser runtime: 1 Chrome real-profile test. One Lightpanda cleanup test also hit the test harness guard when cleanup attempted to terminate a host PID outside its allowed process subtree.
- Environment/import mismatch: 1 Browserbase test resolved `agent.secret_scope` from the shared installed runtime, whose `utils` lacked `file_signature`; 1 SearxNG “no credentials” test saw credentials from the loaded environment.
- Batch-order-sensitive failures: 3 approval/command-guard tests, 3 skill-bundle provenance tests, and 1 STT idle-unload test passed when rerun as a focused selection.

The browser documentation already says Hermes falls back to built-in browser tools when the Browser Use CLI is unavailable; no CLI installation or invocation was attempted. No lazy dependency installation or external service/browser invocation was performed. The current focused regression set passes; the broad tools suite still has the environment/test-isolation failures listed above.
