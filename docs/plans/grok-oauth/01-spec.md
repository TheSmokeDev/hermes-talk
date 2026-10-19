# Grok voice on a SuperGrok / X Premium+ subscription — spec

Status: implemented, release gated on the probe. 2026-09-01.
Owner repo: TheSmokeDev/hermes-talk. No core (NousResearch/hermes-agent) edits.

## Goal

`TALK_PROVIDER=grok` with no xAI key set and a `hermes auth add xai-oauth`
login just works. `hermes talk doctor` explains which lane won. A rejected or
tier-denied token becomes one operator line instead of an aiohttp traceback.

## Why now

hermes-talk 0.15.1 runs realtime voice on OpenAI (API key **or** the Codex /
ChatGPT subscription), Gemini (key), and Grok (key only). The product stance
is "subscriptions are the point; the user picks the battery." Grok was the one
provider still forcing a metered `XAI_API_KEY`.

The host already owns an xAI OAuth lane: `hermes auth add xai-oauth` is a
device-code login (~6h access tokens with refresh), and
`hermes_cli/auth.py:5209-5301 resolve_xai_oauth_runtime_credentials()`
refreshes under its own lock, quarantines dead tokens, and returns
`{"api_key": <access_token>, "provider": "xai-oauth", ...}`. hermes-talk does
not implement OAuth — it consumes the host lane exactly the way the Codex lane
consumes the Codex CLI's auth store.

## The probe gate (Gate 0)

One unproven assumption gates release: nobody has shown an xAI OAuth bearer
being accepted on `wss://api.x.ai/v1/realtime`. Every public voice integration
is key-only, and the host has a 403 → `xai_oauth_tier_denied` path that hits
paying users. So the lane ships behind a live probe:

```
TALK_PROVIDER=grok hermes talk doctor --probe
```

| Call | Expect | Meaning |
|---|---|---|
| `POST https://api.x.ai/v1/realtime/client_secrets` body `{"model":"grok-voice-latest"}` `Authorization: Bearer <access_token>` | 200 | the subscription reaches the realtime surface |
| WS `wss://api.x.ai/v1/realtime?model=grok-voice-latest` same bearer, first server event | 101 + `session.created` | the wire we actually use accepts it |

- 200 / 101 → tag `v0.16.0`.
- 401 → the token shape is wrong for this surface; check the login's scope
  carries `api:access` before concluding anything.
- 403 → voice stays keyed. Ship only the honest fallback (the 403 remediation
  string below, in doctor and at connect). No fake lane.

The probe prints status codes and the first event type only — never the
token, never an `auth.json` path.

## Design

Sibling of `talk_auth.py`; provider auth modules never import each other.
`TalkAuth` / `TalkAuthError` are imported from `talk_auth` so `talk_cli`'s
existing `except` tuple keeps catching Grok failures.

### `talk_grok_auth.py`

- `prefer_xai_oauth(env)` — strict parse of `TALK_PREFER_XAI_OAUTH`: absent →
  `False`; blank or garbage → `TalkAuthError` (fail closed, like
  `prefer_codex_oauth`).
- `resolve_grok_auth(*, env=None, hermes_home=None) -> TalkAuth`, in order:
  1. preference on → OAuth only; missing/unusable login refuses and says
     metered keys were not used.
  2. `TALK_XAI_API_KEY` (set-but-blank refuses).
  3. `XAI_API_KEY` (same rule).
  4. the host `xai-oauth` login.
- `_resolve_xai_oauth(hermes_home)`:
  - try-import `hermes_cli.auth.resolve_xai_oauth_runtime_credentials` inside
    the function; the host owns refresh and quarantine. A host `AuthError`
    becomes `TalkAuthError("xAI OAuth token is unusable; run \`hermes auth add
    xai-oauth\`")` — message only, never the host exception text (it can carry
    paths).
  - host not importable → read-only parse of `HERMES_HOME/auth.json` →
    `providers["xai-oauth"]["tokens"]["access_token"]`; a JWT `exp` in the past
    refuses with the relogin command.
  - nothing usable →
    `provider grok is selected but no xAI key is configured and no xAI OAuth
    login exists; set XAI_API_KEY or run \`hermes auth add xai-oauth\``.
