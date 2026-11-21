# Same-chat hybrid Discord voice (experimental)

`/talk hybrid join` adds short host tool operations and native background children
to [same-chat delegated voice](delegated-discord.md). It uses the same initiating
speaker, profile and text conversation. Ordinary `/talk join` is unchanged.

This is a development integration, not a released host compatibility promise.
It requires the host plugin APIs `submit_message`, `submission_status`,
`control_submission`, `conversation_tools` and `execute_tools`, plus plugin
`allow_gateway_injection` permission. Missing APIs or a refused tool catalog stop
setup. There is no fallback to detached API-server work or bare tool dispatch.
Realtime and cascade use this route; GPT-Live does not.

Follow the delegated-mode setup, including `/voice off` in the text chat, then
use `/talk hybrid join`. The initiating speaker can ask for a short operation,
start a longer task, and discuss another subject while the child works. Host
tool availability, permissions and approvals still apply. This change does not
add a spoken approval bridge: an approval may still require the host's interface.

| Voice tool | Host behavior |
| --- | --- |
| Advertised host tool | Executes the exact arguments through the normal host conversation loop and tool executor. |
| `delegate_work` | Starts native `delegate_task` children; refuses if the host cannot run them in the background. |
| `delegation_control` | Lists native children or requests steering/stop for a specific child ID. |
| `submit_task` | Submits an ordinary parent conversation turn; it can occupy the parent until it finishes. |
| `submission_status` / `control_task` | Observes or controls that exact submitted parent turn, not its children. |

The parent remains a single writer. Short operations queue behind any current
parent turn; they can run while native background children work. A slow direct
tool can occupy the parent too: the prompt asks the model to delegate code changes,
investigations and test suites, but it cannot predict every tool's duration.
The voice call remains duplex while host requests are pending.

Submission status reports available controls and observed tool progress, not a
percentage or an invented completion estimate. Queued work can be cancelled;
running parent work can be steered or interrupted when the host advertises that
control. A stale authority/generation is refused. A lost control acknowledgement
is reported as unknown and is not automatically retried.

Native child steering is backend-dependent. This integration verifies delivery
to the next native Hermes agent inference. It does not add steering delivery to
native Codex app-server children: their queue acknowledgement is not proof of
delivery. Child control requires the host's resident parent/tree ownership; it
does not adopt arbitrary child sessions after a restart. A stopped or completed
parent turn is not evidence that its children stopped or completed.

Exact tool requests reuse their original admission identity when observed again.
A repeated provider call cannot change its arguments. Terminal receipts carry
actual tool outputs; background completion notices remain separate. Updates and
results enter voice context and announcements wait for idle speech/playback,
using the existing delegated-mode behavior. Interrupting speech does not cancel
work. No automatic child-result injection into an active host inference is added.

The delegated-mode reset/compaction, history and audio-bridge limits still apply.
Leave and rejoin after a reset or compaction. Talk tracks at most 64 requests per
call and does not adopt its previous call's requests after a process restart.
Live microphone, provider audio, Discord reconnect and spoken approval behavior
need acceptance testing before relying on this mode in production.
