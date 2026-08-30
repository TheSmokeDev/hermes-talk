# Full-capability voice (capability bridge) for hermes-talk — spec

Status: direction approved, pre-implementation. 2026-08-28.
Owner repo: TheSmokeDev/hermes-talk. No core edits.
Target: 0.15.0. Includes one folded bugfix (transcript flush drop, below).

## Why now — tonight's evidence

A live Discord-lane session (2026-08-28 ~18:53) showed the model reaching for
REAL host capabilities the voice surface doesn't expose: `computer_use`,
`tool_describe`, `tool_call`. The ledger denied all three (default-deny
worked), but the user heard flat refusals. The model wasn't hallucinating —
`computer_use` is a genuine registered core tool
(`tools/computer_use/tool.py`, in `_HERMES_CORE_TOOLS`). It learned the name
from the capability catalog our own `talk_capabilities` lane reads. The model
knew what Hermes can do and tried to do it. The surface failed it, not the
model.

The vision (operator-approved): voice is the MANAGER of Hermes's whole
capability surface — knows everything, does what's safe directly, delegates
the rest with spoken approvals, and narrates progress. Never a bare "I can't."

## Host facts (verified 2026-08-28 against the installed checkout)

- **Registry**: `tools/registry.py` singleton; every built-in/MCP/plugin tool
  registers with schema + `check_fn` availability gates. Tools carry NO
  read-only/mutating flag — risk classification lives in-handler (terminal
  command guards, computer-use `_SAFE_ACTIONS` vs `_DESTRUCTIVE_ACTIONS`) or
  in the approval layer.
- **Dispatch hazard**: `ctx.dispatch_tool` bypasses the whole
  `handle_function_call` layer (no hooks, no middleware, no tool_search
  scoping), and residual in-handler approval gates FAIL OPEN when the calling
  thread has no interactive/gateway approval context
  (`tools/approval.py:3081-3086`; computer-use default-allow at
  `tools/computer_use/tool.py:534-537`). **Bare dispatch of gated tools from a
  plugin thread is silently unguarded. Forbidden in this design.**
- **The approval substrate exists, voice-shaped**:
  - REST: `POST /v1/runs` streams `approval.request` events over SSE;
    `POST /v1/runs/{id}/approval {choice}` resolves them
    (`gateway/platforms/api_server.py:6422-6450, 6730-6816`; advertised in
    `/v1/capabilities`).
  - In-process: `register_gateway_notify(key, cb)` /
    `resolve_gateway_approval(key, choice)` / `set_current_session_key` are
    public module functions in `tools/approval.py` — the same seam the
    gateway runner uses per turn.
- **Catalog lanes**: `/v1/skills`, `/v1/toolsets`, `/v1/capabilities`,
  `/health/detailed` already power `talk_capabilities` out-of-process;
  in-process enumeration works via `tools/registry` +
  `model_tools.get_tool_definitions(skip_tool_search_assembly=True)`.
  NOTE: the plugin's tier-1 probe name `list_capabilities` does not exist
  upstream (dead fast path today) — this spec gives the lane a real source.
- **delegate_task** refuses in-process without a parent agent in gateway mode
  (`tools/delegate_tool.py:2802`); the api-server run lane is already our
  proven fallback and becomes THE lane for gated work.
