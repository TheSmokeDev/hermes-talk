<!--
Thanks for the PR. Fill the sections that apply and delete the ones that
do not. The guide behind every question: CONTRIBUTING.md.
Never paste a credential, a transcript, or audio into a PR.
-->

## What and why

<!-- The symptom or the gap, and why this approach. If a spoken sentence
changes, quote it before and after — the wording is a contract here. -->

Fixes #

## How to test

<!-- The command(s) and the expected output, so a reviewer reproduces it
without asking. For a bug: the reproduction on main, then the proof. -->

```bash

```

## Platforms

<!-- Where you ran it. CI covers ubuntu + windows × Python 3.11–3.13; say so if that is all you have. -->

- [ ] Windows
- [ ] Linux
- [ ] macOS

## Live receipt (required when a provider lane, credential resolution, or the delegation path is touched)

<!-- Paste the relevant part of `hermes talk check --json` — provider, plugin
and host versions, and each step's status. If the change cannot reach a
live turn, `hermes talk doctor --json` instead. Both redact by construction;
still read what you paste. A new or changed provider lane also wants a
filled-in docs/PROVIDER-RECEIPT.md table. -->

```json

```

## Checklist

- [ ] One logical change; tests ride with it, not behind it
- [ ] `pytest -q` and `ruff check .` pass locally on the pinned ruff (`pip install -e ".[dev]"` or `uv run --extra dev …`)
- [ ] Commits follow Conventional Commits with a hermes-talk scope (`fix(audio): …`, `feat(realtime): …`, `docs: …`)
- [ ] No auth-store writes outside the documented Codex refresh; tokens reach only the provider's own host
- [ ] Nothing secret in logs, receipts, spoken sentences, or test fixtures
- [ ] Every spoken sentence I added or changed claims only what an artifact proves
- [ ] Docs updated where behaviour changed (README, `docs/OPERATING.md`, `docs/VOICE-COMMANDS.md`) — or N/A
- [ ] `CHANGELOG.md` `[Unreleased]` entry added (name yourself — merged work is credited)
