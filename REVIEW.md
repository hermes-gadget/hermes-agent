# Backlog round 2 review — hermes-agent

Branch: `backlog2-20261003` (local only). No remotes were changed, and no live
Hermes services or user configuration were modified.

## #476 — IMPLEMENTED

The bundled Google Workspace OAuth copy-paste flow used `http://localhost:1`,
which Chrome rejects as an unsafe port. The redirect is now
`http://localhost:8765`; the skill guidance and regression test use the same
browser-safe loopback contract.

Files changed:

- `skills/productivity/google-workspace/scripts/setup.py`
- `skills/productivity/google-workspace/SKILL.md`
- `tests/skills/test_google_workspace_setup.py`

Proof: `scripts/run_tests.sh tests/skills/test_google_workspace_setup.py -q`
passed (4 tests). No live OAuth authorization was attempted.

## #493 — CLOSED-ALREADY

The current base already contains the intended secure solution. Commit
`e4aeb65599` added per-route webhook `toolsets` overrides, and
`9345c67854` bound those grants to the authenticated route after the delivery
ID security fix. The default webhook toolset remains constrained, while a
trusted route can manually opt into `terminal`/`file`; the CLI deliberately
does not let an agent-created subscription self-grant those tools.

Files changed: none for this item. The behavior is documented in
`website/docs/user-guide/messaging/webhooks.md` and covered by
`tests/gateway/test_webhook_route_toolsets.py`.

Proof: `scripts/run_tests.sh tests/gateway/test_webhook_route_toolsets.py -q`
passed (12 tests). The live configuration was intentionally not changed.

## #513 — IMPLEMENTED

`channel_prompts` was a generic shared-key bridge even for Matrix, although the
Matrix adapter has no consumer. The loader now excludes that one unsupported
`(channel_prompts, matrix)` pair and removes it from merged platform data and
`extra`, including the legacy top-level form. Generic shared-key bridging for
registered plugin platforms is preserved.

Files changed:

- `gateway/config_loader.py`
- `tests/gateway/test_config_loader_platform_shared_keys.py`

Proof: `scripts/run_tests.sh tests/gateway/test_config_loader_platform_shared_keys.py -q`
passed (3 tests), including the real YAML load/merge/bridge path. No user
`config.yaml` was edited.

## #522 — IMPLEMENTED

`hermes mcp add` now accepts `--all-tools` (with `--yes` as an alias) and
passes it through to discovery. The tool-selection prompt is skipped and all
discovered tools are enabled, so a stdio script no longer cancels on EOF.

Files changed:

- `hermes_cli/subcommands/mcp.py`
- `hermes_cli/mcp_config.py`
- `tests/hermes_cli/test_mcp_config.py`
- `tests/hermes_cli/test_subcommands_followup.py`

Proof: the focused `TestMcpAdd` run passed (4 tests), and the parser regression
test accepts both flags. The flag addresses the discovery/tool-selection prompt;
existing HTTP auth and overwrite prompts retain their interactive semantics.

## #532 — IMPLEMENTED

The deny-glob `rm` boundary treated the `rm` suffix in options such as
Docker's `--rm` as the command. The boundary now also excludes `-`, preserving
blocks for a real `rm -rf ...backup...` while allowing container commands with
backup-named mounts.

Files changed:

- `tools/approval_floors.py`
- `tests/tools/test_approval_deny_rules.py`

Proof: `scripts/run_tests.sh tests/tools/test_approval_deny_rules.py -q`
passed (39 tests), including Docker/Podman `--rm` and real `rm` controls.

## Test notes

The combined focused run excluding one unrelated pre-existing import test
passed: 89 tests, 0 failures. The full `tests/hermes_cli/test_mcp_config.py`
file still has one existing failure,
`TestMcpTest::test_cli_dispatcher_forwards_test_exit_code`: importing
`hermes_cli.main` probes the real worktree marker
`.git/worktrees/backlog2-20261003/hermes-update-pull`, which the test home I/O
guard rejects. The failure reproduces in an isolated file run and is unrelated
to these changes; the modified `TestMcpAdd` tests pass independently.

`git diff --check` passes. The external `/home/ben/mcp-gmail` checkout was not
modified because the task scope is this worktree only.
