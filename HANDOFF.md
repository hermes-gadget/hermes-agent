# Runtime Batch Handoff

Branch: `fix/runtime-batch-20260923` (base `d4611ac837`, `fork/main`). The five requested fixes span seven focused fix/test commits, listed below. A separate test-fixture hardening commit makes the complete tools suite deterministic in this environment. No push, deployment, or service action was performed, and no other worktree was touched.

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

The canonical per-file runner completed the full `tests/tools/` sweep: 543 files, 8,294 passed, 0 failed, and 87 skipped in 432.8 seconds with 4 workers. It ran under `env -i` with a worktree-local Python wrapper, temporary `HERMES_HOME`, and optional test dependencies installed to `.d6-test-deps/` using `uv pip --target`. The shared Hermes venv was unchanged. Daytona, Modal, fal-client, and Parallel SDK tests ran with those dependencies available.

Three test-fixture gaps discovered by the full sweep were corrected: the real-profile browser test now mocks Chromium launch and its DevTools port; the Browserbase test's fake `agent` package includes `agent.secret_scope`; and the no-credentials web test also simulates the optional `ddgs` package being absent, matching its existing unavailable-package setup.

The #256 before/after reproduction used a non-executable `gw_docs.json` larger than the referenced-script scan budget, read by a Python heredoc after a `-f` check: the base guard blocked it and the current guard allows it. A shell-referenced script containing `systemctl restart hermes-gateway` remained blocked on both versions.

The browser documentation already says Hermes falls back to built-in browser tools when the Browser Use CLI is unavailable; no CLI installation or invocation was attempted. No external service/browser invocation was performed.
