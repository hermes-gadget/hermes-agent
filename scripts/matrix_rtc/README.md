# Native MatrixRTC transport qualification

This qualification harness supports Hermes Matrix live voice. It builds
Element's native SDK and exercises actual encrypted audio against an isolated
Synapse, MatrixRTC authorisation service and LiveKit SFU. The gateway integration and setup instructions are in
[`plugins/platforms/matrix/README-live-voice.md`](../../plugins/platforms/matrix/README-live-voice.md).

## Qualification result: 29 October 2026

The native delayed-sticky fix passes **all six gates** after the final compatibility patch.
The separately enrolled helper also passes incoming encrypted PCM duplex and
child-reaping tests in all three formats, plus an actual call through the production
VPS with disposable accounts ([incoming receipt](validation-incoming-2026-10-29.json),
[production receipt](validation-production-2026-10-29.json)). The patched
native client also passed the upstream Element Web/embedded Element Call
interoperability tests in room-state and sticky modes (2 tests, 45.2 s).
The reference client was Element Web `v1.12.25`. Ben's actual desktop and mobile
clients still require acceptance testing after device enrolment and integration.

The fix pins matrix-rust-sdk's encrypted delayed-send API from upstream PR
[#6973](https://github.com/matrix-org/matrix-rust-sdk/pull/6973) at commit
`f076a9de6f59b43509d0cbbd4b1077c5c58d2c22`, and changes the RTC adapter to call
`send_raw(...).with_sticky_duration(...).into_delayed(...)`. The SDK prepares
and encrypts the event; no custom crypto is introduced. The
[patch](native-delayed-sticky.patch) includes the resolved lockfile, and the
[fixed-run receipt](validation-fixed-2026-10-29.json) records six passing gates.
The upstream API is still a draft, so this is a pinned local patch pending an
upstream RTC release. The patch also limits slot-state ingestion to Current mode:
legacy sticky Element calls do not publish `m.rtc.slot`. The no-slot duplex test
reproduced `slot is not open` before that fix and passes encrypted audio after it;
Current still reads and enforces slot state.

To reproduce the fixed native run, add `--native-fix` to **each** prepare,
build and test command below. Use separate work directories for baseline and
patched runs; the runner refuses to overwrite unrelated SDK edits.

### Original release baseline

The native build succeeded. Actual SFU media tests, using independent users and
Olm-distributed media keys, produced these results:

| Membership format | Encrypted audio both ways | Killed peer removed within 45 s |
| --- | --- | --- |
| Current | Pass | **Fail** |
| Sticky2025 | Pass | **Fail** |
| RoomState | Pass | Pass |

Every received tone had a measured energy ratio of 1.000. All 31 existing
Discord GPT-Live regression tests also passed through `scripts/run_tests.sh`.
The machine-readable [receipt](validation-2026-10-29.json) records all six
native gate results. Full lab logs are kept in the disposable work directory.

The failing peers were audible before their processes were killed and reaped;
their authenticated membership still appeared in the surviving client's roster
45 seconds later. The pinned SDK's `send_delayed_sticky_event` explicitly returns
`DelayedEventsNotSupported`, causing a degraded five-minute membership lifetime.
This reproduces the missing operation tracked in
[matrix-rust-sdk #6973](https://github.com/matrix-org/matrix-rust-sdk/pull/6973),
which was still open when checked. RoomState uses the supported delayed-state
leave and passed the same process-death test.

Element Call v0.26.1 documents
[Compatibility as its default mode](https://github.com/element-hq/element-call/blob/v0.26.1/src/config/ConfigOptions.ts),
using room-state membership. This is a viable candidate for a restricted first
release, but these two-native-client tests do not establish compatibility with
Ben's actual desktop and mobile builds. The fixed run above clears the native
cleanup issue. Device enrolment, integration and production infrastructure are now implemented.
Ben's actual desktop/mobile client acceptance remains a separate check. The
original baseline qualification did not change production configuration.

## Run the lab

Use Linux with Docker Compose, a host Rust installation accessible through
`~/.cargo` and `~/.rustup`, and at least 8 GiB of free disk space. The initial
build takes several minutes. Rust 1.99.1 and clang 21 were used for qualification;
clang, glib and sqlite development libraries are confined to the builder image.
The container runs as the invoking user, with three CPUs and 6 GiB of RAM.

From the Hermes checkout:

```sh
python3 scripts/matrix_rtc_lab.py prepare --work-dir /absolute/path/to/disposable-lab
python3 scripts/matrix_rtc_lab.py build --work-dir /absolute/path/to/disposable-lab
python3 scripts/matrix_rtc_lab.py test --work-dir /absolute/path/to/disposable-lab
```

The test command writes `receipt.json` and one log per gate, then removes only
the lab containers and its named volume. It returns nonzero when a gate fails.
Use `--format current`, `--format sticky` or `--format state` to narrow a rerun.
After an interrupted run, clean up with:

```sh
python3 scripts/matrix_rtc_lab.py down --work-dir /absolute/path/to/disposable-lab
```

The lab binds only to loopback: TCP 8008, 7880–7881, 7890–7891, 16080–16081;
UDP 50100–50200 and 50300–50400. It uses open registration and public development
secrets on its private Docker network. Never reuse these configuration files for
production. The runner passes no live Matrix or OpenAI credentials into the lab.
All users, devices and rooms are disposable. Audio consists of generated tones;
the Hermes gates do not write audio recordings.

## What the gates prove

`hermes_gate.rs` is compiled as a child module of the pinned SDK's existing
`e2e_call` test harness, reusing its account provisioning and encrypted-room
setup. The baseline does not change SDK implementation; `--native-fix` applies the
reviewed patch described above. Cargo uses the checked-in lockfile.

For each SDK membership format (`Current`, `Sticky2025`, `RoomState`):

- **Encrypted duplex:** two separately authenticated users/devices exchange
  media keys, publish distinct tones, decode and verify the other's PCM stream
  through the SDK media API, bind the participant to the expected Matrix user,
  and leave cleanly.
- **Crash cleanup:** the peer runs in a separate OS process. After the observer
  decodes its encrypted audio, the test kills and reaps that process without a
  graceful leave. Its authenticated membership must disappear from the
  observer's roster within 45 seconds (30-second delayed leave plus sync margin).
  A transport disconnect alone does not pass this gate.

Passing these automated gates is necessary, but does not establish Element
Web/Desktop or Element X interoperability. Those clients must also pass actual
encrypted calls, hangup/redial and abrupt-process-death tests before rollout.

## Pinned components

- [matrix-rust-rtc v0.5.0-rc.8](https://github.com/element-hq/matrix-rust-rtc/tree/de417a84c566932192c011d9931403b304b9156b),
  commit `de417a84c566932192c011d9931403b304b9156b`.
- Its locked matrix-rust-sdk revision is
  `c6ad52ecc09eaa36601f55effb0a48a533eb87dd`.
- Synapse `v1.164.1`, lk-jwt-service `0.9.0`, LiveKit server `v1.13.11`.
- Ubuntu builder base is pinned by digest in `Dockerfile`.

The SDK is distributed under AGPL-3.0-only OR Element Commercial terms; its
copyright, license and source remain in the fetched checkout. See its
[LICENSE](https://github.com/element-hq/matrix-rust-rtc/blob/de417a84c566932192c011d9931403b304b9156b/LICENSE)
and the upstream test files' SPDX headers.

## Integration after transport qualification

Keep the agreed product scope: incoming private calls from Ben to the existing
Hermes contact, from Element Web/Desktop and Element X. Use `gpt-live-1` with
Gleam, normal Hermes tools and memories, quiet backend progress, final replies,
barge-in and no `[Voice] @Ben` transcript echo.

The gateway owns the native helper as a reaped child process with bounded
control/PCM queues and call-generation IDs. Its verified Matrix device and
crypto store must be separate from the mautrix chat device and scoped per
Hermes profile. Do not copy the chat crypto store, reuse its token with a new
store, or reset the account's cross-signing identity to enrol the call device.

The tested Discord GPT-Live wire client is shared behind platform-neutral audio
and backend-dispatch callbacks after the transport gate passed. Preserve
the continuous 20 ms PCM clock, progress/final correlation and current Discord
regressions. Calls use a shared per-profile lease: decline busy while Discord
is active, accept only fresh authorised rings in two-person encrypted DMs, and
stop on an unexpected third participant. Matrix backend requests enter through
the normal Matrix routing and authorisation path.

Before enabling production, back up and rehearse restoration of the vexcore
Synapse database, signing keys, media and configuration. Preserve its server
identity, the `synapse` Docker alias, the separate Endermatrix stack and coturn.
Deploy authenticated LiveKit and lk-jwt-service with `/livekit/sfu` and
`/livekit/jwt` TLS routes, homeserver RTC discovery, compatible delayed leaves
and application-service integration. Restrict full access to `vexcore.org`,
disable SFU room auto-creation and test UDP plus TCP media fallback.

Activation is off by default. Enrol a verified device and pass the automated
gates before enabling private, allowlisted acceptance calls. Actual client
acceptance must cover both client families, tools/memory, progress, interruption,
redial, busy handling and cellular access. The production TCP-only media test
also passed with UDP rejected inside its disposable network namespace
([receipt](validation-tcp-2026-10-29.json)). The existing authenticated TURN/TCP
service on 3478 supplies fallback because the provider blocks direct TCP 7881.
LiveKit and coturn use host media networking to avoid public-address hairpin
failures; signalling binds only loopback and the private Caddy interface.
Coturn retains its credentials/certificates and uses UDP relays 49160–49200. Preserve the agreed
15-minute idle, 60-minute maximum and 180-second backend timeout defaults;
record no call audio and keep API `store: false`.
