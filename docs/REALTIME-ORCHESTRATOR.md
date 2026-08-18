# Realtime Orchestrator — architecture map (on-demand context)

Written 2026-08-15 after wiring the full tool-calling lane live. Load this
instead of re-crawling the repos. Sources: hermes-talk @ `0e3b477` (main,
PRs #29/#30), hermes-agent fork branch `feat/realtime-execution-attachment-20260813`
merged into the live install checkout.

## The one-sentence version

The Realtime voice session is the Hermes manager: the provider's function
calls are projected from the **host-authorized tool schemas** and execute
through **canonical Hermes execution** — no second tool registry, no closed
handler table.

## Where the pieces live

### Plugin side (`hermes-talk`, installed at `%LOCALAPPDATA%\hermes\plugins\hermes-talk`)

- `__init__.py` — `register(ctx)`; captures the host's realtime execution
  attachment via `capture_realtime_execution_attachment` on the session
  invocation (~line 185).
- `talk_core_realtime.py` / `talk_core_session.py` — provider-neutral
  realtime session core; batches provider function calls into ONE canonical
  Hermes execution, returns ordered durable tool results, requests one
  continuation. Response-local cancellation + barge-in boundaries preserved.
- `talk_openai_realtime.py` — OpenAI Realtime provider (`gpt-realtime-2.1`,
  voice `cedar` on this machine).
- `talk_tools.py` — the curated Talk verbs that remain: `search_memory`,
  `search_vault`, `delegate_task`, `check_work`, `list_agents`,
  `steer_agent`, `redirect_agent`, `stop_work`, `talk_status`,
  `talk_capabilities`.
  (`talk_identity.py` renders the exact schema names into the session prompt
  so the persona knows its real surface.)
- `talk_discord.py` — Discord `/talk join` runs the SAME `run_talk_session`
  with `DiscordAudio` swapped in; identical tools/instructions. Mutating
  tools are gated by an immutable-ID operator allowlist.
- `talk_runs.py` — async run registry: slow work returns a spoken receipt
  (`WORK_STARTED #<id>`) and the watcher speaks the result when it lands.
- `talk_auth.py` — fail-closed auth order: `TALK_OPENAI_API_KEY` →
  `OPENAI_API_KEY` → Codex CLI OAuth (winning lane on this machine:
  `codex-oauth`).

### Host side (`hermes-agent`)

- `gateway/realtime_execution_attachment.py` — the canonical execution
  attachment the plugin captures. Added by fork branch
  `feat/realtime-execution-attachment-20260813`; **not yet upstream**
  (NousResearch PR pending — track the fork branch until it merges).
- `agent/external_tool_batch.py` + `gateway/external_tool_batch.py` — the
  batched canonical execution path for provider function calls.
- Plugin capability surface (upstream, current): `ctx.dispatch_tool`
  (hermes_cli/plugins.py ~2171) runs any host tool through normal
  approvals/redaction/budgets; `ctx.subagent_lifecycle`
  (agent/subagent_lifecycle.py) gives launch/status/wait/cancel/result;
  model-facing `delegate_task` (tools/delegate_tool.py) already exposes
  spawn / `{"action":"list"}` / `steer` / `stop` — the "subagent orchestra"
  primitives.
- Core has NO duplex realtime voice by design — plugin-owned Realtime
  session is the sanctioned shape (upstream docs steer voice to plugins).

### Live install layout (this machine)

- `HERMES_HOME=C:\Users\Degen\AppData\Local\hermes` (NOT `~/.hermes` — the
  `~/.hermes/plugins/hermes-talk` copy is a stale v0.6.1 leftover).
- Hermes venv: `%LOCALAPPDATA%\hermes\hermes-agent\venv`, installing from
  `C:\Users\Degen\isolated-dev\hermes-agent-prp005-desktop-realtime-20260809`
  (branch `feat/discord-core-session-runtime`, attachment branch merged
  2026-08-15, pre-merge HEAD `d03d60fe3` if rollback is needed).
- Dev clone of the plugin: `C:\Users\Degen\hermes-talk` (tracks origin/main).
- Gateway runs as `pythonw -m hermes_cli.main gateway run` (supervisor tree);
  after ANY plugin or host update it must be restarted — kill the specific
  PID only, never kill-all.

## Verify (no talking required)

```bash
hermes plugins list          # hermes-talk · enabled · 0.8.0
hermes talk doctor --json    # 8/8 pass, incl. "host exposes every Talk capability"
hermes talk &                # wire canary: established TLS :443 = session mints+connects
```

Test gate: in `C:\Users\Degen\hermes-talk` — `.venv/Scripts/python -m pytest -q`
(834 passed / 7 skipped at wiring time) + `ruff check`.

## Known limits

- Subagent completion announcements need
  `PluginContext.active_parent_session_id` (upstream PR #79716, pending).
  Below that, announcements are suppressed, not guessed.
- `redirect_agent`'s clean-abort path wants host ≥ 0.20 (install is 0.20.0).
- Live voice proof (speak a real tool request; Discord `/talk join`) needs a
  human at the mic — the canary proves everything short of speech.

## Next phase (agreed direction, not built)

Talk as ONE mountable capability, not separate voice brains: HUD voice
button, Bot Mode agent chat, desktop chat, Discord all attach the same Talk
lane with a per-surface context envelope (active profile/session, surface ID,
selected bot, available UI actions). Real mutations still travel through
canonical Hermes tools/approvals. Bot Mode (NousResearch/Hermes-Bot-Mode) is
a desktop `plugin.js` over `profiles.*` RPCs — reference for the surface
hooks, not something Talk duplicates.
