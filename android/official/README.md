# Talk surface patched into the official Hermes Agent app

This directory is the **official-app** half of the Talk Android work: the patch source applied
to the real Hermes Agent APK pulled from the phone (not a separate shell app).

## What it does

`TalkActivity.java` (package `ai.nousresearch.hermes.mobile`) is a native entry point inside the
official app:

- hosts the hermes-talk dashboard surface (`/talk`) in an Android `WebView`;
- holds `RECORD_AUDIO` and grants the page's `getUserMedia` request, so Talk uses the **phone
  microphone**;
- persists the dashboard login cookie in the app's WebView;
- injects the optional remote-dashboard token into the page's sessionStorage
  (`hermes-talk-dashboard-token`) so Talk's non-loopback dashboard routes authorize;
- adds a second launcher icon **"Hermes Talk"** inside the official app package.

The official app's dashboard/provider auth (Codex OAuth for the realtime lane) stays host-side and
unchanged — the phone carries no model credentials.

## Build

Portable toolchain is already in the repo (`.tools/jdk21`, `.tools/android-sdk` build-tools 37 +
android-37, Apktool 2.11.1, baksmali 3.0.10).

```powershell
pwsh -File android\official\build-official-talk.ps1
```

Pipeline: `javac` → `d8` → `baksmali` → merge smali into
`.tools/decompile/apktool-src` (`smali_classes2`) → Apktool rebuild → merge the arm64 native
libraries from the Play split → zipalign → debug-sign.

Output: `.tools/decompile/hermes-agent-talk.apk` (47.5 MB, v3-signed; TalkActivity verified as a
launchable activity labelled "Hermes Talk").

## Install (destructive, explicit go required)

The Play build is signed with Google's app-signing key, so the patched build cannot update it in
place:

```powershell
$adb = <adb path from .tools\env.ps1>
$serial = '<adb serial from `adb devices`>'
& $adb -s $serial uninstall ai.nousresearch.hermes.mobile      # wipes app data/session
& $adb -s $serial install .tools\decompile\hermes-agent-talk.apk
& $adb -s $serial shell am start -n ai.nousresearch.hermes.mobile/.TalkActivity `
  --es talk_token <TALK_DASHBOARD_TOKEN>
```

Caveats:

- Uninstalling the Play build drops its local session/login; the phone has to sign in to the
  dashboard again (server secret is unchanged, so sessions survive a dashboard restart).
- The app is wrapped in **PairIP** (`com.pairip.application.Application`). A debug-signed rebuild
  may be refused or degraded by its licence check; this was not bypassed. Reinstall from Play
  (the account is whitelisted) if that happens.
- First launch of "Hermes Talk" needs the dashboard sign-in inside the WebView (one time);
  the talk token is then injected automatically.

## PR path

`TalkActivity.java` + a manifest entry is the entire upstream patch surface. When the official
source repo is available (it is not reachable from the current GitHub accounts — no
`NousResearch/*mobile*` repo is visible), the same file is added under the app's real module,
with a Talk launcher entry, and offered as a PR. The decompiled rebuild is a local test harness,
not the PR.