- **Progress narration exists** (#33): `talk_progress.py` phase speech +
  `talk_runs` registry/watcher. This feature reuses both, unchanged.

## Design

### 1. Real capability knowledge in the voice instructions

The session instructions gain a bounded CAPABILITIES section: the resolved
tool categories + skill count + the delegation ceiling line ("you can
delegate anything Hermes can do"), plus the hard rule: **never invent tool
names; the only callable tools are the advertised set; capability questions
go to `talk_capabilities`.** Size-capped like the other identity sections;
assembled from the real catalog lanes (fail-open to today's preamble if the
catalog is unreachable).

### 2. Classification table (plugin-owned, default-deny)

`talk_operator_auth` already gates the 10 talk tools. It gains a host-tool
classification table with three classes:

- `VOICE_INLINE_SAFE` — curated read-only host tools callable inline via
  `ctx.dispatch_tool` (only after the thread binds approval context; see §3).
  v1 keeps this list SHORT and auditable.
- `VOICE_PERMIT_GATED` — state-reading-but-sensitive (e.g. computer-use
  capture/list actions) allowed under a fresh spoken permit.
- Everything else — delegate/run lane.

Unclassified names (the `tool_describe` class of invention) get the new
deny receipt from §4.

### 3. The delegate/run bridge with spoken approvals

Heavy or mutating work routes to the api-server run lane with the approval
loop bridged into speech:

1. Voice starts the run (`delegate_task` with the task naming the needed
   capability — e.g. "check my screen and summarize" with computer_use).
2. The runs SSE `approval.request` event arrives → the voice speaks the
   prompt verbatim-ish ("The agent wants to click the mouse. Approve?").
3. The operator's spoken answer resolves through the existing permit
   machinery → `POST .../approval` with `once` / `session` / `deny`.
   **`always` is never grantable by voice** — the choice set is narrowed in
   code, not in the prompt.
4. Approval prompt timeout = deny (fail closed). A barge-in during an
   approval prompt = deny.
5. Progress narration rides the existing #33 machinery; the result lands
   spoken through the existing runs watcher.

In-process `register_gateway_notify` bridging stays OUT of v1 — the REST
loop is the sanctioned, already-shipped substrate.

### 4. Deny receipts that steer, not refuse

When the ledger denies anything (unclassified name, permit-gated without
permit), the receipt spoken back follows the delegation ceiling: state the
limit, offer the bridge. Shape: "I can't do that directly in voice, but I
can spin up an agent that can — want me to?" Never a bare refusal. The model
instructions get the same rule so it stops inventing names in the first
place.

### 5. Folded bugfix — transcript flush drop (Discord lane)

2026-08-28 evidence: `talk_transcript` handoff refused and dropped with
"no Talk connection is bound, so there's nowhere to deliver the result."
Root-cause and fix the Discord-lane flush binding so session transcripts
persist (or queue) instead of dropping. Own commit, regression test
(Discord-lane session end writes the transcript).

## Testing

- Classification table: every class routes correctly; unclassified → steering
  receipt; `always` ungrantable by voice (code-level assertion).
- Bridge: fake run lane with scripted SSE approval.request → spoken prompt →
  yes/no/barge-in/timeout → correct resolve call; denial paths.
- Instructions: capability section is bounded, names the delegation ceiling,
  includes the never-invent rule.
- Deny receipts: wording tests (steer, never bare refusal).
- Transcript flush: Discord-lane regression.
- Existing suites untouched and green; plugin-guard green.
- Live smoke (operator machine): terminal call — "what can you do?" → real
  catalog answer; "check my computer" → delegate + spoken approval
  round-trip + progress narration; transcript persists after.

## Non-goals (this PR)

- In-process notify-callback bridging (REST loop only).
- Inline dispatch of any mutating/gated tool.
- Granting `always` by voice. Ever.
- Dashboard approval UI (dashboard keeps its current behavior; the bridge is
  a voice-lane feature).
- Per-persona/per-profile capability sets (Bot Mode mapping is a follow-up).
- Changing the upstream RealtimeVoiceProvider discussion.

## Acceptance

1. Voice answers "what can you do" from the real catalog, with the
   delegation ceiling stated.
2. A computer-use ask produces a delegated run + a spoken approval
   round-trip (once/session/deny only) + narrated progress + spoken result.
3. A denied call steers to delegation instead of refusing flat.
4. Discord-lane transcripts persist at session end.
5. Suite + ruff + plugin-guard green; CHANGELOG 0.15.0.
