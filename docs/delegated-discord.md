# Same-chat delegated Discord voice (opt-in)

`/talk delegate join` keeps a duplex voice conversation while Hermes runs work in
that same Discord text conversation. Ordinary `/talk join` keeps its existing
behavior. This opt-in route uses provider-owned Realtime or cascade voice.
`TALK_VOICE_MODE=live` still requires an explicit canonical task and refuses this
route; it does not add GPT-Live delegation support. This route requires the host's public `PluginContext.submit_message`
and `submission_status` APIs and the host plugin's `allow_gateway_injection`
permission. It never silently switches to detached agents or an API session.

Before joining:

1. Send Hermes a text message in the intended server channel to establish a session.
2. Join the bot to the intended Discord voice channel using its existing controls.
3. Set `/voice off` in that text chat. Talk owns spoken replies while this call is
   active; leaving host automatic TTS enabled can produce duplicate audio. Talk
   does not change or restore the host's voice settings on your behalf.
4. Run `/talk delegate join`. The user who invoked it is the only speaker allowed
   to submit work and the only user allowed to control this call from its text chat.

Ask for a task, keep talking while it runs, then ask for its result. `submit_task`
returns admission, not completion; `submission_status` returns the host's exact
settled host-turn result. That turn may start a child that finishes later; it is
not proof that the whole delegated task is complete. Typed background delegation
updates receive a separate, unattributed notice. The voice model does not execute arbitrary host tools directly.
Only attributed speech may request work; ambiguous or unresolved speakers fail
closed. Host permissions still apply independently.

The plugin reads bounded context from the public persistence reader, pinned to the
invoking profile and session. Text updates become context without being treated
as permission. Only a terminal submission receipt triggers its result announcement;
a new assistant message alone is not proof that a particular task finished.
Announcements wait for speech and playback to become idle. Barge-in remains enabled.
There is no new scheduler, resource locking, task cancellation or steering API.

Blocking host receipt calls use a dedicated, bounded worker pool for each call.
They do not occupy the gateway's default workers, which admission storage needs.
Leaving Talk closes the pool without waiting on the gateway loop; an already-started
host call may still commit, so leaving is not cancellation of submitted work.

Limits: reset, compaction or rewritten delivered history requires leaving and
rejoining. Receipts are bounded to 64 per voice call and are not adopted after a
process restart; results remain in Discord. Interrupting an announcement can cut
it short; ask for the result again. The audio transport reuses upstream's existing
private Discord adapter bridge, whose compatibility is separate from the public
submission/persistence APIs. Real microphone, Discord reconnect and provider audio
behavior require live acceptance testing.

Versioned `TALK.md` preferences use the upstream loader and remain additive to this
route's execution contract. An invalid file refuses session startup. The 0.25
spoken workflow feature targets unbound Desktop sessions; it does not replace this
route or grant Discord voice access to private slash-command catalogs. Discord's
permission-checked text-channel picker remains the workflow entry point.
