# Address an existing application task

A Hermes voice task owns the conversation and its permissions. Its selected
recipient is the existing application task you want to address. Selecting that
recipient does not switch the Hermes task or start another worker.

The executing host must advertise its authenticated recipient bridge. Update
both the host and Talk; the plugin alone cannot add desktop control to a remote
host. Availability is reported per application and per task. A discovered title
or stored conversation is not proof that the task can receive instructions.

## Say what you want to address

| Request | Result |
|---|---|
| "List my Codex tasks" | Lists verified recipients on the selected execution host. |
| "Select Codex task NAME" | Selects that exact recipient; duplicate names require a choice. |
| "Tell Codex: focus on the login bug" | Sends to the selected existing Codex recipient. A selected Claude task cannot receive this request. |
| "Select Claude Code task NAME" | Selects the existing Claude task and reports its available operations. |
| "Start a Codex worker to inspect this repo" | Creates a separate Codex worker through the existing task coordinator. |
| "Check that run" / "Steer that run" | Addresses the original worker run and its current control receipt. |
| "Inspect the selected task's window" | Requests a fresh screenshot from that host's verified computer-use capability. |

These requests work through the same task coordinator on dashboard, terminal and
Discord. Before sending, the bridge checks application, process, task, composer
and exact message. A shell prompt, changed task, unexpected approval dialog or
unverified composer refuses delivery. Inspect the returned capability instead of
assuming every discovered task supports sending.

## Read the receipt

- **queued**: a durable operation exists; delivery is not established.
- **posted**: the exact message appeared in the selected conversation.
- **accepted**: the selected recipient acknowledged the operation.
- **completed**: the original operation has a completion receipt.
- **failed**: delivery failed with a recorded reason.
- **unknown**: delivery is uncertain. Check the original operation before retrying.

A worker acknowledgment cannot establish delivery to a desktop application.
Reading an uncertain operation reconciles its original recipient without posting
another message. The returned result retains the full receipt and any screenshot
artifact metadata; spoken summaries are shorter and cannot authorize actions.

## Disconnect, switch and return

Accepted work continues when audio disconnects. Reconnect renews authorization
before new controls or private results. Transcript retries reuse their original
identities and never replay a reasoning call or worker launch. Requests admitted
before a target switch stay attached to their original task.

Transcript batches flush at 100 ms, 32 fragments or 8 KiB. A single larger provider
item is preserved as one item. Item snapshots and finished conversation turns
are separate, so late words and repeated words are not silently discarded.
Completion becomes eligible independently of pending transcript writes; actual
playback still depends on provider audio and interruption state.

Discord rechecks the operator and every human listener. A changed audience can
stop private output without cancelling already accepted work.
