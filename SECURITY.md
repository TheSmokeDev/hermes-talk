# Security policy

hermes-talk holds real credentials on the operator's behalf — an OpenAI key or
a ChatGPT (Codex) OAuth login, an xAI key or SuperGrok login, a Gemini key, an
ElevenLabs key — and it gives a voice model the authority to run Hermes tools
and hand work to background agents. Those two facts define what a security
bug is here.

## Supported versions

| Version | Supported |
|---|---|
| The latest release on [PyPI](https://pypi.org/project/hermes-talk/) | yes — fixes ship as a new release |
| `main` | yes — fixes land here first |
| Anything older | no — upgrade with `hermes plugins update hermes-talk`, then restart the gateway |

## Reporting a vulnerability

**Report privately** through GitHub's private vulnerability reporting:
[open a draft security advisory](https://github.com/TheSmokeDev/hermes-talk/security/advisories/new).
Do not open a public issue for a security bug, and do not paste credentials,
transcripts, or audio anywhere — a redacted `hermes talk diagnostics --bundle`
is the right artifact to attach if configuration matters to the report.

What to expect:

- **Acknowledgement within 72 hours** of the report.
- A fix on `main` and a PyPI release, with an advisory that credits you
  unless you ask otherwise. Coordinated disclosure: we ask that you hold the
  details until the release is out, and we will not sit on a fix to make that
  wait longer than it needs to be.
- If the problem is in Hermes Agent itself rather than this plugin, we will
  say so and point you at the upstream process instead of leaving the report
  in limbo.

## What counts

Reports in these classes get the fastest turnaround. Each one is an invariant
the code is built to hold, so a break is a bug by definition:

- **Credential leakage.** Any path where a provider key, an OAuth access or
  refresh token, or the ephemeral session secret reaches a log line, an HTTP
  response, a transcript, a run record, a doctor/check/diagnostics report, or
  a spoken sentence. The rule is that the raw credential is spent on exactly
  one upstream endpoint and appears nowhere else.
- **Auth-store writes.** Anything that writes to a credential store outside
  the one documented path (the Codex OAuth refresh that rewrites
  `auth.json` atomically with the same tokens the Codex CLI itself would
  write). `doctor`, `diagnostics`, and the core-contract availability probes
  are read-only by contract.
- **Tool-authority bypass.** A speaker, browser tab, or message that is not
  the operator getting a mutating tool (`delegate_task`, `steer_agent`,
  `redirect_agent`, `stop_work`, a spoken approval) to run: the Discord
  speaker-binding ledger, the per-turn permit, the dashboard's
  `TALK_DASHBOARD_TOKEN` / loopback gate, and the approval bridge's
  never-`always` ceiling are all in scope. So is background output wearing
  the operator's voice (a run result entering the conversation as a user
  turn instead of a contained system item).
- **Redaction failures.** A secret-shaped value or a filesystem path
  surviving the redaction in `hermes talk doctor`, `check`, or
  `diagnostics`, or a diagnostics bundle written with permissions wider than
  owner-only.
- **Supply chain.** Anything in the published wheel that is not in this
  repository, or a workflow that can be made to run untrusted code with a
  token that has write access.

Provider outages, rate limits, model behaviour, and "the voice said something
wrong" are bugs, not vulnerabilities — file them as
[ordinary issues](https://github.com/TheSmokeDev/hermes-talk/issues/new/choose).
