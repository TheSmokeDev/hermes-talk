# Slice 2: same-session call and answer

Phone is input/output for the original Hermes conversation, with that session's
history, memory, tools and existing permissions. No separate child session, Phone
tool allowlist, manual task approval, result-release step or spoken pairing code.

The outgoing call receives an opaque route in its server-side initial context.
Dograh's supported HTTP body template supplies that route automatically; it is not
a model argument. The transport associates each input with the originating call
and chat. Retried input keys refer to the same journal entry and dispatch once.

The call tool captures the original session's route and context. A worker sends
phone input through public `PluginContext.inject_message(session_key=...)`, using
Hermes's normal session queue. The resulting turn uses the session's normal memory
and tools. `phone_reply` returns its answer to the waiting voice call directly.
Native `post_message` and handle edits provide status in the same conversation.
Desktop/dashboard/mobile and Discord rendering remain owned by Hermes.

Verify transport retries, automatic routing, same-session input, direct answer
return, and the absence of a second permission policy. Then install the exact
candidate, run the native no-dial history check, and exercise the owned-number
call. Retain both the Hermes transcript and the provider's spoken response.
A local call does not prove a live Discord or physical mobile test; record those
surface checks separately in the existing local WORK-STATUS.md.
