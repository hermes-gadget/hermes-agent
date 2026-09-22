# HANDOFF — Mission D4 (#353)

## Status

Done: implementation and targeted tests are complete. Deployment and live validation remain for Ben.

## Branch and commits

- Branch: `fix/ov-plugin-autostart`
- Implementation commit: `72ff8063cddee8a6422b4edb9b666f7d4abffed5` (`fix(openviking): honor systemd autostart ownership`)
- This handoff is committed immediately after the implementation commit on the same branch.

## Files changed

- `plugins/memory/openviking/_autostart.py` — autostart modes, systemd unit detection, and nonblocking unit delegation.
- `plugins/memory/openviking/__init__.py` — config schema and guarded startup wiring; preserves no-unit child startup and the unhealthy-listener guard.
- `plugins/memory/openviking/_setup.py` — uses the autostart sibling's startup state.
- `plugins/memory/openviking/README.md` — documents `auto`, `never`, and `spawn`.
- `tests/plugins/memory/test_openviking_provider.py` — unit ownership, config gate/profile scope, no-unit behavior, and unhealthy listener coverage.
- `HANDOFF.md` — this delivery and validation record.

## Tests

- Command: `scripts/run_tests.sh tests/plugins/memory/`
- Result: 31 files; 466 passed, 3 skipped, 0 failed.

## Push state

The implementation commit is pushed to `fork/fix/ov-plugin-autostart`. This handoff is the following commit and is pushed to the same branch before delivery. No upstream push or merge was made.

## Gated items for Ben

- Deploy the branch through the normal release path. Loading the plugin change requires a Hermes gateway restart; that restart is Ben's. No service was started, stopped, or restarted during this task.
- After deployment and Ben's gateway restart, use one ordinary memory-backed Hermes request while the existing `openviking.service` is active. Confirm `systemctl --user show openviking.service -p ActiveState -p MainPID` reports it active, and compare its `MainPID` with the listener shown by `ss -ltnp 'sport = :1933'`. Check the Hermes gateway log for the systemd-unit startup status and confirm there is no separate `openviking-server` child. Do not stop or restart OpenViking to run this validation.
- The persisted setting is `memory.openviking.autostart`; its default is `auto`. Set it to `never` to disable automatic startup. `spawn` only uses a child process when no OpenViking systemd user unit is detected.

## Open questions

None.

## Evidence

- Required task contract: `/home/ben/.review-notes/normal-queue-20260922/d4-ov-autostart/BRIEF.md`
- Targeted regression coverage: `tests/plugins/memory/test_openviking_provider.py`
- Final test command and result: `scripts/run_tests.sh tests/plugins/memory/` — 466 passed, 3 skipped, 0 failed.
