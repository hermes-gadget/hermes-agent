# Live assistant integration

Matrix is the personal assistant; Discord is the project and coding assistant.
Both use the existing GPT-Live client delegation transport and their normal
Hermes conversation, tools, memory provider and approval rules. The additional
assistant behaviour is opt-in, per transport and configured owner:

```yaml
matrix:
  gpt_live:
    enabled: true
    assistant_enabled: true
    assistant_role: personal
    user_id: "@owner:example.org"
discord:
  gpt_live:
    enabled: true
    assistant_enabled: true
    assistant_role: project
    user_id: "123456789"
    voice_channel_id: "234567890"

voice:
  assistant:
    projects:
      radio:
        channel_id: "345678901"
        profile: default
    # Optional read-only operations feed. Native gateway status works without it.
    fleet_url: http://127.0.0.1:8788/api/v2/state
    # Explicitly include unparented workers belonging to these feed owner IDs.
    fleet_owners: [local]
```

Keep the existing voice, credentials and custom speaking instructions. The role
is appended to those instructions. Defaults are `personal` for Matrix and
`project` for Discord. `assistant_enabled` defaults to false for existing
installations; set it false, apply the normal adapter reload, and redial to restore
the previous voice behaviour. Disabling it does not cancel project work. Use the normal `hermes
config set` configuration interface; no extra credentials or service are needed.

## Project execution

The optional `live_voice` toolset is added only to configured owners' voice
control conversations, through the adapter's session toolset extension. It is
not a core tool bundle and respects `agent.disabled_toolsets`. Its
`voice_assistant` tool validates the originating call, owner, profile and coordinator
scope at execution. Other channels keep their normal toolsets.
An already admitted coordinator turn may finish dispatch after hangup; the exact
gateway event must still own it, and an inherited voice marker alone is insufficient.

`projects` discovers authorized Discord destinations. Explicit `projects`
bindings win; otherwise a named channel's configured prompt/topic or an
owner-authored pinned brief may designate one execution channel with a Discord
channel mention. Multiple references require an explicit destination. A channel
ID is an explicit destination and does not follow references. Configuration,
topics and owner briefs are the routing sources; ordinary chat is not one.

`dispatch` creates a visible task brief, follows the channel's existing
auto-thread/forum rules, and admits a normal user-authorized gateway turn. The
target's model, channel prompt, skills, profile routes and workspace instructions
apply. The voice conversation and its coordinator restrictions do not replace
the project agent's conversation. The result includes a monitoring URL.

Each request claims a durable receipt before Discord I/O. Reuse its `request_id`
on retry. Acceptance means admitted/queued, not completed. A crashed or uncertain
admission is never replayed automatically; inspect the linked conversation and
fleet state before issuing another request. Missing Discord visibility refuses
execution. Replies still use normal Discord delivery after the caller hangs up.

`agents` returns verified fleet state and prior request receipts. `steer` and
`stop` require the receipt of the currently active channel task; a queued,
finished or replaced task cannot accidentally control a different job.
`continue` sends a new task into the prior request's Discord conversation.
`select` persists the chosen project for the voice conversation. Source
authorization is rechecked after Discord I/O, and final reports retain their
own receipt and original call: an old result cannot satisfy a newer request or
jump into a redialed call.

## Fleet status

Gateway session state includes agents started outside voice. The native worker
registry supplies owned descendants and their latest observed tools. The
optional operations feed adds external coding workers. Parentage grants no
extra access: channel workers must belong to an authorized owner conversation;
unparented workers need an explicit matching `fleet_owners` entry.

Snapshots expose location, task, state, activity, observation times, pending
input and coverage. Tool activity is not proof of success. Stale external
activity is labelled unknown, and a `notLoaded` coding session is not called a
current failure. Feed errors do not stop native status. Snapshots refresh every
ten seconds; changed facts enter quiet context and new input requests can be
spoken. Completion uses the separate verified result path. Voice context
contains at most thirty fleet records and declares omissions; the status tool
can filter by project or agent ID. Discord's project role receives only the
existence/state of personal Matrix sessions, not their conversation details.

## Continuity and personal work

SessionDB stores timed voice fragments and call summaries separately from model
messages. Input and output transcript event IDs deduplicate fragments; repeated
words remain valid. Interruption marks affected output without pretending to
know exact audio playback. No raw call audio is saved. Startup context combines
bounded spoken history, recent text and verified project receipts; `/new`
clears conversational references and project selection, while compression keeps
the conversation's lineage. Slow memory recall runs after audio connection
through the existing cached memory manager.

Matrix uses existing calendar/email integrations, normal memory-provider tools,
and the scheduler. Explicit preference/decision writes and reminder scheduling
remain normal checked transactions, while longer work runs in background
workers. A reminder must become a real scheduler entry with an unambiguous date,
time and timezone. No briefing schedule is created without a request. Short
coordinator answers go directly to GPT-Live; longer reports retain the configured
voice-summary route. Status context stays quiet instead of narrating periodic
worker counts.

## Validation

Run with the checkout's test interpreter through `scripts/run_tests.sh`:

```sh
scripts/run_tests.sh tests/plugins/platforms/live_voice/ \
  tests/plugins/platforms/matrix/test_live_voice.py \
  tests/gateway/test_discord_gpt_live.py
```

