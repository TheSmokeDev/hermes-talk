# Provider compatibility receipt

The test suite is offline by design: every realtime session in `tests/` runs
against a scripted transcript, never a socket. That is what keeps CI free of
secrets and flakes — and it means a provider changing its wire under us (a
renamed event, a new close code, a model retired, an auth flow tightened)
reaches us through a user, not a red build. This page is how that user
turns "it stopped working" into evidence we can act on in one pass. A
**pass** is worth filing too: it tells the next person which provider,
model, host, and plugin versions are known to work together right now.

File it through the
[provider compatibility form](https://github.com/TheSmokeDev/hermes-talk/issues/new?template=provider_compatibility_report.yml).
The form's fields are the table below.

## What a receipt is not

No audio. No transcripts. No prompts or task results. No credential, no
session id, no filesystem path. `hermes talk check --json` and
`hermes talk doctor --json` are built to carry none of those, which is why
they are the body of the report — still read what you paste. If you are
unsure whether a value is a secret, leave it out and say so; a report with
a gap is fixable, a leaked key is not.

## Producing it — about two minutes

1. **The mechanical half.**

   ```bash
   hermes talk check --json              # the configured lane
   hermes talk check --json --provider grok   # another live lane, this process only
   ```

   `check` runs the doctor checks, opens a **real** session on the provider
   through the same adapter and credential path the voice uses (connect →
   `SessionReady` → one text turn → `ResponseFinished`), then hands one
   bounded task to a real Hermes agent. Each step reports `pass`, `fail`,
   or `skip` with its duration. The `provider_session` step's details carry
   `auth_source`, `session_ready`, `response_finished`, `audio_bytes`, and
   `transcript_chars` — counts, never content. If the session cannot be
   reached at all, paste `hermes talk doctor --json` instead; it is
   read-only and names the check that refused.

2. **The human half — one short call.** Start the session on the surface
   you use (`hermes talk`, `/talk`, `/talk join`, the dashboard tab) and:

   | Do | Tick if | Which event it proves |
   |---|---|---|
   | Say anything | the reply starts | `SpeechStarted` → `ResponseStarted` → `OutputAudio` |
   | Wait for the reply to finish | it finishes cleanly | `ResponseFinished` |
   | Say "status report" | the reply quotes the version, auth lane, agent lane, or audio state | `FunctionCall` round-trip (the `talk_status` tool ran and its result came back through the model) |
   | Start talking over a reply | playback cuts | barge-in (`CancelResponse` / `TruncateOutput`; on Gemini Live there is no client-side cancel on the wire, so the plugin drops playback locally — tick it if playback stopped) |
   | Hang up (Ctrl+C, `/talk leave`, close the tab) | no error is printed or spoken | `SessionTerminated` |

   Something did not happen? Leave it unticked. The unticked row is the
   finding.

3. **The error, verbatim, if there was one** — the type and the message the
   provider or the plugin printed (a WebSocket close code, an HTTP status,
   a `ProviderFailure` reason). Redact anything that looks like a key or a
   session id.

## The table

| Field | Where it comes from | Example |
|---|---|---|
| Verdict | your call: PASS / PARTIAL / FAIL | `PARTIAL` |
| Provider | `TALK_PROVIDER`, or the doctor's `provider` check | `grok` |
| Model | the doctor's `model` check | `grok-voice-latest` |
| Credential lane | the doctor's `auth` check — the lane, never the credential | `xAI OAuth` |
| Surface | where you ran the call | `Discord voice channel` |
| hermes-talk version | `hermes plugins list` | `0.16.0` |
| Hermes host version | `hermes --version` | `0.21.0` |
| Python / OS | `python --version`, your OS | `3.12.6 / Windows 11` |
| Events observed | the checklist above | `SessionReady, SpeechStarted, ResponseFinished` — no `FunctionCall` |
| `check --json` (or `doctor --json`) | the command's output | the report, pasted whole |
| Provider error, verbatim | the console or the spoken failure | `ProviderFailure: close code 1008 policy violation` |
| Last version it worked on | if you know it | `0.15.0` |

## How maintainers use it

- The `provider` label goes on every receipt; the
  [label search](https://github.com/TheSmokeDev/hermes-talk/issues?q=label%3Aprovider)
  is the running compatibility record — newest receipt per provider/model
  pair is the current word on it.
- **A FAIL or PARTIAL** is reproduced with `hermes talk check --provider
  <lane>` on the same plugin version. The unticked events point at the
  adapter function: `SessionReady` is the setup acknowledgement translation
  in `talk_<provider>_realtime.py`; `SpeechStarted`/`SpeechStopped` are the
  turn-detection events; `FunctionCall` is the tool-call translation and
  `SubmitToolResult` its return path; barge-in is `CancelResponse` /
  `TruncateOutput`. The fix ships with a regression test whose scripted
  server event is the wire shape from the receipt — the report becomes the
  fixture.
- **A PASS on a version pair we had not seen** is acknowledged and closed;
  the issue stays searchable as the receipt. If it is the first pass on a
  new host or model version, the README's provider row or the operating
  manual is updated to say so.
- **A receipt that names an upstream break** (the Hermes host API moved,
  not the provider) is redirected to
  [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/issues)
  with a link back, so the reporter is not left in limbo.

A receipt from the PR author is required for any change on a provider
wire — see the [merge bar](../CONTRIBUTING.md#the-merge-bar). The same
table, filled in, is what the PR template's "Live receipt" section wants.
