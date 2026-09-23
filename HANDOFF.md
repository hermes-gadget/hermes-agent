# Runtime Batch Handoff

Branch: `fix/runtime-batch-20260923` (base `d4611ac837`, `fork/main`). The five requested fixes span seven focused fix/test commits; the D6b #348 continuation adds one focused approval-test commit. A separate test-fixture hardening commit makes the complete tools suite deterministic in this environment. No push, deployment, or service action was performed, and no other worktree was touched.

## Changes

- `a2b9871364` — #256: skip common data-file references in cron lifecycle script checks while still detecting executable script references.
- `a57b744bd9` — #327: read the private-URL browser setting from the existing config cache at runtime instead of process-lifetime caches.
- `57932ca4c1` — #317: report worker OOM only when systemd explicitly records `oom-kill`; a `-9` exit alone is not treated as proof.
- `8450b2cb1b` — #317 follow-up: retain failed worker-scope results until inspected, then reset the completed failure state so `--collect` cannot erase the evidence first.
- `5f57ed97f9` — #323: classify quota, rate-limit, and server-side vision errors as provider degradation in error results.
- `71e3a71c8a` — #348 initial fix: canonicalize the temp-path fixture to match the approval detector's lexical path contract; this did not address the remaining env-dependent classifier assertion.
- `bd951b0645` — #348 D6b: check the cleanup exemption directly for linked and canonical spellings, then verify the canonical target is auto-exempted.
- `a8fa97a683` — restore the approval config resolver after an interrupt test so its patch does not leak into later tests.

## Validation

Focused regressions passed after the final source change: 796 passed, 4 skipped across cron lifecycle, browser URL safety/SSRF/browser CLI/kanban, process registry, vision, and approval tests. The process-registry file alone passed 99 with 4 skipped.

The remaining #348 failure came from the test asserting that the linked spelling must match a dangerous-command pattern. That pattern matches `/tmp/...` paths but not the default scratch paths under `/home/...`; the cleanup-exemption helper itself correctly rejects the linked spelling and accepts the canonical target. The corrected test keeps those helper assertions as negative/positive controls, then asserts that the canonical target is auto-exempted without depending on the classifier's path pattern. The focused command below was run with the shell's `TMPDIR` unset (which resolves to `/tmp` here), with the coordinator's scratch `TMPDIR` selected, and with a forced `/tmp` basetemp:

`env PYTHONPATH= HERMES_HOME=/tmp/d6-home /home/ben/.hermes/hermes-agent/venv/bin/python -m pytest tests/tools/test_approval.py::TestDetectDangerousRm::test_symlinked_temp_dir_only_exempts_canonical_target -q`

| #348 focused check environment | Result |
| --- | --- |
| Shell default (`TMPDIR` unset; Python tempdir `/tmp`) | 1 passed in 1.32s |
| Coordinator scratch (`TMPDIR=/home/ben/.hermes/cache/scratch`) | 1 passed in 1.28s |
| Forced `--basetemp=/tmp/d6chk` | 1 passed in 1.21s |

The requested eight-file regression set (`test_approval.py`, `test_approval_interrupt.py`, `test_url_safety.py`, `test_vision_tools.py`, `test_process_registry.py`, `test_kanban_tools.py`, `test_browser_ssrf_local.py`, and `test_gateway_restart_loop.py`) passed with `PYTHONPATH=` and `HERMES_HOME=/tmp/d6-home`: 675 passed, 4 skipped in 45.15s with the shell default, and 675 passed, 4 skipped in 57.20s with `TMPDIR=/home/ben/.hermes/cache/scratch`.

The canonical per-file runner completed the full `tests/tools/` sweep: 543 files, 8,294 passed, 0 failed, and 87 skipped in 432.8 seconds with 4 workers. It ran under `env -i` with a worktree-local Python wrapper, temporary `HERMES_HOME`, and optional test dependencies installed to `.d6-test-deps/` using `uv pip --target`. The shared Hermes venv was unchanged. Daytona, Modal, fal-client, and Parallel SDK tests ran with those dependencies available.

Three test-fixture gaps discovered by the full sweep were corrected: the real-profile browser test now mocks Chromium launch and its DevTools port; the Browserbase test's fake `agent` package includes `agent.secret_scope`; and the no-credentials web test also simulates the optional `ddgs` package being absent, matching its existing unavailable-package setup.

The #256 before/after reproduction used a non-executable `gw_docs.json` larger than the referenced-script scan budget, read by a Python heredoc after a `-f` check: the base guard blocked it and the current guard allows it. A shell-referenced script containing `systemctl restart hermes-gateway` remained blocked on both versions.

The browser documentation already says Hermes falls back to built-in browser tools when the Browser Use CLI is unavailable; no CLI installation or invocation was attempted. No external service/browser invocation was performed.
