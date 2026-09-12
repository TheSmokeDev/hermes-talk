# GPT-Live and task workers

Hermes Talk 0.18.0 connects GPT-Live conversation to a selected Hermes task.
That task can delegate to Hermes or an explicitly configured Codex worker while
you keep talking. Voice, typed input, worker receipts and results share task
ownership. Closing or switching voice does not cancel accepted background work.

**Operator microphone acceptance is still pending for subscription and API on
all three surfaces.** Provider-only connection probes and offline regressions
are narrower evidence. Use the acceptance checklist below before treating an
installation as complete.

## Prerequisites

- Python 3.11 or newer, with dependencies installed in the Python environment
  used by the Hermes host and gateway.
- A compatible Hermes build exposing authenticated task history, linked-child
  execution/control and native attachment. Codex needs the profile-scoped
  `PluginContext.register_task_worker_provider` hook. Discord also needs trusted
  command invocation and speaker/audience proofs. A legacy-compatible host
  version number or a plugin-only update does not establish these capabilities.
- Codex CLI **0.154.0** for Codex workers. This is the adapter's verified version,
  not a floating latest-version requirement. Worker configuration and Codex
  authentication belong on the host that will execute the job.
- A valid Codex OAuth login with its account ID for Live subscription, or an
  explicitly selected API key for Live API. Provider entitlement is checked when
  connecting; installing the code does not grant access.

| Surface | Subscription audio | API audio | Dependencies |
|---|---|---|---|
| Dashboard | Browser WebRTC | Browser WebRTC | Base package on server; browser microphone permission |
| Terminal | `aiortc` WebRTC | `aiohttp` WebSocket | `[audio,live]` for subscription; `[audio]` for API |
| Discord | `aiortc` WebRTC | `aiohttp` WebSocket | `[live]` for subscription, host Discord voice dependencies |

`aiohttp` is a base dependency. Install `[audio,live]` if one host needs every
native surface. The existing Realtime, Grok, Gemini and cascade lanes retain
their documented scopes. Gemini on Discord remains unavailable. Live uses
provider-native turn detection; semantic endpointing overrides are unsupported.

## Choose billing and voice

Set these in the active profile's private environment, and restart the affected
gateway/dashboard processes. A terminal-only environment change applies only
to processes started from that terminal.

```dotenv
TALK_VOICE_MODE=live
TALK_LIVE_AUTH=subscription
TALK_LIVE_SUBSCRIPTION_MODEL=gpt-live-1-codex
TALK_LIVE_SUBSCRIPTION_VOICE=cove
TALK_LIVE_API_MODEL=gpt-live-1
TALK_LIVE_API_VOICE=marin
```

Run `codex login` as the account running Talk for subscription authentication.
`CODEX_HOME` may point to that account's alternate Codex store. Live requires the
OAuth account identity as well as the access token. A missing, expired or rejected
subscription fails in that lane; **it never falls back to a paid API key**.

For API billing, explicitly set `TALK_LIVE_AUTH=api` and provide
`TALK_OPENAI_API_KEY` or `OPENAI_API_KEY` through the private environment. The
Talk-scoped key wins. A configured blank key refuses; API mode never falls back
to OAuth. Do not place provider credentials in browser code, URLs, screenshots
or public support bundles.

| Setting | Default | Accepted values |
|---|---|---|
| `TALK_VOICE_MODE` | `native` | `native`, `cascade`, `live` |
| `TALK_LIVE_AUTH` | `subscription` | `subscription`, `api` |
| `TALK_LIVE_SUBSCRIPTION_MODEL` | `gpt-live-1-codex` | `gpt-live-1-codex`, `gpt-live-1-boulder-alpha` |
| `TALK_LIVE_SUBSCRIPTION_VOICE` | `cove` | `arbor`, `breeze`, `cove`, `ember`, `juniper`, `maple`, `sol`, `spruce`, `vale` |
| `TALK_LIVE_API_MODEL` | `gpt-live-1` | `gpt-live-1` |
| `TALK_LIVE_API_VOICE` | `marin` | `alloy`, `ash`, `ballad`, `beacon`, `bossa`, `cedar`, `cinder`, `coral`, `delta`, `echo`, `gleam`, `marin`, `meridian`, `quartz`, `ripple`, `sage`, `shimmer`, `stone`, `tempo`, `verse`, `vesper`, `willow` |

Only the selected billing option's model/voice settings apply. The compatibility
aliases `TALK_LIVE_MODEL` and `TALK_LIVE_VOICE` are overridden by its explicit
subscription/API settings. Prefer the separate settings when switching billing;
unsupported combinations and blank configured values refuse. `TALK_MODEL`,
`TALK_VOICE` and `TALK_PREFER_CODEX_OAUTH` retain their Realtime meanings and do
not select Live billing. `TALK_PROVIDER` continues to select the legacy provider.

The dashboard receives an opaque task/audio binding and SDP answer. Provider
credentials, account IDs and provider session IDs stay on the server. The server
owns Live delegation decisions; browser data-channel events do not authorize work.
Native transports use the same task coordinator through authenticated host routes.

