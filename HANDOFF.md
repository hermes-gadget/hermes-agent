# Runtime Batch Handoff

Branch: `fix/runtime-batch-20260923` (base `d4611ac837`, `fork/main`). The six fix/test commits are listed below; this handoff is recorded in a separate docs commit. No push, deployment, or service action was performed, and no other worktree was touched.

## Changes

- `a2b9871364` — #256: skip common data-file references in cron lifecycle script checks while still detecting executable script references.
- `a57b744bd9` — #327: read the private-URL browser setting from the existing config cache at runtime instead of process-lifetime caches.
- `57932ca4c1` — #317: report worker OOM only when systemd explicitly records `oom-kill`; a `-9` exit alone is not treated as proof.
- `5f57ed97f9` — #323: classify quota, rate-limit, and server-side vision errors as provider degradation in error results.
- `71e3a71c8a` — #348: canonicalize the temp-path test fixture to match the approval detector's intended lexical path contract.
- `a8fa97a683` — restore the approval config resolver after an interrupt test so its patch does not leak into later tests.

## Validation

Focused regressions passed: cron lifecycle tests (300); browser URL safety/SSRF/browser CLI/kanban (229); process registry (98 passed, 4 skipped); vision tools (41); approval canonical-temp checks (5), approval suites (127), and the three broader approval/command-guard cases that had failed in the batched suite (3).

The complete `tests/tools` file set was run in bounded batches after the one-shot runner was terminated by its execution limit. Of 8,379 collected tests, 8,232 passed, 86 skipped, and 61 failed. The failures were concentrated in unavailable optional runtimes/SDKs and order/environment-sensitive tests:

- Missing optional dependencies: 15 Daytona tests; 2 Modal tests; 31 fal-client image/video tests; and 2 parallel-web tests. Lazy installs are disabled in this environment.
- Missing browser runtime: 1 Chrome real-profile test. One Lightpanda cleanup test also hit the test harness guard when cleanup attempted to terminate a host PID outside its allowed process subtree.
- Environment/import mismatch: 1 Browserbase test resolved `agent.secret_scope` from the shared installed runtime, whose `utils` lacked `file_signature`; 1 SearxNG “no credentials” test saw credentials from the loaded environment.
- Batch-order-sensitive failures: 3 approval/command-guard tests, 3 skill-bundle provenance tests, and 1 STT idle-unload test passed when rerun as a focused selection.

No lazy dependency installation or external service/browser invocation was performed. The targeted regressions for all five fixes passed.
