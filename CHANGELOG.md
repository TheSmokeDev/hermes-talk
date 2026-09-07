# Changelog

All notable changes to hermes-talk, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). Each release opens with what it means before
listing what it contains.

One honesty note, because this file exists to be trusted: the dashboard tab
and the api-server agent lane landed inside the 0.4.0 development window,
but 0.4.0's release title named only the steering verb. They are recorded
below under 0.4.0 — the first version that shipped them — with the gap
named rather than smoothed.

## [Unreleased]

### Added
- `TALK_CASCADE_SPEED` — delivery pace for the cascade voice, `0.7`–`1.2`
  (`1.0` is normal), resolved at call time so an operator's edit lands on
  the next session rather than the next process. Fail-closed like the rest
  of the cascade knobs: junk and out-of-range values refuse with the range
  rather than clamping, because a silently clamped pace is audible on every
  word and never says why. Unset sends no `speed` field at all, so a cascade
  that does not use the knob puts exactly the same bytes on the wire as
  before it existed. `CascadeVoice` grew a `voice_settings` argument to
  carry it; omitting it reproduces the previous BOS frame exactly.
### Changed
- `starlette` is now a **dev/test** dependency. It is where
  `StreamingResponse` and `ClientDisconnect` actually live (fastapi
  re-exports them), and the relay deadlock fixed below is starlette
  behavior — untestable in CI without the real class, which is exactly how the
  deadlock shipped. `dashboard/plugin_api.py` still falls back to its own
  stub when starlette is absent, so the plugin installs with no web
  dependency of its own.

### Fixed
- Room-scoped spoken approvals now return the approval event's exact
  `request_id` under the field name required by the Hermes run API. They no
  longer fail with HTTP 400 `approval_request_required` while a pending action
  waits for an answer.
- The dashboard cascade relay no longer deadlocks on the servers Hermes
  actually runs on. `POST /api/plugins/hermes-talk/cascade-tts` returned
  HTTP 200 and then zero bytes of PCM, followed by a `ClientDisconnect` in
  the log once the browser gave up — so the dashboard's cloned voice
  silently degraded to text-only with no error anywhere.

  Starlette's `StreamingResponse` only trusts `send()` for disconnect
  signalling when the server advertises ASGI `spec_version` 2.4 or above.
  Below that it races the body generator against a disconnect listener, and
  that listener calls `receive()` first — taking the browser's
  `http.request` messages off the channel. The relay's own
  `request.stream()` then only ever saw `http.disconnect`, so it waited
  forever for text that had already arrived. **Uvicorn advertises 2.3 for
  HTTP**, hardcoded in both `h11_impl.py` and `httptools_impl.py`, so this
  was the branch every dashboard took; only its websocket protocols say
  2.4, which is why the websocket lanes never showed it.

  The relay now returns a `RelayResponse` that owns the request body
  channel and takes Starlette's own `>= 2.4` path verbatim. A browser that
  vanishes mid-upload raises `ClientDisconnect` into the feeder, which is
  now handled as an abort — the same path as a stream that ends without its
  `done` line — rather than escaping as an error.

  The existing tests could not have caught this: they drive the route with
  a fake request and never touch an ASGI server, and Starlette's own
  `TestClient` omits `spec_version` from the scope, so a test written
  through it takes the same broken branch and HANGS instead of failing. The
  new coverage drives the real relay body over a real single-consumer ASGI
  channel and asserts on the scope VALUE rather than on a version string,
  so a future uvicorn that advertises 2.4 leaves it meaningful.

- The dashboard's cloned voice no longer ticks at every chunk seam. The
  page opened its AudioContext at the browser default (48kHz on Windows)
  and then handed it 24kHz buffers, and Web Audio resamples each
  `AudioBuffer` independently — two chunks resampled in isolation do not
  line up where they meet, so every chunk boundary was a discontinuity
  while the PCM leaving the server was provably clean. Measured on a 440 Hz
  tone split into 40 uneven chunks: the per-chunk path jumped 0.105 between
  adjacent output samples where a smooth signal steps 0.035, and a 3x step
  at a seam is the tick you hear. The context is now requested at the PCM's
  own 24kHz so nothing is resampled at all; browsers that refuse the rate
  get a resampler that carries the previous chunk's last sample and its
  fractional read position across chunks, which measures identical to
  resampling the whole tone at once. Barge-in clears that carry-over along
  with the generation bump, so an interrupted sentence cannot splice itself
  onto the next answer.
- The dashboard cascade no longer goes silently mute on HTTP/1.1. The relay
  posted its request body as a `ReadableStream` with `duplex: "half"`, and
  Chrome only sends a streaming request body over HTTP/2 or HTTP/3 — on a
  plain HTTP/1.1 origin, which a local dashboard almost always is, the
  fetch rejects outright. The `.catch(() => {})` swallowed it by design, so
  the model's text captioned normally, no audio ever played, no error
  appeared anywhere, and zero requests reached `/cascade-tts`. The page now
  checks the protocol it actually negotiated
  (`performance.getEntriesByType("navigation")[0].nextHopProtocol`) and
  posts the whole answer once its text is done when it cannot stream. That
  costs sentence pipelining — the first sentence no longer plays while the
  model writes the second — but it produces audio instead of silence.
- A cascade relay that fails for a real reason now says so once, through
  the tab's own error surface and the console, instead of vanishing into
  the same `.catch()`. "Silently" was half of the bug above: a mute voice
  and a working one were indistinguishable. A deliberate abort (barge-in,
  hang-up) stays quiet, because that is not a failure.
- The cascade voice no longer forces a generation per chunk, which is what
  flattened its prosody. Every text frame carried
  `try_trigger_generation: true`, defeating the buffer whose entire purpose
  is giving ElevenLabs enough context to carry intonation across a sentence
  boundary — their own reference calls forcing generation on small amounts
  of text "lower quality audio" and recommends leaving the flag at `false`.
  It compounded with the chunker: `SentenceChunker` ends a chunk on an
  ellipsis run, so a line written with "..." pacing markers became a
  separate forced generation per marker, and a multi-sentence answer read as
  a series of unrelated fragments. Chunks now go out as bare text and
  nothing replaces the trigger: the model buffers on its own
  `chunk_length_schedule`, and the empty-text EOS frame the turn already
  ended on closes the socket — which ElevenLabs documents as forcing
  generation of whatever is still buffered. The cascade opens and closes one
  socket per RESPONSE, so that close happens on every turn regardless.
  Latency where it is audible is therefore unchanged. All three cascade
  surfaces — terminal, Discord, dashboard relay — change together.

  Deliberately NOT added: an explicit `flush: true` on the EOS frame. Their
  flush example attaches the flag to a frame carrying real text, `text` is a
  required field on `SendText`, and an empty-text frame is `CloseConnection`
  — a different message in the same schema. A combined
  `{"text": "", "flush": true}` appears nowhere in their documentation, and
  on this endpoint there is no schema-legal way to force generation without
  either real text or the close. Their sibling `multi-stream-input` endpoint
  does define a purpose-built `FlushContextClient` frame with optional
  `text`, which is what a persistent-socket design would need; this lane
  does not, because its socket is per-turn.
- The cascade socket now states `enable_ssml_parsing` instead of relying on
  a default ElevenLabs does not document. Every neighbouring query parameter
  declares a default in their schema and this one declares none, so a
  `<break time="0.4s" />` was as likely to be dropped as spoken, with
  nothing in the response saying which. Break tags are supported on this
  lane's model and cap at 3 seconds.

## [0.17.0] — 2026-09-03

Prove it, then pause it. `hermes talk check` runs the whole path — doctor,
one live provider turn, one bounded Hermes run that has to echo a token —
so a green report means the lane works right now, not that the config
parses. `hermes talk diagnostics --bundle` turns a bug report into one
redacted file. The model can pause the microphone without hanging up, two
delegated jobs that touch the same thing can no longer race each other, and
an announcement waits for the speaker to finish instead of talking over the
answer you are still hearing. First release with the Hermes core realtime
contract adapter, a security policy, and a contributing guide.

