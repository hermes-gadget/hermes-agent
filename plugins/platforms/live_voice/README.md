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
installations; set it false and end/redial the call to restore the previous voice
behaviour. Disabling it does not cancel project work. Use the normal `hermes
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
