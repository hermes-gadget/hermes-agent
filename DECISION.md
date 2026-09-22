# D3: preserve the two local commits across `hermes update`

**Observed 2026-09-22.** This is an analysis and recommendation only. No update was run, no config was changed, and the live checkout was not switched.

## Current state

- The live checkout `/home/ben/.hermes/hermes-agent` is on `main` at `1ba01e6e45dc3fbb019ef75db8c8404d12b68147`; its parent is `9e736f50eb63195a02a447c39b5160ba247fc9c0`. Its local `origin/main` tracking ref is `8a92051f20e6b371c4ff1a46a5bcec7138cc4e8c`.
- Tracked files in the live checkout are clean. `git status` reports one untracked entry; I did not inspect or touch it. A parked custom branch must be clean for the updater's parked-branch guard to proceed.
- The local `origin/main` ref is stale. Read-only `git ls-remote origin refs/heads/main` returned `28aceb3451f5a5d2a27396b2adf29231be34c482`. GitHub's compare endpoint reports that tip is 1,883 commits ahead of the local `8a92051` tracking ref. Comparing current upstream with local HEAD `1ba01e6` returns `diverged`, `behind_by=2`, and merge base `8a92051`; comparing it with `9e736f5` returns `diverged`, `behind_by=1`. Thus neither local commit is on current upstream `main`. Recheck the remote tip before Ben runs an update, since it can advance again.
- `/home/ben/.hermes/config.yaml` sets `updates.pre_update_backup: false` and `updates.non_interactive_local_changes: stash`. It does not set `updates.auto_switch_parked_branch` or `updates.parked_branch_strategy`; there is no managed config file overriding them. The effective defaults are `true` and `switch` respectively.

## What the next update does with this state

The default target is `main`. The updater fetches `origin/main`; because the live checkout is also on `main`, the parked-branch strategy is bypassed. Its one untracked status entry is included in the updater's `git stash push --include-untracked`. A non-interactive run with the current `stash` setting restores it on success; an interactive CLI asks before restoring. `--keep-stash` leaves it parked, and a failed restore leaves it in the stash.

After fetching the observed newer `origin/main`, `HEAD..origin/main` is non-empty. The updater tries `git merge --ff-only origin/main`. The local-only commits make the histories diverge, so fast-forward fails. `_reconcile_diverged_checkout` sees that the checked-out branch name equals the target branch and executes `git reset --hard origin/main`. For a shared-ancestor divergence like this, it does not create the orphan-history rescue ref. `main` therefore moves off both local commits; any other local refs that happen to point at them are incidental protection, not a guarantee from this update path.

The current `updates.pre_update_backup: false` resolves to backup mode `off`, so this run would not create the normal pre-update snapshot. After a successful code swap, the normal update tail can restart and verify the gateway fleet. This worktree did not run that flow.

## Config and flag controls

| Setting | Current effective value | Effect |
|---|---:|---|
| `updates.auto_switch_parked_branch` | `true` (default) | For a different, parked branch, permits automatic handling only when the tree is clean and branch state is verifiable. `false` makes the code update skip on a parked branch. |
| `updates.parked_branch_strategy` | `switch` (default) | For a clean parked branch with commits absent from the update target, `switch` updates `main` and leaves the commits on the parked branch. `update_in_place` merges `origin/main` into the parked branch and leaves that branch checked out. |
| `updates.non_interactive_local_changes` | `stash` | Applies to dirty trees in non-interactive/gateway or `--yes` runs. `discard` drops the saved local changes after the pull; interactive runs still stash and ask. It does not protect committed changes. |
| `updates.pre_update_backup` | `false` | Disables the pre-update snapshot. `quick` enables the snapshot; `full` adds the full backup. This is file-state recovery, not a Git commit-preservation mechanism. |
| `hermes update --switch-branch` | not set | One-run override for `update_in_place`: switch to the target branch instead. It has no effect with the default `switch` strategy. |
| `hermes update --branch NAME` | not set | Changes the target branch; without it the target is `main`. |

## Option A — park on a maintained branch and update it in place (recommended)

This uses the updater's supported custom-branch path. It preserves the two commits on the active branch and merges upstream into them. A merge conflict stops the code update and leaves the local commits intact. Successful updates add a merge commit to this maintained branch.

First review the untracked entry. The commands below assume Ben elects to preserve it in a Git stash so the parked-branch guard sees a clean tree; do not run the stash command until its scope is understood.

