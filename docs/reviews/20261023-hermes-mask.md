# Hermes signature masking and MCP startup handoff — 2026-10-23

Branch: `burn/hermes-mask-20261023`

Baseline: `e7cb5bfab461a5842970d084bf1102f410d73082` (the worktree's `origin/main`)

## Outcome

Both requested bugs were reproduced against the baseline and addressed locally. Complete
`[sig1:<base64>]` envelopes survive generic tool-output masking, while identical values outside
the envelope remain masked. CLI startup now either observes a usable MCP registration or reports
an unavailable enabled/selected server to the session, including in quiet `-z` mode.

Implementation commits:

- `4ec9b6e5fe` — preserve opaque signature spans and add redaction contracts.
- `66256f044b` — surface MCP startup diagnostics, wire oneshot stderr, and document the behavior.

## #644: signature bytes corrupted by view masking

The concrete corruption site was `agent/redact.py`'s `_JWT_RE` pass. Its intentional one-part
JWT support matches any sufficiently long `eyJ…` base64 JSON string, including a signature
envelope's payload, then replaces it with a head/tail display mask. The original JWT change
(`ee9c0a3ed0`) was checked: removing ordinary JWT masking would regress its intended protection.

`redact_sensitive_text` now partitions complete `sig1` spans from surrounding text and applies
the existing generic passes to the surrounding spans. The token syntax accepts standard and
URL-safe base64 alphabets, optional padding, and adjacent tokens. Incomplete wrappers,
whitespace in the payload, and invalid payload characters do not receive the exemption.
The existing exact registered-vault-secret scrub still runs before generic pattern handling.

`tests/agent/test_redact_signature_tokens.py` contains two parameterized behavior contracts:

- Valid base64 containing a literal maskable pubkey substring, plus encoded JSON containing a
  `pubkey` field, remains byte-identical inside the token across text, terminal output, file
  output, and log formatting. Identical pubkey/base64 bytes outside are still masked; repeated
  masking is stable. Standard/URL-safe and padded/unpadded forms are covered.
- Bare payloads and malformed signature wrappers retain ordinary masking.

On the baseline, the initial reproduction produced **4 failed, 4 passed**: all four output
surfaces corrupted the valid encoded JSON token. With the fix and expanded encoding coverage,
the file passes **12 tests**. No masking regex was weakened for ordinary non-token text.

## #607: MCP discovery deadline silently loses the server

`hermes_cli/mcp_startup.py:272` joins background discovery for the configured bound and then
returns, whether or not discovery finished. Before the fix, the caller immediately built the
agent's tool snapshot without checking or reporting the unfinished server. Interactive startup
defaults to 1.5 seconds; single-query startup defaults to 15 seconds. These bounds already existed
in the earlier non-interactive discovery fix (`3572d4bca1`).

The bridge already records connection failures and exposes them through `get_mcp_status`.
The missing behavior was in the CLI readiness caller: an expired wait or completed failure was
silent in the session. Ordinary CLI logging is file-only, and `run_oneshot` additionally disables
logging and redirects stdout/stderr to `/dev/null` while building/running the agent.

`ensure_mcp_discovery_before_agent_build` now checks the current profile's status after the same
bounded wait. Each unavailable enabled/selected server produces a redacted diagnostic containing
its name, the recorded error or expired-wait reason, `hermes mcp test <name>` guidance, and (for a
pending cold start) the relevant timeout setting. Healthy, lazy, disabled, and explicitly
excluded servers are not reported as failures. The default destination is session stderr;
oneshot passes its saved real stderr through the existing call chain so the diagnostic escapes
its output suppression and stdout remains the final response.

This uses the brief's **clear actionable error** option. Discovery/retry policy, connection
deadlines, and prompt/tool caching policy are unchanged; the fix adds no mid-conversation tool
or system-prompt mutation. No bridge implementation changes were needed for the reproduced race.

`tests/hermes_cli/test_mcp_startup_diagnostics.py` exercises real config loading, background
discovery, the MCP SDK, stdio subprocess transport, registration, and a tool call against a
temporary `HERMES_HOME`:

- A local protocol peer named `gmail` waits on a file gate during `tools/list`. A 0.05-second
  startup wait returns within a 2-second ceiling and exposes a named timeout diagnostic. After
  release, the real tool registers and is successfully called; the next readiness check is quiet.
  Disabled and excluded fixture servers never generate diagnostics.
- A nonexistent server executable generates a real connection failure. Its filename/reason and
  recovery command reach session stderr through both the helper and `run_oneshot`. The oneshot
  test substitutes only model/runtime handling to avoid external inference; MCP discovery is real.

All **3 cases failed on the baseline** because stderr was empty. All pass with the fix.

## Verification commands and receipts

All pytest runs used `scripts/run_tests.sh`, with this explicit local interpreter and isolated
development paths; the runner additionally scrubs credentials and isolates test homes:

```bash
HERMES_PYTHON="$PWD/.venv/bin/python" \
HERMES_HOME="$PWD/.build/mask-home" \
HERMES_RUNTIME_DIR="$PWD/.build/mask-runtime" \
scripts/run_tests.sh \
  tests/agent/test_redact.py \
  tests/agent/test_redact_signature_tokens.py \
  tests/agent/test_redaction_registry.py \
  tests/tools/test_browser_vault.py \
  tests/tools/test_terminal_tool_exception_redaction.py \
  tests/hermes_cli/test_mcp_discovery_timing.py \
  tests/hermes_cli/test_mcp_startup_diagnostics.py \
  tests/hermes_cli/test_mcp_startup.py \
  tests/hermes_cli/test_oneshot_exit_contract.py \
  tests/hermes_cli/test_oneshot_resume.py \
  tests/hermes_cli/test_oneshot_fallback.py \
  tests/hermes_cli/test_oneshot_usage_file.py \
  tests/tools/test_mcp_startup_summary_names_failures.py \
  --file-retries 0 -j 4 -q
```

Result: **13 files, 349 tests passed, 0 failed**, exit 0; 45.9 seconds in the runner. Retries were
disabled. The local receipt is `.build/hermes-mask-targeted-tests.log` (ignored, not committed).

The initial baseline check of `tests/agent/test_redact.py` and
`tests/hermes_cli/test_mcp_discovery_timing.py` passed **222 tests**. Regression-only red runs used
the same environment prefix and `--file-retries 0 -j 1 -q` with, respectively,
`tests/agent/test_redact_signature_tokens.py` and
`tests/hermes_cli/test_mcp_startup_diagnostics.py`. Their ignored receipts are
`.build/mask-red-tests.log` and `.build/mcp-red-tests.log`.

```bash
.venv/bin/python -m ruff check agent/redact.py hermes_cli/mcp_startup.py \
  hermes_cli/oneshot.py tests/agent/test_redact_signature_tokens.py \
  tests/hermes_cli/test_mcp_startup_diagnostics.py
git diff --check
```

Results: Ruff **All checks passed**, exit 0; diff whitespace check exit 0. This is a Python-only
runtime change plus Markdown documentation; no dependency or frontend build changed.

## Deliberately left out, open questions, and remaining work

- No live config access/edit, gateway restart/stop, deployment, live database migration, secrets
  access, external Gmail request, delegated agent, or changes in another worktree. No branch push,
  PR, or publication of this report was performed.
- Signature recognition is an explicit `sig1` boundary rule, not a blanket exemption for arbitrary
  base64 or a cryptographic signature validator. Ordinary JWT/key masking remains in place.
- Tests prove the CLI timeout/failure behavior with a real local MCP peer named `gmail`; they do
  not prove the deployed Gmail server's availability or exclude an additional live environment or
  authentication problem. Those would require separately authorized operator investigation.
- No required local implementation work remains. Ben owns review/merge and any later production
  activation. Shared OpenViking memory was updated with the two reusable root-cause facts.
