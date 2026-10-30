# Matrix live voice

Incoming MatrixRTC audio calls enter the same Hermes agent, Matrix conversation,
permissions, tools and memory as text messages. The voice frontend uses
`gpt-live-1` and Gleam. User transcripts are not echoed to the message thread;
assistant text, tool previews and results retain normal Matrix delivery.
Backend progress is quiet voice context, and final/late replies keep their
original delegation ID. API sessions set `store: false`; call audio is not saved.

This implementation answers fresh rings from one configured Matrix user in an
invite-only encrypted room containing exactly that user and Hermes. It stops
when an unexpected participant appears. It supports MatrixRTC/Element Call
room-state, 2025 sticky and current signalling; legacy P2P `m.call.invite` calls
are outside this implementation. Discord and Matrix share a lease per routed
Hermes profile: an active call makes the other transport busy.

## Install and enrol

Build the helper with Docker and the pinned Rust toolchain described in
[`scripts/matrix_rtc/README.md`](../../../scripts/matrix_rtc/README.md):

```sh
: "${HERMES_HOME:?Set HERMES_HOME to the intended profile}"
python3 scripts/build_matrix_call.py --work-dir /absolute/disposable/build
install -D -m 700 /absolute/disposable/build/hermes-matrix-call \
  "$HERMES_HOME/matrix-call/bin/hermes-matrix-call"
hermes --run-module plugins.platforms.matrix.setup_call --configure-chat
```

Set `HERMES_HOME` to the intended profile before running setup. The enrolment
command reads that profile's `.env`, or prompts privately for the Matrix
password. Credentials travel over stdin; they are never process arguments or
printed. Use `--create-first-identity` only for an account with no server-side
cross-signing identity. The helper checks the server before creating one and
never replaces an existing identity. For an established account, provide
`MATRIX_RECOVERY_KEY` or verify the new device with an existing trusted client.

The native call device uses its own login, encrypted SQLite store and exclusive
lock under `matrix-call/device/`, separate from the mautrix chat device. It
persists a private session, store password and (when newly created) recovery key
with mode 0600 in a mode 0700 directory. Keep this directory in the profile's
private backup. Do not copy another device's crypto store or log in its token
against a new store. Setup can save the recovery key to the profile's `.env` to
verify the existing chat device without changing its token.

## Configuration

The homeserver must advertise an authenticated MatrixRTC backend. The qualified
stack is Synapse 1.164.1, lk-jwt-service 0.9.0 and LiveKit 1.13.11. Both old focus
well-known and current `/rtc/transports` discovery are needed across the supported
clients. See the qualification README for the application-service and delayed
leave requirements. Put signalling behind TLS and expose LiveKit UDP 7882 and
TCP 7881; keep room auto-creation disabled on LiveKit and restrict full-access
homeservers on lk-jwt-service. Configure `/livekit/jwt` as a stripped proxy prefix
for this service version, including `/sfu/get`, `/get_token` and `/healthz`.

Require encrypted Matrix chat (`MATRIX_E2EE_MODE=required` or platform extra
`e2ee_mode: required`). In the profile's `config.yaml`:

```yaml
matrix:
  gpt_live:
    enabled: true
    user_id: "@ben:example.org"
    model: gpt-live-1
    voice: gleam
    idle_timeout_seconds: 900
    max_session_minutes: 60
    reply_timeout_seconds: 180
```

The default helper path is profile-local; `helper_path` can select another build.
`ca_file` is only needed for a private test CA. OpenAI credentials stay in the
Python gateway and follow the routed profile's existing secret scope; the
native child inherits only OS, proxy and TLS settings.

The helper and gateway use a private inherited socket with bounded framed PCM
queues and generation IDs. Audio is mono PCM16 at 24 kHz, continuously paced in
20 ms frames. Native 48 kHz receive frames are downsampled before forwarding. Completed
streaming preambles retain interim metadata even when their deltas and segment
break arrive in one tick, so they cannot finish a pending voice request.
Playback and native queues flush on local barge-in. The gateway reaps a crashed
helper and tries one restart; shutdown sends a graceful leave before terminating
a stuck child. Server-side delayed leaves clear membership after abrupt death.

## Validation

Run Python checks through `scripts/run_tests.sh`, including
`tests/plugins/platforms/matrix/test_live_voice.py` and the existing Matrix,
Discord GPT-Live and voice-mixer suites. Native qualification runs real encrypted
duplex media and process-death cleanup for all three dialects. The additional
`hermes_gate::incoming_helper` test enrols a separate verified device, rings it,
exchanges PCM across the real process boundary and verifies clean child exit.
Run it after building the helper into the same native target directory. No real
call audio or live credentials are used in those tests.

Real-client acceptance must cover Element Web/Desktop and Element X: incoming
call, tools/memory and final text, interruption, hangup/redial, Discord busy,
cellular access and TCP media fallback. Automated native/reference-client tests
do not establish those results for a user's actual installed clients.

## Multiple jobs during a call

Both Matrix and Discord use the shared voice coordinator. Independent voice
requests can remain pending concurrently (up to four), with replies matched to
their own request. Voice backend turns coordinate native background subagents;
a scoped pre-tool policy prevents the parent from doing worker tool calls itself.
The parent can delegate, manage its jobs, inspect tool metadata and clarify a
request, then return promptly after confirmed dispatch. Children keep their normal
tools, delegation limits and approval rules. Simple conversation and restating
verified results can be handled directly.

Background completion reports are spoken once as separate updates, including
when another request is pending. They are restricted to the same authorized
user, profile, and conversation. A completion already queued for an old call
will not jump to a redialed call. Jobs belong to the conversation and continue
after hangup; their normal text results remain available.

Queue-only admission applies at both adapter and runner busy guards, including
the startup sentinel. Separate requests remain FIFO entries rather than merged
steers. A delegation with no fresh transcript cannot replay an earlier request;
standalone conversational holds such as "one moment" stay in the voice frontend.
An immediate empty thinking append suppresses pending automatic wait acknowledgments
while transcript settlement or backend work continues. This cancels pending
acknowledgments, not speech that the model has already generated.

Live replies retain the configured `auxiliary.voice_summary` route (DeepSeek in
the private deployment), using a style-preserving recap before GPT-Live speaks.
The summarizer keeps original warmth, facts, uncertainty and job status, and adds
no new filler or promises. Ordinary voice-note summary behavior is unchanged.

The voice model also receives a conversation-owned roster of every active
worker, including nested agents, with task labels, current tools and verified
outcomes. Native start/stop events retain fast jobs that finish between checks.
The roster refreshes on those events and every ten seconds; unchanged snapshots
stay quiet and subsequent context contains only changed workers. Useful spoken
progress is limited to once per 45 seconds, with a
two-minute reminder for unchanged work. Final reports still use their separate
completion route. Tool activity never proves that an action succeeded.

Tracking survives a redial and session compression; `/new` clears the roster.
All active workers remain visible, with up to 128 inactive records plus any
ancestors needed to track live descendants. Older history is counted explicitly
as omitted. Status exposes no transcripts, tool arguments or raw error details.
