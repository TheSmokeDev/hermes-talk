# Native Phone mode for Hermes Talk

Owner: Pedro. Requested November 14, 2026. Goal closure requires Pedro's explicit confirmation.

## Outcome

One Hermes Talk installation provides normal direct voice and optional Dograh-backed
telephone calls. Phone configuration belongs to Hermes plugin settings. Call review,
approval, questions, answers and status belong to the existing Hermes conversation,
including Desktop/dashboard, Discord and clients viewing that conversation on mobile.
There is no second dashboard, app, custom chat UI or provider credential store.

The acceptance flow is: prepare an owned-number call in Hermes, approve it using the
host's existing controls, answer the telephone, receive a caller question in that
same Hermes conversation, answer there, and hear the approved answer on the call.

## Slice 1: Native plugin integration

- Carry the tested phone modules into `talk_phone`, with no account configuration,
  private identifiers, recordings or operational scripts in source.
- Register phone tools as a disabled-by-default toolset of `hermes-talk`; normal
  Talk registration must work without a Dograh account or a running phone server.
- Expose settings through the Hermes manifest, commands through the host command
  picker, and setup/doctor/bridge under `hermes talk phone`.
- Support explicit migration from the separate plugin in the current profile.
  Preserve its journal, invalidate previous grants and leave existing calls alone.
- Use the existing native choice/clarify surface for owner call review. No model
  can approve itself. Keep session binding, exact-number policy and one-use approval.
- Validate packaging, registration, optional-off behavior and ordinary Talk regressions.

## Slice 2: Same-conversation question and answer

- Capture a native `PluginContext.post_message` handle in the owning call turn.
  Publish bounded call status/questions through that handle's thread-safe edit API.
  The handle targets the original conversation; the bridge never chooses another chat.
- Reuse Hermes's native event/message delivery for local/Desktop/mobile viewers and
  messaging adapters. Never replace the response stream or open a separate interface.
- Pair the call through an authenticated owner command and verify the Dograh run.
  Surface caller content as untrusted. Only the authenticated owner releases an answer.
- Test delivery, refusal, expiration, isolation and answer return over real local MCP/HTTP.
- Exercise the integrated candidate in the existing isolated lab with an authorized
  owned-number call. Retain the exact version, carrier completion, question delivery,
  owner answer and spoken answer evidence in the local work checklist.

## Boundaries

Phone mode is optional. It uses the configured Dograh account/carrier; installing
Talk does not purchase numbers, dial, start a public tunnel or change business lines.
Native host APIs are feature-detected and fail closed when the requested operation
cannot be represented. Caller speech never becomes owner authority.

The current primary checkout's uncommitted Desktop work is preserved. Implementation
uses the existing `fix-live-replay-ci` worktree on `codex/phone-mode`, based on
`029ccedf52313afe67061ad8ad0b0b2239a3b0fa`. No new checkout was created.

## Sources

- Hermes plugin guide and public API: <https://hermes-agent.nousresearch.com/docs/developer-guide/plugins>
- Native messages and choices: pinned Hermes 0.23.0 `PluginContext.post_message`,
  `PluginContext.ask_choice`, and their host implementations.
- Existing Talk: `__init__.py`, `talk_cli.py`, `talk_tools.py`, `talk_host.py`,
  `talk_operator_auth.py`, `talk_native_api.py` and `docs/DESKTOP.md`.
- Existing phone implementation: private owner repository `hermes-dograh`,
  implementation `119ebbd11e481e1be19aba0a373a4c0199966eb3`.

Execution evidence and remaining actions live in the existing local WORK-STATUS.md.
