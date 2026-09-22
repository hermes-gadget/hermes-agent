# D3 handoff

**Status: partial — branch contents are ready; fork push is pending.**

## Branch and commits

- Worktree branch: `preserve/local-main-20260922`.
- Preserved source commits, unchanged and in order:
  - `9e736f50eb63195a02a447c39b5160ba247fc9c0` — approvals deny-glob fix.
  - `1ba01e6e45dc3fbb019ef75db8c8404d12b68147` — OpenViking non-primary write suppression.
- Additional commits on this branch:
  - `7eae09c48dccaa861879cbbb9a3687bcb2cc119f` — isolate the OpenViking warning test from the host's real port state.
  - `0ac41b28add3a5813eace567dc37497789046f5b` — `DECISION.md` analysis and Ben's options.
  - This handoff is committed separately after review.

## Files changed

- `tests/plugins/memory/test_openviking_provider.py` — one-line stub for the external localhost port probe in the unhealthy-server warning test.
- `DECISION.md` — verified updater behavior, effective config, exact supported options and commands, and source line references.
- `HANDOFF.md` — this operational record.

## Tests

- `scripts/run_tests.sh tests/tools/test_approval_deny_rules.py` — passed, 38 tests.
- `scripts/run_tests.sh tests/plugins/memory/test_openviking_provider.py` — first run: 67 passed, 1 failed because a real localhost listener caused the optional listener-description suffix to be added to the warning. After commit `7eae09c`, rerun passed, 68 tests.
- No full suite was run.

## Push state

Pending. The destination branch was absent on `fork` at inspection. After committing this handoff, push only to the fork:

```bash
git push -u fork preserve/local-main-20260922
```

Verify with `git ls-remote fork refs/heads/preserve/local-main-20260922` and compare the reported SHA with `git rev-parse HEAD`. Do not push to `origin`.

## Ben's gated items

- Review `DECISION.md`, choose Option A or B, and inspect the live checkout's untracked entry before deciding how to preserve it.
- No config changes were applied. The current `updates.pre_update_backup: false` disables the pre-update snapshot; either option documents the command to set it to `quick`.
- No `hermes update` was run. A normal successful update may restart and verify the gateway fleet; Ben owns scheduling that update and any service restart/deploy action.
- No open code questions remain. The decision is whether Ben wants automatic updates merged into a maintained branch (Option A, recommended) or wants to reapply only missing patches after each update (Option B).

## Evidence

- `DECISION.md` — current checkout/config observations, remote comparison, behavior analysis, and updater source references.
- `tests/tools/test_approval_deny_rules.py` — targeted test for the approvals commit.
- `tests/plugins/memory/test_openviking_provider.py` — targeted tests for the OpenViking commit and the environment-isolation correction.
- Remote main comparison observed: `8a92051f20e6b371c4ff1a46a5bcec7138cc4e8c...28aceb3451f5a5d2a27396b2adf29231be34c482`; the latter diverges from `1ba01e6e45dc3fbb019ef75db8c8404d12b68147` with two local-only commits.
