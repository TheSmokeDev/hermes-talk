---
name: phone
description: Continue this Hermes conversation over an optional phone call.
---

# Hermes Talk Phone

For the optional Dograh carrier connection, enter both Phone secrets through
`hermes plugins config hermes-talk`, then run `hermes talk phone setup`.
The profile owner grants `plugins.entries.hermes-talk.allow_gateway_injection: true`
to allow caller input in the current session. Check the grant with `/phone doctor`.

Phone is another input/output for the original Hermes chat. It uses that session's
history, memory, tools and existing permissions. It does not create a separate
assistant, child session, phone tool allowlist, task approval or result-release step.

Prepare a call with `phone_prepare`, review the exact number and call card in the
native `/phone review CALL_ID` picker, then use `phone_call` once. Carrier and model
charges apply. Never retry an unknown dial result: inspect `phone_status` first.

The approved call connects automatically. There are no spoken pairing codes.
Phone input appears as a normal turn in the same Hermes conversation. Use the
session's usual memory and tools to answer or act, then call `phone_reply` with
the request ID and a concise answer to speak. Any existing Hermes permission
prompts still apply. `/phone answer REQUEST_ID TEXT` also sends an explicit answer.

`/phone doctor` checks the selected carrier connection. `/phone doctor notice`
checks native message delivery without dialing. `/phone doctor input` exercises
the original chat's input queue and history without dialing. These diagnostics
state their individual results; they do not substitute for a real spoken round trip.

The transport runs with `hermes talk phone bridge --port 8787`, over the existing
private relay or authenticated HTTPS connection. Its internal call address routes
the provider to the right session automatically; it is never entered by the owner.
Keep credentials in Hermes's native secret storage, never in chat or Git.

`/phone stop` blocks new dialing and disconnects Phone input. It does not hang up
an existing carrier call; end that call in Dograh. `/phone resume` permits new
reviewed calls without restoring old approvals. After checking an ambiguous call
actually ended, `/phone reconcile CALL_ID confirmed-ended` records that result.