```bash
git -C /home/ben/.hermes/hermes-agent status --short --branch --untracked-files=all
git -C /home/ben/.hermes/hermes-agent stash push --include-untracked -m "d3-untracked-before-update-20260922"
git -C /home/ben/.hermes/hermes-agent status --short --branch
git -C /home/ben/.hermes/hermes-agent switch -c preserve/live-main-local-20260922

hermes config set updates.auto_switch_parked_branch true
hermes config set updates.parked_branch_strategy update_in_place
hermes config set updates.pre_update_backup quick
hermes config get updates.auto_switch_parked_branch
hermes config get updates.parked_branch_strategy
hermes config get updates.pre_update_backup

cd /home/ben/.hermes/hermes-agent
hermes update
```

After the update, find the stash selector carrying `d3-untracked-before-update-20260922`, apply it, verify the file is restored, and only then drop it:

```bash
git -C /home/ben/.hermes/hermes-agent stash list --format='%gd %s'
git -C /home/ben/.hermes/hermes-agent stash apply 'stash@{N}'
git -C /home/ben/.hermes/hermes-agent status --short --branch
git -C /home/ben/.hermes/hermes-agent stash drop 'stash@{N}'
```

Replace `N` with the selector printed next to the unique stash message. Keep the checkout on `preserve/live-main-local-20260922`; switching back to `main` would put the live checkout back on the stale branch.

## Option B — let the updater switch to `main`, then reapply only missing patches

This keeps the preserved branch untouched and lets the updater fast-forward/reset `main`. It requires repeating the detection and cherry-pick after every update. The same untracked-entry review and clean-tree preparation from Option A are required.

```bash
git -C /home/ben/.hermes/hermes-agent status --short --branch --untracked-files=all
git -C /home/ben/.hermes/hermes-agent stash push --include-untracked -m "d3-untracked-before-update-20260922"
git -C /home/ben/.hermes/hermes-agent status --short --branch
git -C /home/ben/.hermes/hermes-agent switch -c preserve/live-main-local-20260922

hermes config set updates.auto_switch_parked_branch true
hermes config set updates.parked_branch_strategy switch
hermes config set updates.pre_update_backup quick
hermes config get updates.auto_switch_parked_branch
hermes config get updates.parked_branch_strategy
hermes config get updates.pre_update_backup

cd /home/ben/.hermes/hermes-agent
hermes update
git -C /home/ben/.hermes/hermes-agent switch main
git -C /home/ben/.hermes/hermes-agent cherry -v main preserve/live-main-local-20260922
```

`git cherry -v` marks patch-equivalent commits with `-` and missing patches with `+`. Cherry-pick only the SHAs from the `+` rows, oldest first:

```bash
git -C /home/ben/.hermes/hermes-agent cherry-pick <oldest-plus-sha> <next-plus-sha>
```

If there are no `+` rows, do not cherry-pick. On a conflict, resolve and run `git cherry-pick --continue`, or return to the post-update `main` with `git cherry-pick --abort`. Then restore the named stash:

```bash
git -C /home/ben/.hermes/hermes-agent stash list --format='%gd %s'
git -C /home/ben/.hermes/hermes-agent stash apply 'stash@{N}'
git -C /home/ben/.hermes/hermes-agent status --short --branch
git -C /home/ben/.hermes/hermes-agent stash drop 'stash@{N}'
```

Replace `N` with the selector printed next to the unique stash message, and drop it only after verifying the file is restored. A later update will reset the cherry-picked copies from `main`, so repeat the `git cherry` check each time.

## Other option

Keeping the pushed fork branch as an archive is already part of this D3 work, but by itself it does not change what `hermes update` does to the active `main` checkout. The supported automatic preservation path is Option A; no better in-place strategy appeared in the updater code.

## Source references

- Default target branch: `hermes_cli/main_install_repair.py:1275-1277`.
- Fetch and pull sequence: `hermes_cli/update_cmd.py:1632-1673`, `856-891`.
- Same-branch reset versus custom-branch merge: `hermes_cli/update_cmd.py:775-824`.
- Parked-branch guard and strategy: `hermes_cli/update_cmd.py:907-949`, `951-1020`; clean/unmerged checks: `hermes_cli/update_cmd_git.py:100-128`.
- Stash includes untracked files: `hermes_cli/update_cmd_stash.py:59-93`.
- Effective config loader and defaults: `hermes_cli/config.py:2092-2096`, `2292-2337`; `hermes_cli/config_defaults.py:2262-2292`.
- Backup `false` means off: `hermes_cli/update_cmd_maint.py:631-654`.
- `--switch-branch` behavior: `hermes_cli/subcommands/update.py:46-61`.
- The default-switch and update-in-place behaviors are also exercised in `tests/hermes_cli/test_update_parked_branch_guard.py:305-400`.
- Normal successful update includes fleet restart/verification: `hermes_cli/update_cmd.py:1531-1535`.