Tests use real SessionDB, gateway identity/admission and Discord adapter code
with fake Discord I/O, plus a local GPT-Live WebSocket server. No real Discord
messages, paid API calls or live call audio are used. Actual Element and Discord
clients still require listening acceptance for acoustic timing and voice style.

## Questions, attachments and recovery

Use `voice_assistant(action=prompt, question=..., choices=..., proposed_action=...)`
before asking an operational question or offering an executable action. `respond`
accepts its `prompt_id` and `response`. There is one foreground question; other
questions remain queued. Background reports do not replace the foreground
referent. An interrupted question must be repeated with `prompt` before assent
executes its action. Native clarification responses are tied to their exact ID;
tool approvals retain the native approval flow.

`attach(agent_id=...)` watches an existing native Hermes root or owned child and
returns an `attachment_id` for `steer`/`stop`. A pending native question is returned
with the attachment. Root controls check the source, session ID, process-local
execution generation and agent identity at admission and execution. A replaced
run is refused, including when `/stop` would ordinarily broaden to sibling
sessions or `/steer` would queue a new turn. Saved IDs do not restore authority
after a gateway restart. `detach` stops monitoring. External MissionDeck/Codex
workers remain visible through status. Discord's project role hides persisted
personal results and artifacts as well as live personal conversation contents.

A transient GPT-Live WebSocket failure now recovers within the same Matrix or
Discord call. Four attempts with increasing delays share a 30-second deadline.
The logical call, voice lease, pending questions and accepted backend work stay
in place; each connection gets a new vendor session and separate usage record.
Up to five seconds of opening speech are buffered in memory. Overflow discards
the incomplete utterance and asks for repetition. Hangup cancels recovery; an
authentication/configuration failure is permanent. No raw call recording is
created and accepted actions are not replayed.

## Alert preferences and briefings

One observer per owner/profile is shared by the connected adapters, including
between voice calls. It uses verified native completion hooks for watched roots
and children. Blockers and watched completions are the default spoken notices;
off-call blockers go to the owner's verified Matrix DM, while project results
keep their normal Discord delivery. Ambiguous Matrix DM destinations remain
queued until an existing personal room is configured. Resolved blockers are
discarded before delivery; uncertain sends are retained for inspection.

```yaml
voice:
  assistant:
    notifications:
      mode: watched          # decisions | watched | milestones | muted
      timezone: Europe/London
      quiet_hours:           # false disables unsolicited-alert quiet hours
        start: "22:00"
        end: "08:00"
      # matrix_room_id: "!your-existing-personal-room:example.org"
```

`notifications(mode=..., until=...)` persists preferences using the normal
comment-preserving config writer. `until` is an optional future ISO timestamp
with timezone; when it expires the base mode returns. Quiet hours affect
unsolicited notices, while direct replies and requested reminders use their
normal delivery. Spoken notices wait for a quiet playback boundary. Optional
milestones describe observed activity and do not claim successful completion.

`briefing` starts one native background worker for a requested, role-appropriate
briefing from configured integrations and verified project state. Unavailable
sources must be identified. Recurring briefings require an explicit request and
a normal `cronjob` prompt/destination; scheduled work must not call the live-only
`voice_assistant` tool.

## Screen handoffs

`agents` includes verified result/artifact IDs. Use `handoff(artifact_id=...,
destination=current|project|matrix)` to send the selected result or file. An
explicitly produced `MEDIA:` file is frozen into the owner's native cache with
a content hash; later edits to its source do not change the delivered artifact.
An expired or changed cached file is refused. Arbitrary file paths are not tool
arguments. Multiple files require an explicit artifact choice.

For a missing artifact, use `handoff(produce=true, artifact_id=<project-request>,
task=<requested-file>, destination=...)`. This posts a visible follow-up into
the owning project conversation and completes the requested delivery after its
verified result, including after hangup. The durable delivery receipt prevents
replay. Native cache retention applies to frozen files; the original project
conversation remains the source location.

## Conversation evaluation

The automated suite includes a real local WebSocket reconnect, native Discord
admission/media delivery, exact clarification responses, stale execution guards,
profile alternation, native child control, off-call alerts and deferred file
handoffs. Fake external I/O keeps these checks free of live messages/model calls.

Run the listening scenarios in actual Element and Discord clients against a
disposable project fixture. Use each platform's normal call entry, run the
scenario, and record the observed result; repeat each scenario three times.
Only use dummy calendar/email/reminder data and explicitly requested test actions.

```sh
python scripts/evaluate_live_voice.py --list-scenarios
python scripts/evaluate_live_voice.py --observations listening.jsonl \
  --usage /path/to/profile/logs/gpt-live-usage.jsonl \
  --require-complete --min-repeats 3 --output voice-evaluation.json
```

Each observation is one JSON line, for example:

```json
{"platform":"matrix","scenario":"foreground_followup","success":true,"wrong_target":false,"duplicate_action":false,"response_latency_ms":850,"notes":"Human-reviewed pacing and question reference."}
```

`response_latency_ms`, `interruption_ms` and `recovery_ms` are optional measured
observations. The report distinguishes these from backend-handoff-to-generated-
audio timings, reports p50/p90 and coverage, and deduplicates vendor usage
snapshots. Playback counters record frames actually read by the platform mixer,
separately from generated or discarded bytes; they are not proof of speaker
audibility. Critical wrong-target/duplicate-action failures always fail the
report. The reporting command only reads local files and makes no API calls.
