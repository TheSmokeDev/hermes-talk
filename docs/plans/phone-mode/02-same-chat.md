# Slice 2: same-conversation call and answer

The call tool captures `ctx.post_message` during the owning Hermes turn. The host
returns a conversation-bound message handle. A bounded background observer reads
only that call's journal and edits only that handle. It does not choose another
chat, impersonate a user turn or start model work.

Hermes owns rendering and delivery. Its local event stream supplies native
Desktop/dashboard/mobile viewers; its messaging adapters preserve the Discord
channel/thread and use ordinary message editing. This avoids a second interface
and avoids sealing the ongoing agent response stream with a phone notification.

Caller questions are labelled untrusted and include the native owner answer command.
Only the authenticated owner in the original conversation can answer. The MCP
bridge returns that exact answer to the verified paired call; tasks keep separate
owner approval and result-release gates.

Proof sequence: install the exact candidate in the existing isolated Hermes
profile; import its test connection; explicitly start the authenticated bridge;
prepare/review one owned-number call; pair its spoken code; observe the question
in the native conversation; enter the test answer there; inspect the returned MCP
answer and actual call transcript/recording. No call is retried after an ambiguous
creation result.

Surface contract tests and real surface checks are recorded separately. A local
phone call does not, by itself, prove a live Discord or physical mobile session.
Keep exact evidence and any remaining surface checks in the local WORK-STATUS.md.
