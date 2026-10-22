# Source update completion ownership

## Phase seam

The command process owns admission, the update lock and output lifetime, pre-update
inventory, all-profile snapshots, gateway pause, Git selection/stash/restore and
syntax/HEAD guards, and the ZIP download/stage/dirty recheck/release graft/swap.
It imports the completion transport before swapping code. Once the final tree is
selected (including upstream merge), Git, already-current retry and ZIP all send
one versioned JSON request to `update_completion.py` **from that tree**. No cached
application module is evicted or reloaded in the command process.

The request carries canonical source/home, desktop product selection, interactive
and gateway mode, pre-update version, active and sibling snapshot identifiers,
serialized runtime plan, open receipt identity/data and paused-Windows token. It
contains data, never callables or pickles. stdin stays inherited for interactive
configuration prompts; gateway mode retains its non-interactive behavior. Child
output stays visible and is mirrored by the parent's update output stream.

## New-code owner

A stdlib-only entrypoint starts using the available Python with `-I -S`, so no
old site-packages or executable `.pth` files initialize. A private bytecode-cache
prefix fences stale cache files before any new-checkout imports. Its explicit
import path points at the new checkout. It calls the new PM interface to prepare the
recorded dependency union, then starts the selected Python with the new activation
environment. That interpreter also starts with site initialization disabled,
then the runtime owner leases and activates its selected generation before any
application imports. Only that interpreter imports application completion code. The same
receipt/correlation identity crosses this preparation boundary (including PM
results). Selected-Python completion owns launcher publication, builders, cache
invalidation, all-profile configuration/state/skills maintenance, process scans,
fleet restart, Windows resume, dashboard deduplication and verification.

The existing per-kind restart and abort-recovery algorithms remain; transient
supervisor/process failures are real even without mixed-generation imports. Only
the purge/reload workaround and independent retry/ZIP tail compositions disappear.
Gateway exit status is written before a restart can terminate the updater's cgroup,
and is demoted on later failure. Verification publishes the final receipt.

## Parent lifecycle and failures

The parent waits and propagates the child's exact nonzero result (a signal is
mapped to shell-style 128+signal). A child cannot succeed by merely exiting zero:
a terminal response with the matching receipt identity is required. The response
returns the mutated Windows token so the parent's registered emergency resume does
not repeat completed work. Normal parent completion performs no maintenance.

The parent retains its original receipt until acknowledged child finalization;
missing/failed child output leaves it available to the existing command-boundary
failure finalizer. The stdlib bootstrap returns correlated PM failure data even
when application imports are unavailable, and normalizes negative signal exits
at each process boundary. POSIX completion owns a new session/process group;
cancellation kills that group before releasing the lock (Windows uses the retained
child's `taskkill /T` tree). The parent records the pending fleet obligation before
starting the completion process, including when preparation cannot begin. The parent's emergency Windows resume remains a last-resort
lifecycle obligation when the child cannot execute or is killed. A failed child
never clears the pending fleet obligation. No automatic code rollback after
maintenance has begun (SQLite snapshots remain file-loss recovery, not rollback).

## Historical surface

All names frozen from the complete reachable shipped updater history stay
resolvable. Historical dependency hooks retain the stdlib-only takeover bridge:
the old parent waits, carries receipt/recovery state and never resumes a retired
installer. Newly retired preparation and module-reload hooks explicitly marked
incomplete stop nonzero and request `hermes update` again; they cannot manufacture
a missing completion request. Current Git/current/ZIP callers use only the
canonical completion transport, not the historical takeover entrypoint.
Unfrozen branch-only retry compositions are deleted, not shimmed. ACP convenience
publication uses the launcher owner's `expose_cli`; the historical ACP entry is
only an adapter, never a second writer. The frozen set is never trimmed or replaced
with tag-only coverage. New current-path imports are unioned with that history.

## Source health and admission

`hermes_cli/source_health.py` closes the interval that a post-update probe alone
cannot cover: Git/stash/branch changes and the individual ZIP swaps happen while
old processes still have cached Python modules. The updater closes admission
before any of those mutations and waits up to 30 seconds for startup, agent
construction and active turns to release their source leases. If work stays
active, it restores the previous gate state and refuses without changing source.
An update started from a tool in an active turn therefore refuses after this
bounded wait; retry it from a separate terminal after the turn finishes.

The gate and kernel-held reader/writer locks live in
`<checkout>/.hermes/source-health/`, an already ignored runtime directory shared
by every profile using that checkout. They are independent of the updater's
home-scoped UI marker and have no age-based expiry. A failed or killed update
leaves a durable `updating` record; losing a PID or releasing the writer lock
does not reopen sessions. Read-only package trees without a gate do not need
to create one: their deployment manager owns code replacement.
Status/stash pathspecs and the ZIP preserve set protect the gate even when a
checkout's local ignore rules omit it; autostash cannot remove its locked files.
Completion processes hold their own leases through PM preparation and selected
Python completion. Killing the transport parent releases its writer lock, but a
successor updater must still drain those children before touching source.

After dependency preparation, completion probes imports in a fresh selected
interpreter before application maintenance and again before publication. The
probe includes `agent.tool_executor` and `agent.conversation_loop`, alongside
the CLI, agent facade, model tools, toolsets and gateway. This resolves lazy
imports such as `agent.display.tool_row_emoji` before the first tool call.
Missing dependencies, a probe that cannot run, and imports resolving to another
checkout fail the mandatory probe. Failed health checks prevent normal builds,
readiness publication and fleet restart. The historical finish path uses the
same health check.

After final source health passes, completion publishes a new generation before
restarting the fleet. An unrelated partial SQLite-runtime verdict still fails
the update receipt as before; it does not hold otherwise healthy code offline.
Bootstrap captures that generation before importing application or PM
code; existing processes cannot adopt it midway through startup or on their next
turn. Gateway startup and the first agent construction eagerly import the tool
execution graph as well. A stale process refuses agent work with a restart
message, including when `--no-gateway-restart` deliberately defers the restart.
This preserves cached prompts and modules rather than reloading a conversation.

Operators see `Gateway refused to start` or `sessions remain held`, the failing
module/symbol, and instructions to finish/retry `hermes update` and restart the
affected processes. `python -m hermes_cli.source_health` checks the selected
runtime without reopening admission. Do not remove the gate to bypass a failed
self-test. Retry the updater to complete its normal dependency/build/restart
flow. Existing syntax rollback, rescue refs, ZIP rollback and stash preservation
remain; no automatic code rollback is added after maintenance has begun.

This is a maintenance pause, not a versioned-tree or zero-downtime deployment.
It avoids replacing checkout paths, editable installs and the existing rollback
machinery. The first rollout still needs the normal fleet restart: code already
loaded by an older release has no source-admission guard. Subsequent updates are
protected once the guarded processes are running. Arbitrary external edits or
Git commands that bypass the updater do not participate in this protocol.

## Verification

Use isolated homes, disposable Git repositories and fake dependency/build/service
adapters only. Exercise an old process with cached incompatible modules across a
real Git transition to new code, selected-Python execution, receipt identity and
snapshot transfer, nonzero/abrupt child exit, lock release and Windows-token
return. Focused existing tests cover dirty ZIP checks/grafts, snapshots, fleet
reconciliation, supervisor timing and historical imports. Native service restart
and Windows/macOS acceptance remain separate required lanes. Run these checks
without live service restarts or access to live user state.
