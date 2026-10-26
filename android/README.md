# Hermes Talk on Android

This directory holds the Android side of Talk, kept separate from the Hermes dashboard
plugin so it can ship (and be reviewed) on its own.

## `HermesTalk/` — installable Talk shell (phase 1)

A small, dependency-free Android app that hosts the existing **Talk dashboard surface**
(`/talk`, browser WebRTC + delegations) in a WebView and puts it on the phone's home screen
as **Hermes Talk**:

- microphone: the app holds `RECORD_AUDIO`, asks for it at first launch, and grants the
  page's `getUserMedia` permission through `WebChromeClient.onPermissionRequest`;
- dashboard login cookies persist in the app's WebView, so the dashboard sign-in happens once;
- the page is the real Talk surface served by the hermes-talk plugin, so target selection,
  delegated runs, steering and transcript all behave the same as on the PC;
- no changes to the official Hermes Agent app and no PairIP/Play-signature problems: this is a
  separate package (`dev.smokedev.hermes.talk`) installed side by side.

Default dashboard address is `https://your-dashboard.tailnet.ts.net/talk`; change it once with:

```powershell
adb shell am start -n dev.smokedev.hermes.talk/.MainActivity `
  --es url https://your-host.tailnet.ts.net/talk
```

### Remote dashboard token

The dashboard plugin keeps `/api/plugins/hermes-talk/*` loopback-only unless
`TALK_DASHBOARD_TOKEN` is set on the host. Once it is set, requests from other machines must
send it as `x-talk-token`. The app accepts it once as an intent extra and remembers it; it
injects it into the Talk page's `sessionStorage` (`hermes-talk-dashboard-token`) after the first
load and reloads once, so Talk authorizes over Tailscale without a manual paste:

```powershell
adb shell am start -n dev.smokedev.hermes.talk/.MainActivity `
  --es talk_token <TALK_DASHBOARD_TOKEN value>
```

Host side (done on this PC): `TALK_DASHBOARD_TOKEN` was added to the Hermes `.env` and the
dashboard restarted; `.env` backup lives under `.tools/backup/`.

### Build (portable toolchain already in the repo)

```powershell
pwsh -File android\HermesTalk\build.ps1
```

Uses the repo-local JDK 21 and Android SDK under `.tools/android-sdk` (build-tools 37.0.0,
platform android-37), writes `hermes-talk.apk` under `.tools/android-build/hermes-talk/`, then
signs with the existing Android debug keystore. Install with `adb install -r`.

### Phase 2 — native screen

Replace the WebView with a Compose screen that speaks the Talk lane directly (the dashboard
plugin's `/live/*` WebRTC transport and the GPT-Live protocol are the reference). The launcher
entry point, package name, and manifest stay, so the phase-1 app is a working day-one shippable
while the native body is built.

### Phase 3 — plugin fallback

If the native surface is not accepted upstream, the same WebView shell can be distributed as
the "install it yourself" app, and the dashboard plugin remains the PC-side counterpart.
