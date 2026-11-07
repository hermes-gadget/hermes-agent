# Private voice imports during the pinned upstream integration

The private Matrix and Discord implementations share the `plugins.platforms.live_voice`
library. It is an internal library without a plugin manifest, so upstream's import
boundary checker classifies imports from it as module-to-core imports. Keeping one
implementation preserves common interruption, request ownership, leases, playback,
tool observations and child-result coordination across both transports.

The boundary baseline records 19 exact imports from that library and three existing
private integration dependencies. The Discord probe uses
`hermes_cli.env_loader.hydrate_profile_secret_sources` to load the selected profile's
secret sources. Matrix uses `gateway.session_identity.replace_source` to preserve
the authoritative conversation source, and `tools.environments.local.served_profile_child_env`
to pass the served profile's environment to the native helper. The pinned upstream
plugin API does not publish equivalent contracts for these three operations.

These imports are deliberate dependencies within this private distribution; they
do not expand the public plugin API. The baseline update adds only these 22 named
crossings, each with count one. Further crossings still fail the checker. Voice
coordination, profile credential isolation, native helper startup and the boundary
checker are exercised separately; live Matrix and Discord acceptance remains an
owner-initiated deployment gate.
