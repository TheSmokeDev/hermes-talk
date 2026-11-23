# Slice 1: native plugin integration

The feature ships inside Hermes Talk. Its phone tools use Hermes's registry and
are hidden when Phone mode is off. Hermes renders `config_schema` using its own
plugin settings. `hermes talk phone setup|doctor|bridge` extends the existing CLI.
`/phone` extends the native command picker; `/phone review` uses `ctx.ask_choice`,
the existing clarify picker/buttons supplied by the active Hermes surface.

Normal voice startup does not read a Dograh secret, contact a carrier, start the
phone bridge, or require a configured telephone number. The package carries its
bounded phone dependencies so enabling the feature does not require a second plugin.

Explicit import reads only the current profile's separate Dograh configuration,
verifies its account/workflow/caller, refuses active or unresolved calls, copies
the journal with SQLite backup, cancels old approvals and revokes old bridge grants.
The owner switches the active plugin with `hermes plugins disable hermes-dograh`
and `hermes plugins enable hermes-talk`. The original journal remains available.

Tests cover disabled tool visibility, schema/handler/voice authority classification,
native owner choices, cancellation, single-use dialing, session isolation, packaging
and the existing phone safety cases. The main Talk regression suite remains required.

No custom Phone page, browser transport, chat renderer or mobile application is added.