## Configure a Codex worker

```bash
npm install -g @openai/codex@0.154.0
codex --version
codex login
```

Configure `plugins.entries.hermes-talk.settings.codex_worker` with `enabled: true`,
an **absolute native Codex executable**, an existing workspace, an explicit model,
and the intended sandbox/approval policy. The full YAML and Windows executable
lookup are in [codex-workers.md](codex-workers.md#configure-on-the-executing-host).
An npm `.cmd`/PowerShell shim is not the native Windows executable.

Ask explicitly: “Use Codex as the worker for this task.” The host must advertise
`hermes-talk-codex` in that profile. Normal delegation continues to use Hermes.
For a registered remote peer, configure the worker on that peer; selection does
not move its execution to the dashboard machine or grant access to other profiles.
Codex worker authentication is independent of the selected Live billing option.

## Start and control a task

Start with a harmless task in a disposable workspace. Select an existing task
before handing off work. Target selection does not create a new job; steering
addresses the original job and its current turn. A queued acknowledgement is
not proof the worker applied the instruction.

### Dashboard

1. Open the authenticated Hermes dashboard and its **Talk** tab.
2. Choose the configured peer, profile and task, then press **Start**. Only this
   operator action requests microphone permission and starts browser audio.
3. Keep speaking or use the text input. Open task results in the results panel;
   select another task and return using the task controls.
4. Press **Stop** to end voice. Reconnect to the task to inspect ongoing work.

Live polling renews the authenticated binding. Logout, a stale binding or a
sideband failure closes audio and stops the microphone. Reconnect requires fresh
authorization. There is no automatic paid reconnect or replacement job.

### Terminal

Configure an explicit authenticated Hermes **dashboard origin**, such as
`http://127.0.0.1:8080` with the port your installation actually uses:

```dotenv
TALK_TASK_API_URL=http://127.0.0.1:8080
```

Use `TALK_TASK_SESSION_TOKEN` for the host's `X-Hermes-Session-Token` when required,
and `TALK_DASHBOARD_TOKEN` for Talk's additional route gate. Keep their values in
private configuration. A raw run-api gateway URL, a URL containing credentials,
or an origin with a path/query/fragment is not valid here.

```bash
hermes talk --targets --peer local --profile default
hermes talk --task TARGET_ID --task-tab terminal
```

`--targets` lists authorized choices without opening a microphone or provider.
`--task` accepts a target ID, exact session ID or exact unambiguous label;
`TALK_TASK_TARGET` is its environment equivalent. `--task-api ORIGIN` overrides
`TALK_TASK_API_URL`. Reuse `--task-tab` for a reconnect to the same attachment.
Live without a selected task refuses before starting audio.

During the call, a complete typed line is submitted as exact task input:

| Terminal input | Effect |
|---|---|
| `/targets` or `/targets PEER PROFILE` | List authorized targets |
| `/select TARGET_ID`, `/return` | Switch the voice attachment, or return |
| `/reconnect` | Reattach with refreshed context |
| `/state`, `/result RUN_ID` | Inspect current work or its complete inert result |
| `/preference important`, `/preference completion`, `/preference frequent` | Set the task's saved spoken-update preference |
| `/pause`, `/resume`, blank Enter | Pause/resume the microphone; Enter toggles when Talk owns a TTY |
| `/interrupt` | Interrupt current voice output; it does not cancel a worker |
| Ctrl+C | End voice |

To steer, approve or cancel work, address the current job in voice or typed input.
Answer only the current approval request; a stale request is refused. A stop
request can retain partial output and cannot undo actions already completed.

### Discord

Join the voice channel through the host's `/voice join`, then use:

```text
/talk join [TARGET [PEER PROFILE]]
/talk select TARGET
/talk return
/talk reconnect
/talk say TEXT
/talk state
/talk result RUN_ID
/talk preference important
/talk pause
/talk resume
/talk interrupt
/talk status
/talk leave
```

Brackets denote optional arguments. With no target, `join` uses the canonical
conversation captured from that authenticated Discord command. Target casing is
preserved. Update preference also accepts `completion` or `frequent`.

Set `TALK_DISCORD_OPERATOR_USER_IDS` to explicit immutable operator IDs. The
host also requires an explicit Discord member or guild-role allowlist grant for
**every human listener**. Unknown members, an unapproved listener, changed room
membership or expired proof stop room access. A display name, model assertion,
shared dashboard token or operator environment variable cannot grant the room
proof. Rejoin through a fresh authenticated command when the old proof retires.

Task/catalog/result text-channel replies are receipts only. Full contents remain
in the authenticated Talk dashboard or terminal; voice uses bounded summaries.
A voice-room proof does not authorize posting those contents to a broader text
channel. Current room approvals permit one exact request, `once` or `deny`.
Closing/changing the room ends its access without cancelling accepted work.
Existing RoomLink grants remain separate and cannot dispatch these external workers.

## Operator acceptance

Run this sequence separately for every row, explicitly selecting billing on the
process that owns audio. Start the microphone yourself; a silent provider probe
cannot replace this sequence.

| Surface | Subscription | API |
|---|---|---|
| Dashboard | Pending microphone acceptance | Pending microphone acceptance |
| Terminal | Pending microphone acceptance | Pending microphone acceptance |
| Discord | Pending microphone acceptance | Pending microphone acceptance |

1. Confirm loaded host/plugin versions, Live billing/model/voice and Codex 0.154.0.
   Select the test task and start the call.
2. Ask it to launch a harmless Codex job. Record the Hermes run ID. Keep chatting
   while it works, then steer **that same job** and verify the correction in its
   result. A voice acknowledgement alone is insufficient.
3. Switch to another authorized task, return, and confirm the original run remains
   inspectable. Submit typed input and verify it is recorded in the selected task.
4. Exercise a harmless approval under the configured worker policy. Answer its
   current request, then confirm an expired/duplicate response cannot approve a
   later request. Do not relax policy just to bypass this check.
5. Inspect the complete result and any artifacts. Interrupt spoken output and
   confirm partial captions remain partial. A short spoken summary is not the result.
6. Start a second harmless job, request cancellation and inspect its actual state
   and retained partial output. An unconfirmed cancellation must stay labelled so.
7. Close voice with work running, reconnect and inspect the original run. Refresh
   context and verify there is no replacement job. Verify a configured remote peer
   still executes on its own host, with its own profile and permissions.
8. Check an unavailable/rejected subscription and explicit API authentication failure.
   Both must close audio cleanly; subscription failure must never spend an API key.
   In Discord, also verify that an unapproved listener prevents task delivery/control.

Record version/revision, surface, selected auth/model, original run identity and
outcome in a private acceptance record. Do not publish conversation content or
credentials. Offline regressions cover duplicate delegation, stale approvals,
disconnects, worker hangs and interrupted audio; installed operator acceptance
adds the real account, microphone, speaker and room behavior.

`hermes talk doctor` is read-only configuration evidence. The existing
`hermes talk check` exercises the legacy `TALK_PROVIDER` flow and a bounded Hermes
run. Neither establishes the six GPT-Live rows above.

## Upgrade and rollback

Before replacing code, record `hermes --version`, `hermes plugins list`,
`codex --version`, the host/plugin commit IDs and the Python dependency lock.
Preserve local tracked **and untracked** changes, including a modified
`package-lock.json`. Keep profiles, credentials, conversation databases and local
changes in an access-restricted rollback snapshot that retains their permissions.
Take a consistent database snapshot after the relevant gateway is stopped. Let
accepted jobs finish, or explicitly cancel and inspect them before this maintenance.

Install the coordinated compatible host build through its supported installer;
keep the existing profile/home data. A floating stock-host update is not proof
that the required task-worker and Discord context contracts are present.

After the candidate is published, install the matching Talk code and dependencies
in the host environment. For an existing unpinned Git plugin:

```bash
hermes plugins update hermes-talk
python -m pip install -U "hermes-talk[audio,live]==0.18.0"
hermes gateway restart
hermes gateway status
hermes plugins list
hermes talk --help
```

For a pinned plugin, use the reviewed **full 40-character commit SHA**. These
PowerShell variables are operator-supplied values from your release/rollback record:

```powershell
hermes plugins install TheSmokeDev/hermes-talk --force --ref $TalkReleaseCommit --enable
python -m pip install --force-reinstall "hermes-talk[audio,live]==0.18.0"
hermes gateway restart
```

`--force` replaces the plugin directory, so preserve its local changes first.
Restart a separately hosted dashboard through its own service as well. Confirm
the running dashboard's `/api/plugins/hermes-talk/status` version and selected
voice mode through an authenticated request; CLI/package versions alone do not
prove what an existing process loaded. Then complete operator acceptance.

For a voice-only rollback, end the call, restore the previous profile settings
(for example `TALK_VOICE_MODE=native` and its previous provider/auth policy), then
restart the affected service. Review that previous policy before starting a call:
legacy Realtime key precedence differs from Live subscription selection.

For a full code rollback, stop the affected gateway, retain any new conversation
history, restore the saved compatible host code and local changes, then restore
the exact prior Talk revision and environment. With the previous values recorded:

```powershell
hermes gateway stop
hermes plugins install TheSmokeDev/hermes-talk --force --ref $PreviousTalkCommit --enable
python -m pip install --force-reinstall "hermes-talk[audio]==$PreviousTalkVersion"
npm install -g "@openai/codex@$PreviousCodexVersion"
hermes gateway restart
hermes gateway status
hermes plugins list
codex --version
```

Restore the saved dependency lock/environment when package dependencies also
changed. If Codex returns to an unsupported worker version, disable the worker
until its matching adapter is restored. Do not restore an old database over new
history or erase a profile to roll code back. Reapply retained local patches only
to the matching host/plugin revision and verify them before restarting.

Sources and licenses: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
