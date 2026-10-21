# Maintaining the private Hermes repository

This independent private repository is `gadgethd/hermes-agent`. It retains the
official Hermes history and license, plus the local customizations. It is outside
GitHub's public fork network. GitHub Actions are disabled initially so inherited
release, publishing, and scheduled workflows do not run during migration.

## Branches, remotes, and the initial snapshot

| Name | Purpose |
| --- | --- |
| `origin` | Private source: `https://github.com/gadgethd/hermes-agent.git` |
| `upstream` | Official source: `https://github.com/NousResearch/hermes-agent.git` |
| `main` | Current Hermes source with local changes |
| `upstream-base` | Pristine official commit beneath our current patch series |
| `snapshot/current-20261021` | Complete source snapshot at private-repository migration |
| `snapshot/committed-before-private-20261021` | Original committed HEAD, before recording outstanding changes |
| `upstream-base/initial-20261021` | Immutable original upstream base |

The original committed HEAD was `8d3f1919beb8454073817049f733928bd636d797`.
Its shared upstream base was `b9df1cccaec26a910e5f0d1d77f52a71641e3d98`.
The migration preserves the 21 commits between those points without rewriting
their authorship. Four further commits record the outstanding source changes;
the maintenance commit adds this guide and excludes local `.fw-context` files.
No upstream upgrade is part of this snapshot.

On the existing installation, `fork` remains the separate public
`hermes-gadget/hermes-agent` remote for any public contribution work. Private
changes are pushed to `origin`.

## Customizations carried at migration

| Area | Purpose / commits |
| --- | --- |
| Approval and write guards | User deny-glob boundaries and trusted cron instruction-write handling (`5602d3ec0d`, `5381dc3c24`) |
| OpenViking | Avoid conversation writes from non-primary contexts; retain matching regressions (`22d2f12a95`, `821bac6e36`) |
| Search and terminal | Dash-pattern handling, package-install guidance, and local PYTHONPATH isolation (`2174787ea1`, `b30862afd3`, `f10a009b9f`, `690de4b51a`) |
| Tool-call repair | Recover malformed envelopes with bounded/redacted diagnostics (`b0a1c7964c`, `fd593e667d`, `a8d84acd72`) |
| Runtime and cron | Backlog fixes, session-lease resilience, and transient SQLite-lock retries (`343d797b08`, `5381dc3c24`, `3cfaae9b7b`, `9784f961b3`, `e91ecc9391`) |
| Channel delegation | Routing, capacity, per-channel opt-out, and isolated credentials (`a60929b6c6`, `6343d26ded`, `59ee1c1727`, `839fc3d844`) |
| LongCat | Same-model key rotation, output budget, and bounded continuation (`2ffb5df001`, `8d3f1919be`) |
| Reaped sessions | Bound database probes off the gateway event loop (`ccaad1833f`) |
| Child notifications | Prevent subagent completions from repointing parent conversations (`865e9edfbb`) |
| Display cleanup | Strip leaked provider verdict blocks from displayed messages (`3073eae8bf`) |
| Voice replies | Summarize long replies before synthesis in both auto-voice paths (`7e6d00af22`) |

Inspect the complete current patch series with:

    git log --reverse --oneline upstream-base..main
    git diff --stat upstream-base..main

Keep future changes focused by feature or fix. Commit the related tests alongside
each behavioral change, and update this inventory when a customization changes or
upstream adopts it.

## Preparing an upstream rebase

Use a separate worktree; the running installation should stay on its current
source until the candidate is reviewed and tested. Choose a fresh date/name for
each attempt. Starting in a clean maintenance checkout:

    git fetch origin
    git fetch upstream main
    git tag snapshot/pre-rebase-YYYYMMDD main
    git push origin refs/tags/snapshot/pre-rebase-YYYYMMDD
    git worktree add -b sync/YYYYMMDD ../hermes-agent-sync-YYYYMMDD main
    cd ../hermes-agent-sync-YYYYMMDD
    git rebase --onto upstream/main upstream-base

Resolve each conflict against the behavior described above. Check whether
upstream already implements a fix before retaining its local patch. Abort with
`git rebase --abort` if the candidate needs to be reconsidered. After a successful
rebase, review which patches survived and run the relevant tests:

    git range-diff upstream-base..main upstream/main..HEAD
    scripts/run_tests.sh tests/gateway/ tests/agent/ tests/cron/ tests/tools/
    git push -u origin sync/YYYYMMDD

Test through `scripts/run_tests.sh`, as required by AGENTS.md. Use an isolated test
environment; this command is an example of the affected areas, not a claim that
the full suite has passed. Publish the candidate branch for review before moving
the running checkout or private main.

Promotion is a separate, deliberate deployment: recheck that private main has not
moved, preserve its old tip, update the tracked upstream base to the candidate's
actual upstream commit, and use an explicit `--force-with-lease` when replacing
main with a rebased series. Do not deploy via an unreviewed upstream pull.

`hermes update` recognizes custom origins and currently skips automatic upstream
sync when private origin/main has commits absent from upstream. It still performs
installation work; use the reviewed candidate workflow for upstream upgrades.

## Authentication and local-only data

On the original machine, the repository-specific Git credential helper obtains
the existing gadgethd authentication over the `projects-vps` SSH connection. It
does not store a token in this repository or change the global hermes-gadget
login. Access from this machine therefore depends on that SSH connection. On a
new machine, authenticate GitHub as gadgethd or use a separately authorized
collaborator account.

Only source history and the reviewed source changes are included. Secrets,
profiles, sessions, logs, virtual environments, `.fw-context`, and runtime
configuration stay outside version control. Plugins or skills installed outside
the source checkout require their own source tracking and deployment; this
repository does not back up the entire Hermes home directory.

## Migration validation

The canonical test runner passed **97 tests across eight gateway files** covering
reaped-session eviction/recovery, background notifications, auto-voice formatting,
voice delivery/isolation, and warning notifications. These checks cover the
affected gateway paths; they are not a full-suite or live-provider test.

The local backup contains a verified complete Git bundle, the original Git
configuration, the outstanding tracked patch, and the new voice-summary source.
The migration also compares the prepared source against the captured working
files before connecting the live checkout to private main.