### Added
- `pause_voice_input` — the model can pause listening without ending the
  call (#100). "Stop listening" or "mute the mic" is a tool call: the
  session stays connected, playback keeps playing, background work keeps
  running and its results are still announced; only the operator's speech
  stops reaching the provider. The flag lives on the capture surface —
  `DuplexAudio` and `DiscordAudio` grew `pause_input` / `resume_input` /
  `input_paused`, the same one-interface pattern as `playback_pending` in
  #87 — so both rooms honour it identically: blocks captured while paused
  are dropped (never queued stale), the already-queued ones are discarded,
  and the Discord bridge keeps the host's buffers drained and its inactivity
  timer armed so the bot is not evicted from the channel. A paused
  microphone cannot hear the word "resume", so the way back is the
  operator's own control — Enter in the standalone `hermes talk` terminal
  (toggle; `p`/`r` explicit — a polling watcher, never a blocking stdin
  read), `/talk pause` / `/talk resume` in Discord (`/talk status` says when
  it is paused) — and the tool is offered ONLY where that control is
  guaranteed: the pause decision is made once, before the tool list is
  built, from the same predicate that starts the keyboard watcher, and the
  registered control is what the receipt names. No control, no pause: a
  piped or non-tty stdin gets no key and no tool; `/talk` at the Hermes
  prompt shares its tty with prompt_toolkit, so that lane never watches
  stdin and offers no pause; and a pause call that arrives anyway is refused
  (`no_resume_path`) rather than armed, because a pause nobody can undo
  would be a hang-up. On Windows an extended key (arrows, Insert, F-keys) is
  consumed whole — before, Down-Arrow's scan code read as `p` and paused the
  microphone. Both directions get a spoken receipt: the model's tool result
  for its own flips, a contained announcement for the operator's. The tool
  classifies read-only (it can only narrow what a session does, and a pause
  is never a path to authority) and refuses — never arms — when no session
  is attached. Ported idea from bielcarpi/hermes-live-voice (MIT), idea
  only.
- Run admission control on `delegate_task` (#101). The model may
  declare `execution_mode` (`exclusive`, the default, or
  `parallel_read_only`) and up to eight normalized `resource_keys` naming
  what a task touches — a repo checkout, a deployment target. Two live runs
  that share a key never overlap unless both are read-only; the check runs
  before a run id is minted or an acceptance record is written, so a refused
  job burns nothing and can never surface as `lost`. The refusal is a spoken
  tool result naming the run in the way and the shared key, never a hang or
  a silent queue, and `check_work` reads out what each running job holds.
  New knob `TALK_TRUST_DECLARED_READ_ONLY`, default off: until the operator
  sets it, `parallel_read_only` is downgraded to `exclusive` and recorded
  that way, because the declaration is the delegating model's own claim, not
  a sandbox. A task that names no keys is exactly the task Talk always ran.
- `hermes talk check` proves the whole voice path end to end, right now
  (#97). Doctor is read-only by design, so a green doctor could
  still hide a dead mint, a refused socket, or a delegation lane that never
  starts. The check runs the doctor checks, then a REAL session on the
  configured provider through the same adapter and credential resolution the
  voice uses (connect, `SessionReady`, one text turn, `ResponseFinished`),
  then ONE bounded Hermes run through the same delegation path the voice
  uses whose output must contain `HERMES_TALK_CHECK_OK`. `--json` reports
  every step as pass/fail/skip with its duration and the exit code is 0 only
  when every non-skipped step passed; `--no-run` skips the agent step;
  `--timeout` budgets it (a run that outlives its budget is stopped, not
  abandoned). The report never carries tokens or paths. A mock cannot go
  green: `--provider` accepts only live lanes, the report's provider is
  validated against the same fail-closed list `TALK_PROVIDER` uses, and the
  live steps refuse under the test harness unless a test opts in by name.
  Ported idea from bielcarpi/hermes-live-voice's `launch-check` (MIT) — idea
  only, no code. Session credential/model/voice resolution moved into one
  `talk_cli.resolve_provider_lane()` so the check proves the session's real
  path rather than a copy of it.
- `hermes talk diagnostics` writes a redacted support bundle for issue
  reports (#98). Most "it doesn't work" reports could not be
  reproduced from what the reporter pasted. The bundle carries versions
  (Python, hermes-talk, the Hermes host, the OS), the NAMES of the `TALK_*` /
  `HERMES_*` variables that are set plus a fixed list of shared ones
  (presence only — values are never read), audio device counts and default
  device names, host capability facts, and every doctor check's outcome with
  an allowlisted subset of its details. No logs, prompts, transcripts, task
  results, audio, secret values, or paths. The serializer is default-deny:
  `BUNDLE_ALLOWLIST` names every key and the shape its value must have,
  identifier-shaped leaves are dropped outright if secret redaction would
  change them, free-text leaves pass redaction plus a path scrub and a
  length cap, and anything not on the list is dropped — a key doctor grows
  later cannot leak by inheritance (a test plants secret-shaped values at
  every level and proves none reach the file). `--bundle [PATH]` writes it
  owner-only (POSIX `0600`; the setup wizard's protected owner-only DACL on
  Windows, applied to the empty temp before any bytes land) and verifies the
  permissions after the move, deleting the file if they cannot be proven;
  `--json` prints it instead. The bug-report issue template now asks for the
  bundle first. Ported idea from bielcarpi/hermes-live-voice's `diagnostics`
  (MIT) — idea only, no code.
- hermes-talk's three realtime lanes now register on the Hermes core
  `RealtimeVoiceProvider` contract (`agent/realtime_voice_provider.py`, API v2 —
  NousResearch/hermes-agent#101808) as `hermes-talk/openai`,
  `hermes-talk/grok`, and `hermes-talk/gemini`, so `hermes realtime --provider
  <name>` drives them through core's own orchestrator. Registration is
  feature-detected on both sides: a host without the hook, or with a different
  contract version, loads hermes-talk exactly as before — one debug line, no
  warning, every other surface intact. Capabilities are declared per lane
  rather than assumed, so core degrades explicitly instead of calling an
  operation the wire cannot perform. Availability stays offline and read-only
  on all three lanes: no socket, no token refresh, no auth-store write.
- The neutral session contract gained `ToolCallsCancelled`, and the Gemini Live
  adapter emits it for `toolCallCancellation`. The server can discard a
  pending tool call mid-turn; until now that was recorded silently and only
  visible as a dropped result on the send path, which told policy nothing until
  it had already produced work nobody wanted.
- The contributor experience, written down. `CONTRIBUTING.md` now
  ranks what we take first (bug fixes on live lanes, then provider and host
  compatibility, security hardening, cross-platform, new providers behind
  the contract, new surfaces, docs), maps the common paths — a new realtime
  provider, a new surface, a new talk tool, a fix, a docs change — to the
  exact files each one touches and ships with, and states the merge bar
  (live-verified before merge on a provider wire; offline tests and fake
  sessions otherwise; no auth-store writes; tokens only to the provider's
  own host; nothing secret in logs or receipts), the branch and
  Conventional-Commit scope conventions derived from the history, a
  first-response-within-24-hours review promise, and how merged work is
  credited. A pull request template carries the same contract (what and
  why, how to test, platforms, a `check --json` receipt when a lane is
  touched, `Fixes #N`). Two new issue forms — a feature request that asks
  for the problem before the proposal and which surfaces and providers it
  reaches, and a **provider compatibility report** that collects a
  provider's PASS/PARTIAL/FAIL as a fixed table (provider, model,
  credential lane, versions, which of `SessionReady` / `SpeechStarted` /
  `FunctionCall` round-trip / barge-in were observed, the `check --json`
  report, the wire error verbatim) with no audio, transcripts, or secrets;
  `docs/PROVIDER-RECEIPT.md` is the how-to behind it and says how
  maintainers act on each verdict. The bug form gained the core-contract
  lane and the check/doctor/diagnostics commands as places a bug can
  happen, and the issue chooser now links private security reporting and
  Discussions. Labels `provider`, `surface`, `security`, and `docs` join
  `good first issue` / `help wanted`. The README credits the people who
  showed up: @kvnloo, @TheAngryPit, @webdevtodayjason. Ported idea from
  bielcarpi/hermes-live-voice's provider-compatibility receipt (MIT) — idea
  only, no text.

### Changed
- README leads with what works today (#96). The first screen is now:
  what it is, the three surfaces and three providers as facts, a one-line
  install, the demo GIF with the with-sound cut on its own line, a full badge
  row (CI, CodeQL, Scorecard, PyPI version and downloads, license), and an
  "Is it working?" block that puts `hermes talk doctor` before any narrative.
  New Surfaces and Providers tables, a "Current boundaries" section that
  states the no-self-hosted-lane limit and the other honest gaps ourselves,
  and a "Where this sits upstream" section for RFC #77111, core PR #101808,
  and docs PR #97325. The stale "650+ offline tests" receipt is now 1,400+
  across 46 files, and the Status block no longer hardcodes a stale version —
  the PyPI badge carries it.
  Every existing section survives — reorganized, none deleted. CONTRIBUTING
  gains the `uv sync --extra dev` path, the `ruff==0.16.5` pin's reason, the
  #93 twelve-test baseline on a box where Hermes is importable, and the
  module table now lists every shipped module.
- The software echo gate (mic blocks below the playback echo floor are
  dropped while model audio plays) now runs on every platform whenever
  PulseAudio AEC is not active — that is always on Windows and macOS.
  On headphones there is no echo to suppress, so it is tunable:
  `TALK_ECHO_GATE=off` disables it; `TALK_ECHO_GATE_OUTPUT_ACTIVE_LEVEL`,
  `TALK_ECHO_GATE_MIN_BARGE_IN_LEVEL`, and `TALK_ECHO_GATE_RATIO` retune it.
  Read when the audio stream is constructed. All four are now in
  `docs/OPERATING.md`'s Audio table.

### Fixed
- `talk doctor` no longer reports a working Grok lane as unconfigured. The
  read-only parse of the host store knew one of the two shapes a Hermes
  `xai-oauth` login lives in — a `providers` block with the tokens nested
  under `tokens` — and a current host writes a device-code login into
  `credential_pool` instead, as a list whose rows carry the tokens FLAT. So
  every operator who logged in on a current Hermes was told
  `no usable Grok authentication lane was found` while the lane resolved and
  connected fine; only the read-only diagnostic was blind, never the call.
  The parse now mirrors the host's own resolver
  (`hermes_cli.auth._xai_oauth_state_from_store`) end to end: `providers`
  first, then the pool in stored order, both tokens required on either — the
  same pair check that rejects a quarantined login, since the host
  quarantines by popping the tokens. A non-list pool slice still yields
  nothing, because the host would not read one either. The receipt gained
  `xai_oauth_source`, and `talk doctor` now prints
  `xai-oauth=valid (via credential_pool)`, so the next person to debug this
  can tell an empty store from an unread one.
- Linux terminal calls now route default audio through PulseAudio's WebRTC
  echo canceller and noise suppressor. Echo-cancelled input bypasses the
  fallback amplitude/VAD gate so barge-in does not clip quiet words.
  ([#81](https://github.com/TheSmokeDev/hermes-talk/pull/81), thanks
  [@kvnloo](https://github.com/kvnloo) — the first outside contribution to a
  live lane.)
- Proactive announcements now wait for the SPEAKER to drain, not just for the
  server's `response.done`. The model streams far faster than
  realtime, so the terminal event can arrive with a second of the previous
  answer still queued locally — and the announcement started on top of it,
  overlapping two responses at the only surface the operator actually has.
  `DuplexAudio` and the Discord bridge gained a non-destructive
  `playback_pending`; the announcement gate now consults it both in the
  pump's poll and in the re-check inside the send lock, so the wire and the
  room are decided together. A deferred announcement is delayed, never
  dropped.
- An announcement deferred for longer than `ANNOUNCE_STARVATION_WARN_S`
  (30s, 0 disables) now tells the operator once. Deferring is
  correct, but a gate that never opens was previously a silent slow poll
  with nothing to see.
- A Discord `talk join` that refuses before going live now says WHAT
  refused instead of "session exited unsuccessfully". The session
  already knew — it printed the reason to the gateway's stderr and returned
  a bare exit code — so the operator, the one person who could act on it,
  was the only one who never saw it. Configuration and provider-connect
  refusals also point at `/talk core join`, which resolves its provider
  through the host and so routes around exactly those two. An audio refusal
  does not: core voice opens the same channel and would fail the same way.
- A tool-setup failure on the legacy Discord lane raised
  `AttributeError` out of the session instead of refusing. That lane
  has no host execution attachment, and the handler closed one
  unconditionally, so the crash — not the tool problem — was what reached
  the operator.
- CI lints against a pinned ruff. The dev extra asked for
  `ruff>=0.4` and CI installed whatever was current, so ruff 0.16 arrived on
  its own and failed the build on `RUF100`: it stopped reporting `BLE001`
  where the handler logs the exception through a name it treats as a logger,
  which retired three `noqa: BLE001` directives in `talk_core_provider.py` —
  the only module that both names its logger `logger` (the other fourteen
  use `_log`, which ruff does not recognise) and carries those directives.
  The trap was that the two versions wanted opposite source: deleting the
  directives fixed CI and broke every dev box still on 0.15.x. `ruff==0.16.5`
  is now pinned in the dev extra (which is what CI installs), the three
  retired directives are gone with their reasons kept as plain comments, and
  the `[tool.ruff]` comment no longer claims CI runs whatever ruff is
  current.
- The plugin scans `safe` again. A literal U+FEFF typed into
  `tests/test_grok_auth.py` — the BOM fixture for the BOM-prefixed auth-store
  test, added with the Grok subscription lane in `01518ae` — tripped the
  upstream `plugin_guard` scanner's `invisible_unicode` check as a HIGH
  finding, taking the whole repo to a BLOCKED verdict. That scanner gates
  `hermes plugins install`, so the block reached users, not just CI. The BOM
  is now written as the escape `"\ufeff"`: identical bytes on disk, pinned by
  the same `startswith(b"ï»¿")` assertion the test already made, and
  no invisible character left in the source for a reader or a scanner to have
  to distinguish from an accidental one.
- The README demo GIF is re-rendered from the original 1280x582 screen
  recording instead of the 640x291 downscale it shipped as, so the
  transcript and the agent's brief in the runs panel are readable rather
  than grey mush (#79). `docs/render-dashboard-gif.py` regenerates it
  from the published release asset.

### Security
- A visible trust surface on the repository (#95). Three new
  workflows: CodeQL over the Python and over the workflow files themselves
  (PR, push, weekly), OpenSSF Scorecard (push, weekly, results published so
  the badge and the public viewer render), and dependency review on every PR
  (fails on a moderate-or-worse advisory or a license outside the permissive
  allowlist). Releases now carry provenance twice: `publish.yml` attests the
  built dist with `actions/attest-build-provenance` before uploading, and the
  PyPI upload passes `attestations: true` (PEP 740). Every third-party action
  in every workflow is pinned to a full commit SHA with its version alongside
  — the standard `plugin-guard.yml` already set for the upstream scanner —
  which also moves `checkout` and `setup-python` from their floating `v4`/`v5`
  majors to current releases. The README header gained CodeQL, Scorecard, and
  PyPI badges.
- `SECURITY.md`: supported versions (the latest PyPI release and
  `main`), private reporting through GitHub security advisories (private
  vulnerability reporting is enabled on the repository), a 72-hour
  acknowledgement target, and what counts — credential leakage, auth-store
  writes outside the documented refresh, tool-authority bypass, redaction
  failures, supply chain.
- `.github/dependabot.yml`: weekly `pip` and `github-actions`
  updates, minor and patch bumps grouped into one PR per ecosystem, majors on
  their own.
- The `ci`, `plugin-guard`, and `CodeQL` workflows run on a least-privilege
  `GITHUB_TOKEN`: `contents: read` at the workflow level (they
  inherited the repository default, which can be read/write), with the one
  write CodeQL needs — `security-events: write` to upload its SARIF —
  granted on the analyze job alone. Closes CodeQL alerts #20/#21 and
  Scorecard's Token-Permissions findings.
- The dashboard `/status` route no longer echoes a configuration error's
  text into its response (CodeQL `py/stack-trace-exposure`). An
  unusable `TALK_VOICE` or `TALK_VOICE_MODE` still keeps the tile
  answerable, but the response now names only WHICH setting refused plus a
  short reference; the exception text — which quotes the offending value —
  goes to the dashboard's own log under the same reference. The mint keeps
  repeating the exact remediation on its own refusal path, where the caller
  is the operator pressing Start.
- A voice session whose transport declares remote speakers
  (`discord_speaker_authorization`) but carries no authorization ledger is now
  REFUSED at session setup, before a secret is minted or a socket is
  opened. That equivalence held by construction and was asserted only in a
  comment several hundred lines from where it is relied on — and the branch
  relying on it silently selects the allow-all `local_operator_authorizer`,
  so a construction bug would have handed every speaker in a voice channel
  full operator authority with nothing anywhere refusing. It refuses through
  the same bounded-reason sink every other startup refusal uses, so a Discord
  `talk join` says what happened instead of "session exited unsuccessfully" —
  and that sentence does not offer `/talk core join`, which would take the
  same channel with the same speakers and refuse identically.
- An unnamed tool call is now identified the same way on every path.
  The two authorizer call sites disagreed (`"tool"` when revoking a permit,
  `""` when authorizing one), so the identity a nameless event was revoked
  under was not the identity it would have been authorized under. One helper
  answers for both, and it returns `""` — which cannot collide with a
  registered tool, a classification set, or a permit's recorded action.

## [0.16.0] — 2026-09-01

Grok voice on an X subscription. `hermes auth add xai-oauth` once,
`TALK_PROVIDER=grok`, no API key — the last provider that still forced a
metered key now rides the host login the way the OpenAI lane rides the
Codex CLI's. Verified live on **X Premium** (the $8 tier) on 2026-09-03:
`POST /v1/realtime/client_secrets` → 200, the realtime socket → 101,
`session.created`. A tier without realtime access still gets the honest
403 line instead of a traceback.

### Added
- **`xai-oauth` auth lane for Grok** (`talk_grok_auth.py`). Resolved
  fail-closed: `TALK_PREFER_XAI_OAUTH` → `TALK_XAI_API_KEY` →
  `XAI_API_KEY` → the host's `xai-oauth` login. The host resolver owns
  refresh and quarantine when importable; otherwise `HERMES_HOME/auth.json`
  is parsed read-only. Talk never writes an auth store and the bearer only
  ever reaches `*.x.ai`.
- **`TALK_PREFER_XAI_OAUTH`** — `true` requires the subscription login and
  refuses metered fallback; blank or invalid values refuse, like the Codex
  twin.
- **`hermes talk doctor --probe`** (grok only, opt-in) — two live calls to
  `api.x.ai` (`POST /v1/realtime/client_secrets` + the realtime handshake)
  that print status codes and the first server event, never the token.
- **Handshake remediation.** A 401/403 on the Grok socket becomes one
  operator line (`run hermes auth add xai-oauth` / `your xAI subscription
  tier does not include realtime API access; set XAI_API_KEY`) instead of
  an aiohttp traceback. Every other failure keeps its original text.

### Changed
- `hermes talk doctor` on the Grok lane reports the winning auth lane and
  `xai-oauth=valid|expired|invalid|missing` without refreshing anything;
  "no xAI key" alone no longer fails when a usable login exists.
- `hermes talk setup` offers the xAI subscription vs an xAI key for
  `TALK_PROVIDER=grok`; without a login it names the command and writes
  nothing.

### Not in this release
- Reading Grok Build CLI's `~/.grok/auth.json`; a device-code login inside
  the plugin; any write to any auth store. The dashboard tab stays
  OpenAI-only.

## [0.15.1] — 2026-09-01

Voice hears you again on end-to-end-encrypted Discord calls. 0.15.0's
`/talk join` could forward white noise to the model for the whole session
when the operator was already in the channel and had not spoken through
`/voice` first — not a microphone problem, an identity one.

### Fixed
- **E2EE audio from an unmapped speaker no longer reaches the model as
  static.** Discord voice is DAVE-encrypted; the host only decrypts an SSRC
  it has already mapped to a user, and it learns that mapping from Discord's
  SPEAKING event (which never arrives for someone already transmitting when
  the bot joins) or from its own silence gate — which `/talk`'s continuous
  drain starved, so the mapping never formed, decrypt was skipped, and Opus
  decoded ciphertext. The bridge now identifies the speaker itself the way
  the host's silence gate would (host inference preferred, sole-allowed-
  member fallback), discards the frames decoded before the mapping existed,
  resets the host decoder for that stream, and warns once per SSRC when a
  speaker cannot be identified rather than forwarding noise. Unencrypted
  (passthrough) audio flows exactly as before.
- **Capability section reflects the live install.** The prompt's toolset
  list is priority-ordered so high-agency tools (`computer_use`, browser,
  terminal) survive the display cap instead of being truncated in catalog
  order; the delegate line names only the categories whose tools actually
  resolved; a category with no tool list is kept rather than silently
  filtered out.

### Added
- One `INFO` receipt when the capture tap goes live —
  `discord capture live: bot_ssrc=… e2ee=… mapped_ssrcs=…` — so the next
  "it hears noise" report carries whether the call was encrypted and who
  was mapped at the moment `/talk` attached.

## [0.15.0] — 2026-08-30

The capability bridge: voice becomes the manager of Hermes's whole capability
surface — the session knows the live install, does what's safe directly,
delegates the rest, and gated work resolves by spoken approval. Never a bare
"I can't."

### Added
- **Live-catalog prompt section.** Session instructions carry a bounded
  capabilities block — skill count + the tool categories usable right now +
  the delegation ceiling + the never-invent-tool-names rule — assembled from
  the real catalog and capped like every other resident section. When the
  catalog is unreachable the section is absent and the prompt is exactly what
  it was before (fail-open). The tier-1 catalog probe stops dispatching the
  guessed `list_capabilities` tool name (dead upstream) and reads the host's
  own registries instead — the same builders the `/v1/skills` and
  `/v1/toolsets` routes run, plus the live, availability-gated resolved-tool
  set from `model_tools.get_tool_definitions`.
- **Host-tool classification table** (`talk_operator_auth`): curated read-only
  host tools (`web_search`, `web_extract`, `vision_analyze`, `session_search`)
  may run inline; `computer_use`'s read actions ride a fresh spoken operator
  permit; everything else — including every destructive computer-use action —
  delegates. Unclassified names and permit-gated failures now deny with a
  steering receipt ("I can't do that directly in a voice call — I can spin up
  an agent that can. Want me to?") instead of a flat refusal.
- **Spoken approvals for delegated runs.** Every api-server run gains an SSE
  sidecar on `/v1/runs/{id}/events`; an `approval.request` becomes a spoken,
  contained prompt, and the operator's answer resolves it through the new
  `resolve_approval` tool (on Discord, behind the existing spoken-permit
  machinery). Voice grants `once`, `session`, or `deny` — `always` is
  ungrantable, narrowed in code. An unanswered question denies on
  `TALK_APPROVAL_PROMPT_TIMEOUT_S` (default 60s); interrupting the question
  denies immediately. Progress narration and the result ride the existing
  watcher machinery unchanged; resolutions annotate run meta like stop
  receipts.

### Fixed
- **Transcript flush no longer drops the conversation when no Talk connection
  is bound.** The session-end memory handoff routed through the ticketed run
  lane and was refused ("no Talk connection is bound") on the Discord lane,
  deleting the transcript unread. The flush now runs a ticket-free lane
  ladder, and when no agent lane exists at all the transcript is restored for
  the next sweep ("handoff deferred") instead of dropped.

### Hardened (adversarial review round)
Eight findings from the pre-release adversarial review, all fixed with
regression tests:
- **Classification is transport-independent.** The host-tool classification
  now rides the execution relay itself, above every authorizer — the local
  single-speaker lane can no longer dispatch a destructive or unclassified
  host tool bare (its in-handler approval gates fail open on the plugin
  thread). *Behavior change:* local voice host-execution steers mutating host
  tools to the delegate lane with spoken approvals instead of running them
  ungated.
- **An answer in flight owns its approval.** A resolve POST outlasting the
  courtesy wait can no longer be followed by a timeout deny or a second
  answer; a late acceptance finalizes the record, and a transport failure
  reopens it and re-arms the fail-closed timer.
- **Malformed approval metadata narrows to deny-only.** A missing or
  unrecognizable `choices` list used to widen the answer set to everything
  voice can grant; it now collapses to `deny`.
- **Dead event streams still get spoken prompts.** The run-events stream is
  single-shot upstream (a reconnect 404s); when a run's watcher dies, the
  poll loop reconciles one conservative prompt (`once`/`deny`) instead of
  letting the approval sit silent until the host's 300s auto-deny. Resolves
  also carry the request's own id for exact routing; this release sent it as
  `approvalId`, while current Hermes requires `request_id` (corrected under
  [Unreleased](#unreleased)).
- **Stale sidecars are quarantined by attach generation.** A delegated run
  outliving its session can no longer speak its approval into — or be
  resolved from — the next session.
- **Transcripts are deleted only on proof.** The flush tiers run
  synchronously and return a completion receipt; a refusal, failed run,
  nonzero one-shot exit, or exception keeps the only copy for the next sweep
  (at-least-once instead of silent loss).
- **A zero-tool live read is a real answer.** The prompt section no longer
  falls back to static enabled/configured flags when the registry's
  availability gates resolved nothing.
- **Cold starts mint the catalog deterministically.** Session start gives the
  background catalog read a bounded head start
  (`TALK_CATALOG_STARTUP_WAIT_S`, default 2.5s, `0` = never wait) instead of
  racing it.

## [0.14.0] — 2026-08-28

The custom-voice cascade leaves the terminal: Discord rooms and the dashboard
tab speak through ElevenLabs too.

### Added
- Discord lane: cascade voice in the voice channel. The lane already enters
  the terminal's shared `run_talk_session`, so the 0.13.0 wiring —
  fail-closed config, text-output session setup, observe-before-relay,
  teardown — applied by construction; this release PROVES it through the
  real `DiscordAudio` surface (fake voice channel, scripted TTS socket):
  cascade PCM24k takes the relay's exact 24k→48k conversion into the room,
  barge-in kills the TTS stream and drains the channel in one step, and a
  non-OpenAI provider refuses before the channel is touched.
- Dashboard lane: `POST /api/plugins/hermes-talk/cascade-tts` — a server-side
  relay for the browser. The tab mints a text-output session (`/session`
  answers `voiceMode: "cascade"` and skips the provider-voice validation),
  streams the model's `response.output_text` deltas to the route as NDJSON
  (`{"delta": ...}` lines, one terminal `{"done": ...}`), and plays the
  PCM24k that streams back through its AudioContext. Only an explicit `done`
  completes an answer: an aborted stream (barge-in, tab closed) cancels the
  TTS instead of flushing it, and a malformed or oversized line cancels with
  one logged receipt rather than half-speaking. The ElevenLabs key never
  leaves the server — the route sits behind the same `TALK_DASHBOARD_TOKEN` /
  loopback gate as the mint, and `CascadeVoice` gains an `on_stream_end`
  hook so the route knows when a response's audio has settled.
- `talk_config.cascade_voice_config(provider)`: the cascade fail-closed
  resolution (provider gate, TTS knob, key, voice id, model) in one place,
  so the terminal, Discord, and dashboard lanes refuse identically.

## [0.13.0] — 2026-08-28

Custom voice: a cascade mode that lets the assistant speak in YOUR voice —
any stock or cloned voice on the operator's ElevenLabs account — while the
realtime provider stays the brain.

### Added
- `TALK_VOICE_MODE` (`native` default | `cascade`, fail-closed). In cascade
  mode the provider session opens in text-output mode; assistant text deltas
  flow through a sentence chunker into a streaming ElevenLabs TTS, and the
  returned PCM24k feeds the SAME playback sink the relay uses for provider
  audio — the playback engine is shared, not forked. Cascade is gated to
  OpenAI (its text-output mode is wired and verified); selecting grok or
  gemini fails closed and names the provider.
- `talk_cascade_voice` module: the sentence chunker (terminal punctuation
  plus clause breaks past a ~120-char budget; decimals, abbreviations,
  initials, acronyms, dotted words, and ellipses never false-split, and a
  split never lands mid-word) and the ElevenLabs stream-input client
  (BOS/voice settings/chunks with `try_trigger_generation`/EOS, base64 audio
  frames, `isFinal` terminal). The key rides the `xi-api-key` header only —
  never the URL, never a log line.
- Barge-in covers the cascade: SpeechStarted aborts the in-flight TTS stream
  and drains pending chunks in the same synchronous step the relay drains
  playback, so a cancelled sentence never speaks; the next response opens a
  fresh stream. A TTS failure degrades that one response to text-only with a
  single logged receipt — the voice session survives.
- Cascade knobs: `TALK_CASCADE_TTS` (`elevenlabs` only, fail-closed),
  `TALK_ELEVENLABS_API_KEY` -> `ELEVENLABS_API_KEY` (set-but-blank refuses),
  `TALK_ELEVENLABS_VOICE_ID` (required in cascade mode, fail-closed with
  remediation), and `TALK_ELEVENLABS_MODEL` (default `eleven_flash_v2_5`).
- Doctor gains a `cascade` check: voice mode, TTS provider, redacted key
  presence, voice-id status, provider gate — read-only, no live probe.

## [0.12.0] — 2026-08-28

A third realtime voice provider: Gemini Live (Google) — the zero-cost lane,
free-tier AI Studio keys included — behind the same provider-neutral session
contract the OpenAI and Grok lanes already speak.

### Added
- `TALK_PROVIDER` gains `gemini` as a third value — call-time resolved,
  fail-closed on any other value, and never inferred from which API keys
  happen to be set.
- `talk_gemini_realtime` adapter: key-in-URL WebSocket to the Gemini Live
  endpoint — on this lane the URL itself is the secret, so it is assembled at
  connect, never logged, and scrubbed out of transport errors. The
  `setup`/`setupComplete` handshake carries model, voice, instructions, and
  function tools (schema types uppercased into the Live enum vocabulary);
  tool `args` arrive as parsed dicts and are translated to the contract's
  JSON strings, with `toolResponse` envelopes keyed by call id — the loop
  round-tripped live on `gemini-3.1-flash-live-preview`. Assistant audio is
  native 24kHz; a pure-Python streaming resampler downsamples the relay's
  24kHz microphone PCM to the 16kHz Live declares for input.
  `serverContent.interrupted` maps to the contract's barge-in path, and
  session-resumption handles are recorded for the follow-up reconnect
  feature (not sent back in v1).
- Gemini knobs: `TALK_GEMINI_API_KEY` -> `GEMINI_API_KEY` (fail-closed;
  set-but-blank is a hard refusal), `TALK_GEMINI_MODEL` (default
  `gemini-3.1-flash-live-preview`), and `TALK_GEMINI_VOICE` (fail-closed and
  case-sensitive: `Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`).
- Gemini's honest degrades, each logged once per session or refused loudly:
  the Live protocol has no client cancel, truncate, or context-delete
  command, so those commands degrade to local playback handling with a
  receipt and a truncation that did not happen is never faked; a standalone
  `StartResponse` maps to a `turnComplete` client-content trigger (the one
  shape the live probe did not exercise); and the Discord lane's
  gated-response flow (`automatic_response=False`) is refused at connect
  rather than silently answering unvetted speakers.
- Doctor's `provider` check covers the Gemini lane with the same read-only
  shape: redacted key presence, model and voice validity, no live probe.
- Gemini setup also enables session resumption (still record-only: only a
  `resumable: true` update confirms a handle, and a `resumable: false`
  update discards the cached one — an invalidated handle is never reused)
  and context-window compression on server sliding-window defaults, so
  audio-only sessions are not cut off near the 15-minute mark.
- Gemini wire hardening against the shipped-provider references (Google Live
  docs, OpenClaw, Pipecat, LiveKit): tool calls the server cancels
  mid-interruption (`toolCallCancellation`) have their results dropped with
  a once-per-call receipt — nothing is answered upstream for a discarded
  call; `goAway` surfaces as a terminal failure the relay can close on
  instead of a dead socket; a bundled `serverContent` frame is processed
  field-by-field before its terminal flag is honored; and trailing
  audio/text arriving after `generationComplete` is dropped with one
  warning per window rather than reopening a phantom response.

### Fixed
- Live smoke: the Gemini endpoint speaks its JSON in BINARY WebSocket frames
  on some connections — both frame types are now accepted, and one malformed
  frame is a non-terminal failure instead of killing the call.

## [0.11.0] — 2026-08-28

A second realtime voice provider: Grok (xAI), behind the same
provider-neutral session contract the OpenAI lane already speaks.

### Added
- `TALK_PROVIDER` selects the realtime provider — `openai` (default) or
  `grok`. Call-time resolved, fail-closed on any other value, and never
  inferred from which API keys happen to be set.
- `talk_grok_realtime` adapter: bearer-authenticated WebSocket to the xAI
  realtime endpoint (no ephemeral mint exists there — the resolved key is the
  socket's credential), GA-vocabulary events translated into the neutral
  contract, application-level `ping` events and normalized `session.updated`
  echoes tolerated without being parsed for authority. The full tool loop
  (function-call arguments to `function_call_output` to follow-up response)
  round-trips with the existing command vocabulary.
- Grok knobs: `TALK_XAI_API_KEY` -> `XAI_API_KEY` (fail-closed; set-but-blank
  is a hard refusal), `TALK_GROK_MODEL` (default `grok-voice-latest`), and
  `TALK_GROK_VOICE` (fail-closed: `ara`, `rex`, `sal`, `eve`, `leo`).
- Terminal and Discord lanes inherit the provider through the shared session
  factory; the dashboard lane is unchanged (xAI has no WebRTC offer endpoint
  — that lane is a Phase 2 backend relay).
- `hermes talk doctor` gains a `provider` check: selection, redacted key
  presence, and model/voice validity for the Grok lane. Read-only, no live
  probe.
- Server-side truncation on Grok is attempted first and, if the server
  refuses the event as unsupported, degrades to cancel-only with one logged
  receipt per session — a truncation that did not happen is never faked.

### Fixed
- Grok user transcripts no longer print duplicated: xAI's cumulative
  input-transcription snapshots decode as non-final partials, identical
  repeats are suppressed, and the completion event yields exactly one final
  per input item (live smoke, 2026-08-28).

## [0.10.1] — 2026-08-27

The voice session now knows what it is and where it lives, and the plugin
installs clean under Hermes's new security scanner.

### Added
- Full Hermes self-knowledge in the voice session (hermes-talk#64). The
  session now carries a lane line naming its own transport — a CLI session
  says it is a terminal on the operator's machine and that Ctrl+C hangs up;
  Discord and dashboard sessions name their own off switches — so "where are
  you running from?" and "how do I turn you off?" get true answers. The
  preamble steers "what can you do?" to the live `talk_capabilities` catalog
  instead of a recitation from memory, and states the delegation ceiling
  plainly: no direct clicking or typing, but delegated agents run the full
  Hermes toolset including computer use — never "I can't" when the honest
  answer is "I can hand that to an agent." A one-line host summary (enabled
  skill/toolset counts) rides session mint when the catalog is already warm,
  and stays absent rather than stalling startup when it is not.

### Fixed
- The plugin now scans `safe` under the upstream `plugin_guard` security
  scanner (NousResearch/hermes-agent, gating `hermes plugins install` since
  Hermes v0.20.4), where one critical finding blocks installation and
  `--force` does not override. The repo was carrying 17 criticals, all of
  them false positives from the test suite doing its job: the redaction and
  containment tests quote injection text, destructive commands, and
  credential-shaped dummies byte-for-byte to prove those protections hold
  against the real thing. Those payloads now live in `tests/fixtures/` as
  `.fixture` files — an extension the scanner does not content-scan — loaded
  through `tests/fixture_data.py` with their bytes and every assertion
  unchanged. Two phrasings that collided with scanner patterns were reworded
  without changing meaning (an auth-source comment in `talk_auth.py`, one
  `HERMES_HOME` row in `docs/OPERATING.md`). A new gate keeps it green:
  `.github/workflows/plugin-guard.yml` downloads the scanner pinned to the
  upstream main commit it resolves at run time and fails the pull request on
  any critical or high finding, and `tests/test_plugin_guard.py` reproduces
  the same check offline when the scanner is vendored locally.

## [0.10.0] — 2026-08-27

Delegated work stops being a black box. A voice session now starts knowing
who you are, approves a mutation in one exchange instead of three, hears
bounded progress while the job runs, and gets the right result back in the
right session — even across a reconnect.

### Added
- Background work now speaks bounded progress milestones between the
  delegation receipt and the terminal result (hermes-talk#33). A live session
  hears "accepted", "executing — Reading files", "blocked" (waiting on an
  approval), and periodic "still working" heartbeats — all built from host
  evidence only, never invented. The only job-specific detail that can leave
  the module is a safe tool label from a fixed mapping table ("Reading files",
  "Running commands", "Searching the web"); unknown tools degrade to
  "Working". Arguments, paths, URLs, output text, and approval commands never
  enter a milestone.

  Three invariants hold the design together: claims never exceed host
  evidence (a phase is set only from a real host signal — the api_server's
  `last_event`, or an in-process `post_tool_call`/`pre_approval_request`
  hook); telemetry is never authority (writing `complete` into meta is a
  receipt OF a terminal artifact, never a substitute — `finish_run` and
  `claim_delivery` remain untouched); and routing keys on correlators, never
  recency (two concurrent jobs cannot cross-route because neither projection
  ever consults "the most recent" anything).

  The visual lane reads the same phase off `meta.phase` for free —
  `list_runs` already surfaces meta, so the dashboard's run list gains
  progress without a new endpoint.
- A capability-kernel port plan maps TaskChad OS v1.7.0's strict discovery,
  immutable artifact, authority-separation, atomic publication, reverse
  disposal, journaled recovery, and lane-truth lessons onto Hermes-owned host
  APIs. This is documentation and an acceptance contract, not a claim that
  `hermes-talk` already supports hot plugin lifecycle changes.
- A voice session can now start already knowing who you are, which repos you
  mean by name, and what your aliases map to — provided you curate the file
  that carries it (hermes-talk#36). Dogfooding on 2026-08-16 kept hitting the
  same two failures: the session asked who the operator was every call, and
  when a spoken name could mean two things it picked one silently. Nothing on
  a voice surface shows you it guessed.

  Three parts, each an extension of a mechanism that already existed rather
  than a new one:

  `memories/WORKING.md` is a new identity section, and the only one YOU write
  instead of the model — nothing fills it for you, and it is read once at
  session mint and stays frozen for the call (an edit lands on the next
  session). It rides the same durable-file pipeline as `USER.md`
  and `MEMORY.md`, so it is threat-scanned per entry, capped (2,000 chars),
  and filtered by `TALK_IDENTITY_INCLUDE` without any of that being written
  twice — hand-authored is not the same as trusted, and anything that can
  write to your Hermes home can append an entry. Two entries claiming the
  same alias BOTH travel: resolving that by file order would bind your words
  to whichever line you wrote first, with no symptom, so the model is told to
  ask instead — a rule that rides the voice preamble on EVERY lane, gated on
  nothing, because the lanes that lose identity sections (a ctx-less gateway
  or dashboard, a pinned include list, a failed scan) are exactly the ones
  where nobody watches a silent guess go by. When a host is attached, one
  sentence naming `search_memory` is appended for anything not in the file.
  An include list pinned before `WORKING` existed silently drops the section
  after upgrade; the session logs one warning at mint when that happens.

  `search_memory` grew a middle tier. Between the transcript read
  (`session_search`) and the api-server fallback it now tries Hermes's Honcho
  memory plugin, and prefixes that answer with `from remembered context:`.
  The prefix is the point: a remembered profile fact can be stale in a way a
  verbatim transcript line cannot, and collapsing the two would make a guess
  and a quote sound identical out loud. The prefix marks FACTS only — an
  error-shaped Honcho answer is spoken without it — and it is reserved:
  transcript or vault content that leads with the literal marker has it
  stripped, so a quote can never dress itself as a recollection. A Honcho
  that is simply absent falls through; a Honcho that is present and refuses
  is spoken, not routed around; and the Honcho dispatch is bounded by
  `TALK_MEMORY_SEARCH_TIMEOUT_S` (default 10s) so a wedged plugin costs one
  spoken failure, never the serialized tool pipeline.

  `TALK_SESSION_KEY` sends `X-Hermes-Session-Key` on run submission, so the
  memory an api-server run reads and writes is scoped to you and survives the
  `/clear` that ends a `session_id`. Unset — the default — sends no header
  and changes nothing. It is deliberately static and operator-set: a key
  derived from the hostname or the clock would change between runs, and the
  one property the knob exists for would be silently missing. It is an
  OPERATOR scope, not a session boundary: every voice-channel participant
  shares it, because the authority ledger gates mutating tools and never
  memory reads — do not set it in a multi-user channel until per-speaker
  scoping lands.

  Not built, and named here so the gap is not mistaken for coverage: a
  session-mint profile pre-fetch (`honcho_context`), per-Discord-channel and
  per-dashboard-session key derivation, per-speaker memory scoping,
  code-enforced binding for spoken entities, homophone detection, any
  producer that fills `WORKING.md` (installed plugins, recent work), and
  mid-call refresh of identity sections. Ambiguity and mishears are handled
  by prompt copy plus the aliases you write yourself, the same way the
  preamble's damage-based confirmation policy governs every other
  consequential action here — not by a mechanism that can refuse.

- A spoken approval now binds to the exact action it approved, so a mutating
  request takes one summary-then-yes exchange instead of a draft → confirm →
  restate → confirm loop (hermes-talk#37). The single-use call permit minted
  in `bind_tool_event` already bound *who* approved and *which* response; it
  now also binds *what*, with each check honest about which threat it
  covers. The permit's expiry (`TALK_APPROVAL_PERMIT_TTL_S`, default 30s,
  monotonic clock) runs from the moment the operator's approving speech
  ended — never from permit mint — so a model that sits on an approved
  action cannot fire a stale yes into a conversation that has moved on; a
  binding with no approval moment mints no permit at all. For tools that
  name a target (`steer_agent`, `redirect_agent`, `stop_work`), the emitted
  target is cross-checked against a bounded window of the spoken exchange
  (operator and assistant transcripts) before the permit exists: a target
  that was never spoken to the operator is refused outright, which is the
  check that catches the model saying "steer agent A" and emitting agent B.
  Free-text arguments (a delegated task's wording) are not covered by that
  cross-check. The tool name presented at execution must match the permit's
  action, and the argument hash is a relay-integrity tripwire only — it
  detects the bound event being rewritten inside this process between bind
  and authorize, and cannot see model-side divergence from the spoken
  summary. Arguments are compared by value rather than by serialization, so
  a provider re-emitting the same arguments in a different key order, or
  `1` as `1.0`, is not mistaken for a changed request. Approvals of mutating
  tools are now logged alongside denials (operator id, tool, target — never
  raw audio); previously only refusals were recorded, which left the audit
  trail unable to show what was actually authorized. The voice preamble now
  tells the model to state its plan once and act on a clear yes.

### Fixed
- Delegated work and memory lookups are now bound to the exact Talk session
  that asked for them (hermes-talk#35). Previously the `WORK_STARTED` receipt
  was backed by nothing but an in-process dict with a fail-open history tee:
  no run recorded who started it or where the answer should go, and the only
  watcher that would ever speak the result died with the session. A job could
  finish with nobody listening, and nothing could tell a reconnecting session
  "this result is yours" apart from "this one is a stranger's". On this box
  that left three real runs stuck at `running` forever.

  `talk_runs` now mints an immutable ticket at acceptance — operator, profile,
  durable Hermes session, Talk generation, and a per-request id — and persists
  it BEFORE the worker thread starts. That one write is fail-closed: if it
  cannot land, dispatch is refused with `RoutingUnavailable` and the operator
  hears "I can't start that yet" instead of a receipt for work nothing could
  route. Everything else about the tee stays fail-open, because once a run is
  accepted its result is owed. Delivery is a two-phase claim, on disk as well
  as in memory: a result is CLAIMED exactly once at enqueue and flipped to
  delivered only after the announcement is actually handed to the wire, so a
  session torn down mid-queue leaves the result re-adoptable instead of
  consumed-but-unspoken (the residual duplication window is a crash between
  the wire hand-off and the flip — said once more on reconnect, never lost).
  A reconnecting session adopts only tickets recorded under its own Hermes
  session AND its own operator/profile binding — ownership is enforced at
  adoption, not just recorded — while a different session adopts nothing,
  and pre-#35 history, which carries no ticket, is never adopted by anyone.
  Announcements still ride the existing contained-system-item path, so an
  adopted result is exactly as untrusted as a fresh one and can never
  re-enter the conversation as operator speech.
- The run-history file is now serialized ACROSS PROCESSES, not just across
  threads: the CLI lane and the dashboard lane (the Hermes web server
  process) share one `state/talk-runs.jsonl`, so every load-modify-append —
  delivery claims, compaction, and run-id allocation — holds an OS-level
  one-byte file lock (the same msvcrt/fcntl mechanism as the transcript
  writer lease), and run ids are floored on the file's own highest persisted
  id inside that lock at every acceptance, which makes cross-process id
  collisions impossible instead of merely unlikely.
- A disabled history tee now REFUSES dispatch instead of silently accepting
  a run with no durable route; callers that legitimately want in-memory-only
  routing opt in by name (`TALK_RUNS_ALLOW_EPHEMERAL=1`).
- The api-server lane's remote run id is now written through the strict,
  cross-process-locked append at the moment it is learned — retried once and
  escalated to an error log if it still cannot land, never dropped as
  fail-open telemetry. It is the only handle a reconnect could resume
  tracking a tier-2 run by, and holding it in memory alone meant it died
  with the process a reconnect exists to recover from. The terminal tee
  happened to carry it for runs that finished, which is how the gap stayed
  hidden.
- An owed result that falls off the bounded adoption tail of the history
  file is now counted and logged instead of vanishing silently.
- The dashboard's session mint binds the browser lane's own return route, so
  `POST /tool` can still start real work under the fail-closed rule. It carries
  no Hermes session id (none is ever bound in the web server process) and never
  the ephemeral credential — a secret does not become an identifier.

## [0.9.0] — 2026-08-18

The room gets an authority boundary: only the operator's voice can authorize a
mutation, the session stops talking over itself, and "what can you do right
now?" is answered from live evidence instead of the system prompt.

### Security
- Operator speaker authority is now enforced at canonical host execution, not
  just at the Discord layer (hermes-talk#39). A mutating tool call must bind to
  the immutable operator identity all the way through the host's own
  authorization path, so another voice in the room cannot induce a mutation
  under the operator's authority.

### Fixed
- Realtime responses are serialized, eliminating duplicate and cut-off speech
  (hermes-talk#38). One active assistant response at a time; superseded
  responses are cancelled cleanly instead of overlapping.

### Added
- A live capability catalog: the new `talk_capabilities` tool answers "what can
  you do right now?" from evidence instead of from the system prompt — installed
  skills, resolved toolsets with their `enabled`/`configured` flags, the
  gateway's feature flags, and bounded run/delegation counts. `talk_capabilities.py`
  reads it in-process off the committed host attachment when a Hermes agent is
  attached, and falls back to the api server (`/v1/skills`, `/v1/toolsets`,
  `/v1/capabilities`, `/health/detailed`) when it is not — the same two-tier
  doctrine `agent_lane()` already uses. A host that does not expose the
  in-process tool degrades to REST rather than failing. The snapshot is
  TTL-cached (`TALK_CAPABILITY_CATALOG_TTL_S`, default 30s) and warmed at the
  dashboard's session mint, so a tool handler never waits on the network.
  Disabled toolsets are reported rather than hidden, so the model can say
  "installed but not usable" instead of quietly offering something that would
  fail. The tool is classified read-only: reading the catalog grants no
  execution authority, and a catalog read consumes its call permit so it cannot
  be replayed as a mutating call.

## [0.8.1] — 2026-08-15

The first PyPI release, and the one where the session stops being a prompt
with a microphone: a typed provider-neutral boundary, native setup and doctor
commands, and an explicit subscription auth lane.

### Added
- A typed provider-neutral Realtime session boundary: `talk_realtime.py` owns
  setup, events, commands, lifecycle states, and the adapter protocol, while
  `talk_openai_realtime.py` owns OpenAI ephemeral minting, WebSocket lifecycle,
  and wire translation. Hermes policy now runs against the neutral contract, and
  failed sessions stop active tool coordination through an acknowledged,
  bounded teardown. The CLI still resolves OpenAI auth and constructs the sole
  bundled OpenAI adapter; this does not add arbitrary provider selection.
- Native `hermes talk setup`: detect current state, ask only unresolved
  auth/model/voice decisions, explicitly confirm each setting, securely commit
  the confirmed set as one rollback-capable atomic transaction, emit a redacted
  apply receipt, then rerun the separately read-only doctor and verify the
  result. Key selection under preferred Codex OAuth reuses an existing key and
  separately confirms the required policy transition; key selection after an
  invalid preference now resolves the scoped key in that same transaction and
  completes setup in one run.
- Native `hermes talk doctor` human and `--json` diagnostics for registration,
  auth selection, model/voice, audio, identity profile/root/count receipts,
  Discord operators, and host capabilities. The command is strictly read-only
  and redacts credentials, identity content, operator IDs, and secret-shaped
  values pasted into malformed configuration fields.
- `TALK_PREFER_CODEX_OAUTH=true` as an explicit fail-closed subscription lane.
  Without it, the existing scoped-key → shared-key → Codex order is unchanged;
  doctor warns when a metered key wins and distinguishes valid OAuth from an
  expired credential that still requires refresh.
- Cross-platform dotenv mutation: Windows names match case-insensitively and
  duplicate case variants collapse deterministically; POSIX names stay
  case-sensitive. New secret files use POSIX owner-only modes or a native
  protected owner-only Windows DACL while existing Windows destination DACLs
  are preserved. Every staged path is cleanup-verified; a surviving temp makes
  the redacted receipt fail instead of claiming rollback. Hermes-home
  provenance follows the host's exact tilde, relative, and platform-default
  path semantics or reports unknown.
- Bounded model compatibility policy for Talk's duplex-audio and live-tool
  requirements. Specialized Whisper/Translate models fail explicitly; unknown
  Realtime-shaped ids are labeled syntax-only instead of certified valid.

### Fixed
- `pip install "hermes-talk[audio]"` now installs cleanly from PyPI
  (hermes-talk#42) — the audio extra previously failed on a fresh machine.

## [0.8.0] — 2026-08-04

The session stops arriving as a stranger. It now knows who it is talking
to, what it already knows, what day it is, and how to look something up
in your written notes — and it stopped telling the model to call tools
it does not have.

### Added
- **`USER` and `MEMORY` actually ride the session.** Both were declared
  with headers, caps and an ordering, and had no producer anywhere. The
  cause was two similarly named host surfaces: `MEMORY.md`/`USER.md`
  live on the agent's memory STORE, and this plugin read the memory
  MANAGER, which holds external providers only. They are now read from
  `<hermes_home>/memories/` directly, so it works on all three lanes —
  the gateway and the dashboard have no agent at all.
- **`search_vault`** — look something up in the operator's long-term
  written notes, as distinct from `search_memory`'s what-was-said.
  Backed by the memory provider's own index read in process, and
  advertised **only** when a lookup can really be served.
- The current date and time, built per session (a module-level clock
  would freeze at import and a long-running gateway would state the day
  it booted).

### Changed
- **The memory pointer stopped lying.** The provider's own
  `system_prompt_block` used to pass straight through, telling the model
  to call `homie_memory_search` — a real tool in a text agent's registry
  and absent from a Realtime session's, so the model called it and got
  "That tool isn't available" on a live call. Every provider's block has
  that shape, so this was wrong as a class. One sentence this plugin
  authors replaces it, naming a tool the session has.
- The vault provider is resolved once behind a single-flight lock (a
  rebuild is a full vault walk, ~0.3s measured, on the loop carrying the
  microphone), and BORROWED from a live agent when one already has it
  initialized — never shut down in that case, because it is that
  agent's.

### Fixed
- A case-variant known section name (`"memory"`) matched neither the
  ordered list nor the extras, so it vanished from the prompt entirely
  rather than rendering out of order.

### Known gaps
- Nothing is written back when a call ends (#9), and every speaker in a
  Discord channel is still treated as the operator (#10).

## [0.7.0] — 2026-08-04

Talk to it in the Discord voice channel Hermes is already sitting in.
`/talk join` turns that channel into a live duplex call — it hears you
while it speaks, you can cut it off mid-sentence, and you can delegate
and steer background agents out loud. Verified on a real call: the
session connected on `gpt-realtime-2.1` over a ChatGPT subscription,
heard the operator, answered, and spawned a background agent by voice.

### Added
- **Discord voice** (`talk_discord.py`). The plugin's audio device is
  seven methods wide, so a voice channel can wear the same shape a
  microphone does — the session, tool calls, steering ledger and
  announcements above it are unchanged.
- `/talk join` / `leave` / `status` inside the gateway. Outside it,
  `/talk` still means the terminal call.

### Changed
- **No second Discord connection.** The host already holds one and
  already decrypts DAVE; the plugin borrows it, so it is one bot, one
  connection, the host's own E2EE. For the duration of a call it takes
  over three host surfaces and returns them on stop — with one documented
  exception: the ambient mixer is dropped rather than handed back,
  because taking over playback closes it permanently and a closed mixer
  still reports itself speaking, which would stall every later host reply.
- Rate conversion is integer 2:1 both ways over `array` — no resampler
  dependency, and specifically no `audioop`, which left the stdlib in 3.13.

### Fixed
Four defects that only a real call could surface, each now pinned by a
regression whose fake models the host's actual behaviour:
- the receive tap was never registered (discord.py stores the bound
  method object and calls what it stored, so rebinding the attribute
  tapped nothing — the call heard silence with nothing logged);
- the playback source was duck-typed where `VoiceClient.play`
  isinstance-checks;
- the adapter lookup imported `Platform` from a module this host does not
  have, so it refused on a healthy gateway;
- capture forwarded only what Discord sent, so during a pause the
  server's turn detection never saw the silence that ends a turn.

## [0.6.1] — 2026-08-03

The polish release: the stop verbs can no longer dead-air the call, and
their receipts survive you hanging up. Closes #2 and #5. One adversarial
review round found three gaps (receipt durability, a reaped-handle race,
announcement interleaving) — all reconciled in-release. 390 tests.

### Changed
- `stop_work` runs its confirmation on daemon workers with a bounded 1.5s
  courtesy wait: the common fast path still speaks the real result, a slow
  server gets honest detached wording, and the voice loop never freezes
  (the old synchronous path could block it ~6s).
- `terminate()` is now confirmed, not just signaled: the exit code is read
  from a handle captured before the signal (immune to the run worker
  reaping the child first), and the run record is consulted before any
  uncertainty claim is spoken.
- All out-of-band announcements (finished children, landed notes) flow
  through one serialized pump — whole batches, deferred while a response
  is in flight, so concurrent events can never interleave or stack active
  responses.

### Added
- Landed steering notes are now **pushed**: the moment a note's delivery
  artifact fires, the live call hears "the note just landed" instead of
  waiting for the next `check_work`.
- Stop receipts persist to the run history (`annotate_run(tee=True)`), so
  a receipt promised past the courtesy wait survives a process restart and
  the next session's `check_work` can still keep the promise.
- `uninstall_watchers()` — a production unhook symmetric with the two
  `ensure_*` calls; the borrowed logger level is reconciled on every
  ensure (operator verbose-logging toggles are honored both directions).

## [0.6.0] — 2026-08-03

The release where delivery confirmation stopped having a blind spot, and
the plugin gained a stronger verb than a queued note. Gate chain: 368
tests + ruff → two Codex adversarial rounds (six findings, all
reconciled) → Kimi K3 design gate PASS — "every mechanism degrades toward
less information, never toward a wrong claim."

### Added
- **Second delivery artifact**: a watcher on the host's pre-API steer
  drain. Notes delivered right before a model request used to terminate
  as false "unconfirmed"; they now land. Attribution is by frame identity
  against the agent captured at steer time — exact, never heuristic.
- **Push lifecycle**: `subagent_start`/`subagent_stop` hooks roster
  children by session id; completions are announced into the live call
  the moment the host reports them (injection-contained: system-role
  item, `tool_choice: "none"`, self-deleting in the same batch).
- **`redirect_agent`** — interrupt a child's current step and re-aim it
  now ("stop, wrong repo"), on the 0.20-public `AIAgent.redirect()`. The
  receipt comes from the return value; the wording never claims more than
  the host guarantees. Degrades to the steer queue on pre-0.20 hosts.
- **Correlation tokens** (closes #1): every note travels as
  `[tk-xxxxxxxx] note` and delivery matching is token-first — two agents
  holding identical text can never land each other's receipts.

### Fixed
- The v0.5 wheel silently omitted `talk_steer` from `py-modules` — the
  exact trap the pyproject comment warns about, caught in the wild.
- The dashboard manifest version had been stuck at 0.3.0.

## [0.5.0] — 2026-08-03

`steer_run` was retired for telling comfortable lies; this is the surface
that replaced it. Three reviews, one verdict: it claimed delivery the
substrate cannot know. v2 speaks only what an artifact proves.

### Added
- Run-control surface: `list_agents` (discovery-first ids), `steer_agent`
  (queued is the only call-time claim), `stop_work` (the one verb every
  lane supports — and every "want me to stop it?" offer is real).
- The receipt ledger (`talk_steer`): queued → landed / unconfirmed /
  missed / superseded, each state upgraded only by a named artifact
  (the host's own drain log line, watched in-process).

### Removed
- `steer_run` — replaced by the surface above.

## [0.4.0] — 2026-08-02

The under-named release: its title was the steering verb, but the same
window shipped the browser dashboard and the api-server agent lane.

### Added
- `steer_run` — redirect a live background agent by voice (superseded in
  0.5.0 by the honest surface).
- **Dashboard tab**: the browser voice page — WebRTC audio, tool relay,
  run watcher, voice picker, and its own token gate
  (`TALK_DASHBOARD_TOKEN`; loopback-only when unset). Four backend routes
  under `/api/plugins/hermes-talk/`, every one auth-gated by construction.
- **Three-tier agent chain**: attached in-process loop → a real agent
  over the api_server platform → detached `hermes -z` one-shot; every
  fall-through announced, the active lane reported by `talk_status`.

## [0.3.0] — 2026-08-02

Sessions that start already knowing you: the host's identity files ride
the session instructions, budgeted and trimmed, so the first sentence out
of the model isn't from a stranger.

### Added
- Voice identity assembly with per-section budgets and the
  `TALK_IDENTITY_INCLUDE` knob (REPLACES the default set — the trap is
  documented where the knob is).
- The autoplaying dashboard demo in the README, recorded from a real
  session (hosted on this release's page).

## [0.2.0] — 2026-08-02

Background delegation that speaks its results: hand work off mid-sentence,
keep talking, and hear the result the moment it lands — even if you went
quiet.

### Added
- `delegate_task` / `check_work` and the async-run registry with a
  durable, honest history tail (a run from a dead process reports `lost`,
  never "still running").
- Dual-lane credentials: an OpenAI API key or a ChatGPT subscription via
  Codex OAuth — resolved fail-closed, and only the ephemeral session
  secret ever touches the socket.
- The offline test suite (82 tests then) and CI across
  {ubuntu, windows} × {3.11, 3.12, 3.13} — no secrets, no network, no
  audio device.

### Fixed
- GA Realtime protocol compliance (session.type on every update; the
  retired beta header dropped) and honest process exit codes.

## [0.1.0] — 2026-08-02

Repo foundation: the plugin manifest, call-time config resolution, and a
pure Realtime wire layer that knows the OpenAI protocol and nothing about
the host.

[0.8.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TheSmokeDev/hermes-talk/releases/tag/v0.1.0