- `grok_auth_diagnostic(*, env, hermes_home, now_s)` — read-only, never calls
  the host resolver (it may refresh and write). Keys: `configured`,
  `winning_lane`, `preference`, `xai_oauth ∈ {missing, invalid, expired,
  valid}`, `metered_key_present`, `metered_key_wins_over_oauth`,
  `metered_keys_ignored`, `refresh_required`, `blocked_by ∈
  {invalid-preference, xai-oauth-unusable, blank-talk-key, blank-xai-key,
  no-usable-auth}`. `refresh_required` mirrors the host skew: 120s when the
  token's lifetime is ≤ 45 min, else 3600s.

### `talk_grok_realtime.py`

`connect()` translates aiohttp's `WSServerHandshakeError` (matched by class
name + integer `status`, so tests need no aiohttp) by status and auth source:

| status | source `xai-oauth` | source key |
|---|---|---|
| 401 | `xAI OAuth token rejected — run \`hermes auth add xai-oauth\`` | `xAI API key rejected (401)` |
| 403 | `your xAI subscription tier does not include realtime API access; set \`XAI_API_KEY\` for Grok voice` | `xAI refused this key for realtime (403)` |

Every other exception keeps today's path. The wire header stays
`Authorization: Bearer <token>`. Caveat: the bearer is checked only at
handshake — a ~6h token outliving the socket is fine; a token that expires
before connect is what the diagnostic catches.

### `talk_cli.py`, `talk_doctor.py`, `talk_setup.py`

- `_grok_auth()` delegates to `resolve_grok_auth()`.
- Doctor's auth check branches on provider; Grok reports the winning lane and
  `xai-oauth=valid|expired|invalid|missing`, warns when a metered key wins over
  a valid login, and fails with the combined remediation only on
  `no-usable-auth`. `--probe` (grok only) is the single opt-in network call.
- Setup offers "xAI subscription" vs "xAI key" for Grok; the subscription
  choice writes `TALK_PREFER_XAI_OAUTH=true`; with no login it names the
  command and writes nothing.

## Testing

One test per path in `tests/test_grok_auth.py` (preference parse, blank keys,
scoped-beats-shared, key-beats-login, host resolver success/`AuthError`, file
fallback valid/missing/expired/unparseable, store byte-identical after every
call, every `blocked_by` reachable, `refresh_required` flip at both skews,
token absent from every receipt and error), plus the handshake matrix in
`tests/test_grok_realtime.py`, the doctor lane + `--probe` zero-network cases
in `tests/test_doctor.py`, and the setup choice in `tests/test_setup.py`.
CI: `pytest -q` + `ruff check .`.

## Security / scanner notes

- hermes-talk never writes `HERMES_HOME/auth.json` (tests assert byte-identity).
- The bearer only ever reaches `*.x.ai`.
- No failure path prints the token or a path that contains it.
- `TALK_PREFER_XAI_OAUTH=true` refuses metered fallback — a subscription user
  cannot be silently billed through a stray `XAI_API_KEY`.

## Non-goals (this release)

- Reading Grok Build CLI's `~/.grok/auth.json`.
- A device-code login inside the plugin — `hermes auth add xai-oauth` owns it.
- Any write to any auth store.
- The dashboard tab (`dashboard/plugin_api.py`) stays OpenAI-only.

## Acceptance

1. No login, no key → doctor fails with the combined remediation, exit
   non-zero, no traceback.
2. After `hermes auth add xai-oauth` → doctor passes, `winning_lane=xai-oauth`,
   no token printed.
3. `--probe` → the Gate 0 table. Green = tag; 403 = fallback wording only.
4. `TALK_PROVIDER=grok`, no key: a Discord session talks back;
   `auth_source=xai-oauth` in the wire log.
5. `XAI_API_KEY` also set → doctor warns "metered key wins";
   `TALK_PREFER_XAI_OAUTH=true` → the login wins.
6. A corrupted token in a copy of the store under `HERMES_HOME` → the 401
   remediation at session start; the original store untouched.

## Reference etiquette

- Host resolver: `hermes_cli/auth.py:5209-5301`
  (`resolve_xai_oauth_runtime_credentials`), `AuthError` at `:989`.
- Store: `%LOCALAPPDATA%\hermes\auth.json` on Windows, `~/.hermes/auth.json`
  elsewhere; provider key `xai-oauth`.
- Realtime: `https://api.x.ai/v1/realtime/client_secrets`,
  `wss://api.x.ai/v1/realtime?model=grok-voice-latest`.
