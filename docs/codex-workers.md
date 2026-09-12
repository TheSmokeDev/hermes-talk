# Codex workers

A bound Talk task can explicitly select Codex for a background job while the realtime
voice manager continues the conversation. Hermes owns the canonical parent/child,
authenticated run, origin receipt, controls and result. Talk supplies a worker provider
through the host's optional `register_task_worker_provider` hook. It never attaches to
an arbitrary Codex desktop task or opens a new network listener.

## Configure on the executing host

Install a compatible host with the generic task-worker hook and this Talk version.
Configure the plugin through its existing settings in that profile's `config.yaml`:

```yaml
plugins:
  entries:
    hermes-talk:
      settings:
        codex_worker:
          enabled: true
          executable: /absolute/path/to/codex
          workspace: /absolute/path/to/workspace
          model: your-explicit-model
          sandbox: read-only
          approval_policy: untrusted
          approvals_reviewer: user
```

Use the actual Codex executable and an existing absolute workspace. On Windows use
the native `codex.exe`, not `codex.cmd` or a PowerShell shim. After
`npm install -g @openai/codex@0.154.0`, find the native binary in PowerShell:

```powershell
Get-ChildItem -LiteralPath (Join-Path (npm root -g) '@openai') -Recurse -File -Filter codex.exe |
    Select-Object -ExpandProperty FullName
```

Choose the binary matching the installed platform, verify it directly with
`& 'C:/absolute/path/to/codex.exe' --version`, and put that absolute path in
`executable`. This integration currently verifies **Codex CLI 0.154.0**
before launching `app-server --listen stdio://`; a different version refuses. Codex
uses its own configured authentication. This setup does not create, read out, or copy
credentials. Disabled or malformed configuration starts no worker process or job.
Supported sandbox choices are `read-only` and `workspace-write`. The returned model,
workspace, approval policy/reviewer and sandbox are verified; unexpected policy changes
refuse before a model turn. No unrestricted sandbox option is offered.

The host advertises `hermes-talk-codex` only for a configured provider in the exact
profile's registry. When it is advertised, the bound `delegate_task` tool gains
`worker: codex`. Ask explicitly to use a Codex worker; ordinary delegation still uses
Hermes. For a configured remote peer, install/configure the worker on that peer.
The dashboard sends the same authenticated linked-child request to that peer; it does
not execute the peer's job on the dashboard machine. Discord voice can dispatch a
worker only with the compatible host's current event-issued operator/audience proof;
reads, controls, approvals and spoken delivery recheck that room binding. Existing
RoomLink hosted-room grants remain separate and cannot dispatch external workers.
See [Discord operation](GPT-LIVE.md#discord).

## Ownership, controls and recovery

Each Hermes job records its original parent, child, origin/action, explicit policy,
and only the Codex thread/turn it created. The worker process stays independent of
voice attachment changes. A fresh input is never re-created from a spoken summary.
Steering uses `turn/steer` with the original `expectedTurnId` and a stable client
message identity. A host queue acknowledgement means queued; it does not prove the
worker applied a correction. Cancellation uses `turn/interrupt`, preserves partial
results and does not promise rollback of completed side effects.

Approval requests retain their server RPC ID, process generation, thread, turn and
item. Only the current original request can be answered; a duplicate response or
reconnect cannot approve the next request. A written decision is labelled a transport
handoff, not proof that an action ran. Unsupported server requests are refused. Owner
validity and the exact canonical child lease are rechecked at the subprocess write
boundary for work and approvals. Cancellation/retirement prevents queued new writes;
the narrow interrupt path can still stop the original worker.

If a response is lost, the adapter reads only its recorded thread and correlates the
original `clientUserMessageId`. It never starts a replacement turn to simulate recovery.
After stop is requested, interruption has a ten-second outcome deadline. A missing
interrupt reply or terminal event closes the owned process and preserves the original
mapping/partial result as `cancellation_unconfirmed`; it never claims confirmed stop or
starts a replacement. The host adapter makes one bounded reconciliation attempt after a disconnected process
with a known thread. A thread start with no recoverable ID stays unknown. Stored
in-progress work without a live active turn also stays unknown. Another explicit user
action is needed to authorize replacement work; a retry does not imply it.

Full assistant result sections and supplied file-change artifacts remain inspectable.
Failure/cancellation retains available partial output. Spoken presentation remains
separate and follows the parent task's saved update preference. Recovered results are
visible and silent.

The derived mapping store supports 32 jobs, 2 MiB per job and 16 MiB total. Terminal
records older than seven days are reclaimed during admission; active/unknown starts
are never silently evicted. An active worker whose canonical owner is deleted is
cancelled and its mapping is removed. There are at most 64 control receipts per job,
16 pending approvals and 256 queued protocol events. Reads/writes and shutdown are
bounded; process teardown targets only the process this adapter created.

## Evidence and protocol

Offline acceptance runs the actual stdio/SQLite implementation with a scripted process,
and the dashboard coordinator through real Hermes HTTP ingress, plugin discovery,
canonical storage, steering and approval routes. No actual Codex model turn, microphone
session, installation or deployment is established by those tests.

Protocol reference: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
The installed CLI's generated JSON schemas are the tested wire contract. This is an
original Python protocol client; no Codex Rust source was copied. The referenced public
Codex source is Apache-2.0; Hermes Talk retains its existing MIT license.
