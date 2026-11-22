// Icons are codicon glyphs drawn by CSS, so a control's text stays its exact
// accessible name and no icon node sits between the label and its button.
const DESKTOP_TALK_VIEW_CSS = `
.ht-desktop-view {
  --htd-fg: var(--ui-text-primary, currentColor);
  --htd-fg-2: var(--ui-text-secondary, color-mix(in srgb, currentColor 74%, transparent));
  --htd-fg-3: var(--ui-text-tertiary, color-mix(in srgb, currentColor 54%, transparent));
  --htd-line: var(--ui-stroke-tertiary, color-mix(in srgb, currentColor 10%, transparent));
  --htd-line-2: var(--ui-stroke-secondary, color-mix(in srgb, currentColor 16%, transparent));
  --htd-hover: var(--chrome-action-hover, color-mix(in srgb, currentColor 6%, transparent));
  --htd-fill: var(--ui-bg-quaternary, color-mix(in srgb, currentColor 4%, transparent));
  --htd-fill-2: var(--ui-bg-secondary, color-mix(in srgb, currentColor 12%, transparent));
  --htd-accent: var(--ui-accent, var(--theme-primary, #4a84fe));
  --htd-danger: var(--dt-destructive, #f85149);
  --htd-ok: var(--ui-success, #2ea043);
  --htd-ink: var(--foreground, var(--htd-fg));
  --htd-paper: var(--background, Canvas);
  --htd-ease: cubic-bezier(.2, .7, .2, 1);
  display:grid; gap:.875rem; min-width:0; color:var(--htd-fg); font:inherit; font-size:.8125rem; line-height:1.45;
}
.ht-desktop-view p, .ht-desktop-view h2, .ht-desktop-view h3 { margin:0; }
.ht-desktop-view .htd-sr { position:absolute; width:1px; height:1px; margin:-1px; padding:0; overflow:hidden; clip-path:inset(50%); white-space:nowrap; border:0; }
.ht-desktop-view .htd-i::before, .ht-desktop-view .htd-orb::before, .ht-desktop-view summary::before,
.ht-desktop-view .htd-alert-title::before { font:normal normal normal 16px/1 codicon; display:inline-block; -webkit-font-smoothing:antialiased; text-decoration:none; }
.ht-desktop-view .htd-i-mic::before { content:"\\ec12"; }
.ht-desktop-view .htd-i-pause::before { content:"\\ead1"; }
.ht-desktop-view .htd-i-play::before { content:"\\eb2c"; }
.ht-desktop-view .htd-i-refresh::before { content:"\\eb37"; font-size:14px; }
.ht-desktop-view .htd-i-attach::before { content:"\\ec34"; }
.ht-desktop-view .htd-i-close::before { content:"\\ea76"; font-size:14px; }
.ht-desktop-view .htd-i-minimize::before { content:"\\eaba"; }
.ht-desktop-view .htd-i-send::before { content:"\\ec0f"; font-size:14px; }

.ht-desktop-view .htd-head { display:flex; align-items:center; gap:.5rem; min-width:0; }
.ht-desktop-view .htd-head h2 { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.8125rem; font-weight:600; }
.ht-desktop-view span.htd-badge:not([data-slot]) { display:inline-flex; align-items:center; gap:.25rem; padding:.125rem .375rem; border:1px solid var(--htd-line-2); border-radius:3px; color:var(--htd-fg-3); font-size:.65rem; font-weight:500; white-space:nowrap; }
.ht-desktop-view .htd-provider::before { content:""; width:.375rem; height:.375rem; border-radius:50%; background:var(--htd-fg-3); transition:background-color .2s; }
.ht-desktop-view .htd-provider[data-live="true"]::before { background:var(--htd-ok); box-shadow:0 0 0 2px color-mix(in srgb, var(--htd-ok) 22%, transparent); }
.ht-desktop-view .htd-icon-btn { flex:none; width:1.5rem; height:1.5rem; padding:0; display:inline-grid; place-items:center; border-radius:3px; }

.ht-desktop-view .htd-hero { display:grid; justify-items:center; gap:.5rem; padding:.25rem 0 .125rem; }
.ht-desktop-view .htd-orb-row { display:flex; align-items:center; justify-content:center; gap:1.25rem; }
.ht-desktop-view .htd-orb-wrap { position:relative; display:grid; place-items:center; width:5.5rem; height:5.5rem; }
.ht-desktop-view .htd-halo, .ht-desktop-view .htd-ring { position:absolute; border-radius:50%; pointer-events:none; }
.ht-desktop-view .htd-halo { inset:0; background:var(--htd-accent); opacity:0; transform:scale(.72); transition:transform .28s var(--htd-ease), opacity .28s var(--htd-ease), background-color .28s; }
.ht-desktop-view .htd-ring { inset:.375rem; border:2px solid transparent; opacity:0; transition:opacity .2s; }
.ht-desktop-view .htd-orb { position:relative; width:4rem; height:4rem; padding:0; display:grid; place-items:center; border:0; border-radius:50%; cursor:pointer; color:var(--htd-paper); background:var(--htd-ink); box-shadow:0 1px 2px rgb(0 0 0 / .22), 0 8px 24px -10px rgb(0 0 0 / .55); transition:background-color .22s var(--htd-ease), color .22s, transform .16s var(--htd-ease), opacity .22s, box-shadow .22s; }
.ht-desktop-view .htd-orb::before { content:"\\ec1c"; font-size:26px; transition:transform .22s var(--htd-ease); }
.ht-desktop-view .htd-orb:hover:not(:disabled) { transform:scale(1.04); }
.ht-desktop-view .htd-orb:active:not(:disabled) { transform:scale(.96); }
.ht-desktop-view .htd-orb:disabled { cursor:default; opacity:.32; box-shadow:none; }
.ht-desktop-view .htd-orb:focus-visible { outline:2px solid var(--htd-accent); outline-offset:5px; }
.ht-desktop-view [data-state="connecting"] .htd-orb { color:var(--htd-accent); background:color-mix(in srgb, var(--htd-accent) 16%, var(--htd-fill)); box-shadow:none; }
.ht-desktop-view [data-state="connecting"] .htd-orb::before { content:"\\ea76"; font-size:22px; }
.ht-desktop-view [data-state="connecting"] .htd-ring { opacity:1; border-top-color:var(--htd-accent); border-right-color:var(--htd-accent); animation:htd-spin .9s linear infinite; }
.ht-desktop-view [data-live="true"] .htd-orb, .ht-desktop-view [data-state="muted"] .htd-orb,
.ht-desktop-view [data-state="sleeping"] .htd-orb { color:#fff; background:var(--htd-accent); }
.ht-desktop-view [data-live="true"] .htd-orb::before, .ht-desktop-view [data-state="muted"] .htd-orb::before,
.ht-desktop-view [data-state="sleeping"] .htd-orb::before { content:""; width:1.125rem; height:1.125rem; border-radius:3px; background:currentColor; }
.ht-desktop-view [data-live="true"] .htd-halo { opacity:.14; transform:scale(.86); }
.ht-desktop-view [data-state="hearing"] .htd-halo { opacity:.24; transform:scale(.96); transition-duration:.12s; }
.ht-desktop-view [data-state="speaking"] .htd-halo { opacity:.3; transform:scale(1); }
.ht-desktop-view [data-state="speaking"] .htd-ring { opacity:.5; border-color:color-mix(in srgb, var(--htd-accent) 55%, transparent); }
.ht-desktop-view [data-state="thinking"] .htd-ring { opacity:.8; border-color:transparent; border-top-color:color-mix(in srgb, var(--htd-accent) 70%, transparent); animation:htd-spin 1.6s linear infinite; }
.ht-desktop-view [data-state="muted"] .htd-orb { background:color-mix(in srgb, var(--htd-accent) 38%, var(--htd-fill-2)); }
.ht-desktop-view [data-state="sleeping"] .htd-orb { background:var(--htd-fill-2); color:var(--htd-fg-2); box-shadow:none; }
.ht-desktop-view [data-state="error"] .htd-ring { opacity:1; border-color:color-mix(in srgb, var(--htd-danger) 60%, transparent); }
.ht-desktop-view[data-animate="true"] [data-state="listening"] .htd-ring,
.ht-desktop-view[data-animate="true"] [data-state="hearing"] .htd-ring { border-color:color-mix(in srgb, var(--htd-accent) 50%, transparent); animation:htd-ripple 2.4s var(--htd-ease) infinite; }
.ht-desktop-view[data-animate="true"] [data-state="speaking"] .htd-ring { animation:htd-ripple 1.4s var(--htd-ease) infinite; }
.ht-desktop-view .htd-side { position:relative; width:2.25rem; height:2.25rem; padding:0; display:grid; place-items:center; border-radius:50%; color:var(--htd-fg-2); background:transparent; box-shadow:inset 0 0 0 1px var(--htd-line-2); animation:htd-pop .2s var(--htd-ease); }
.ht-desktop-view .htd-side:hover:not(:disabled) { color:var(--htd-fg); background:var(--htd-hover); }
.ht-desktop-view .htd-side[aria-pressed="true"] { color:var(--htd-paper); background:var(--htd-ink); box-shadow:none; }
.ht-desktop-view .htd-side.htd-i-mic[aria-pressed="true"] { color:var(--htd-danger); background:color-mix(in srgb, var(--htd-danger) 14%, transparent); box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--htd-danger) 45%, transparent); }
.ht-desktop-view .htd-side.htd-i-mic[aria-pressed="true"]::after { content:""; position:absolute; width:1.125rem; height:1.5px; border-radius:1px; background:currentColor; transform:rotate(-45deg); }
.ht-desktop-view .htd-status-line { min-height:1.25rem; font-weight:500; text-align:center; color:var(--htd-fg); }
.ht-desktop-view [data-state="checking"] + .htd-status-line, .ht-desktop-view .htd-status-line[data-quiet="true"] { color:var(--htd-fg-3); }

.ht-desktop-view .htd-alert { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:.375rem .75rem; padding:.625rem .75rem; border:1px solid color-mix(in srgb, var(--htd-danger) 34%, transparent); border-radius:4px; background:color-mix(in srgb, var(--htd-danger) 7%, transparent); animation:htd-reveal .2s var(--htd-ease); }
.ht-desktop-view .htd-alert[data-tone="info"] { border-color:var(--htd-line-2); background:var(--htd-fill); }
.ht-desktop-view .htd-alert-title { display:flex; align-items:flex-start; gap:.5rem; min-width:0; font-weight:500; overflow-wrap:anywhere; }
.ht-desktop-view .htd-alert-title::before { content:"\\ea6c"; flex:none; font-size:14px; line-height:1.3; color:var(--htd-danger); }
.ht-desktop-view .htd-alert[data-tone="info"] .htd-alert-title::before { content:"\\ea74"; color:var(--htd-fg-3); }
.ht-desktop-view .htd-alert > details { grid-column:1 / -1; }
.ht-desktop-view .htd-alert > details > summary { width:max-content; padding:0; font-size:.6875rem; color:var(--htd-fg-3); }
.ht-desktop-view .htd-alert > details > .htd-text { margin-top:.375rem; color:var(--htd-fg-2); font-size:.75rem; }

.ht-desktop-view .htd-section { display:grid; gap:.5rem; min-width:0; }
.ht-desktop-view .htd-section-label { font-size:.625rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase; color:var(--htd-fg-3); }
.ht-desktop-view .htd-muted { color:var(--htd-fg-3); font-size:.75rem; overflow-wrap:anywhere; }
.ht-desktop-view .htd-text { white-space:pre-wrap; overflow-wrap:anywhere; }
.ht-desktop-view .htd-card { display:grid; gap:.375rem; min-width:0; padding:.625rem .75rem; border:1px solid var(--htd-line); border-radius:4px; background:var(--htd-fill); }
.ht-desktop-view[data-skin="quiet"] .htd-card { border-color:transparent; }
.ht-desktop-view .htd-card-head { display:flex; align-items:flex-start; gap:.5rem; min-width:0; }
.ht-desktop-view .htd-card-head > .htd-text { flex:1; min-width:0; font-weight:500; }
.ht-desktop-view .htd-chip { flex:none; padding:.0625rem .375rem; border-radius:3px; font-size:.625rem; font-weight:600; white-space:nowrap; color:var(--htd-fg-2); background:var(--htd-fill-2); }
.ht-desktop-view .htd-chip[data-tone="live"] { color:var(--htd-accent); background:color-mix(in srgb, var(--htd-accent) 14%, transparent); }
.ht-desktop-view .htd-chip[data-tone="ok"] { color:var(--htd-ok); background:color-mix(in srgb, var(--htd-ok) 14%, transparent); }
.ht-desktop-view .htd-chip[data-tone="bad"] { color:var(--htd-danger); background:color-mix(in srgb, var(--htd-danger) 14%, transparent); }
.ht-desktop-view .htd-controls { display:flex; flex-wrap:wrap; align-items:center; gap:.375rem; }
.ht-desktop-view .htd-captions { display:flex; flex-direction:column-reverse; gap:.5rem; max-height:11rem; overflow-y:auto; padding:.5rem .625rem; border-radius:4px; background:var(--htd-fill); }
.ht-desktop-view .htd-caption { display:grid; gap:.0625rem; }
.ht-desktop-view .htd-caption > span { font-size:.625rem; font-weight:600; letter-spacing:.04em; text-transform:uppercase; color:var(--htd-fg-3); }
.ht-desktop-view .htd-caption[data-role="user"] > span { color:var(--htd-accent); }
.ht-desktop-view .htd-jobs, .ht-desktop-view .htd-history, .ht-desktop-view .htd-result { max-height:20rem; overflow:auto; }
.ht-desktop-view .htd-jobs { display:grid; gap:.5rem; max-height:28rem; }
.ht-desktop-view .htd-approval { border-color:color-mix(in srgb, var(--htd-accent) 40%, transparent); background:color-mix(in srgb, var(--htd-accent) 6%, transparent); }
.ht-desktop-view .htd-approval h3 { font-size:.75rem; font-weight:600; }

.ht-desktop-view details.htd-more { border-top:1px solid var(--htd-line); }
.ht-desktop-view details.htd-more + details.htd-more { margin-top:-.875rem; }
.ht-desktop-view summary { display:flex; align-items:center; gap:.375rem; list-style:none; cursor:pointer; user-select:none; border-radius:3px; }
.ht-desktop-view summary::-webkit-details-marker { display:none; }
.ht-desktop-view summary::before { content:"\\eab6"; font-size:12px; color:var(--htd-fg-3); transition:transform .15s var(--htd-ease); }
.ht-desktop-view details[open] > summary::before { transform:rotate(90deg); }
.ht-desktop-view summary:focus-visible { outline:2px solid var(--htd-accent); outline-offset:2px; }
.ht-desktop-view details.htd-more > summary { padding:.625rem .125rem; font-size:.75rem; font-weight:500; color:var(--htd-fg-2); }
.ht-desktop-view details.htd-more > summary:hover { color:var(--htd-fg); }
.ht-desktop-view .htd-summary-hint { margin-left:auto; max-width:60%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:400; color:var(--htd-fg-3); }
.ht-desktop-view details.htd-more > .htd-body { display:grid; gap:.75rem; padding:0 .125rem .75rem; animation:htd-reveal .18s var(--htd-ease); }

.ht-desktop-view label { display:grid; gap:.3125rem; min-width:0; font-size:.6875rem; font-weight:500; color:var(--htd-fg-3); }
.ht-desktop-view select { width:100%; min-width:0; height:1.75rem; padding:0 .375rem; color:var(--htd-fg); background-color:var(--htd-fill); border:1px solid var(--htd-line-2); border-radius:3px; font:inherit; font-size:.75rem; font-weight:400; }
.ht-desktop-view select option { color:CanvasText; background-color:Canvas; }
.ht-desktop-view select:disabled { opacity:.5; }
.ht-desktop-view textarea:not([data-slot]) { width:100%; min-height:4.5rem; resize:vertical; padding:.5rem; color:inherit; background:var(--htd-fill); border:1px solid var(--htd-line-2); border-radius:3px; font:inherit; }
.ht-desktop-view .htd-route { display:flex; align-items:center; gap:.375rem; min-width:0; }
.ht-desktop-view .htd-route > select { flex:1; }
.ht-desktop-view .htd-route-label { flex:none; font-size:.6875rem; font-weight:500; color:var(--htd-fg-3); }
.ht-desktop-view .htd-route-value { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--htd-fg-2); }
.ht-desktop-view .htd-composer-row { display:flex; align-items:center; gap:.5rem; }
.ht-desktop-view .htd-composer-row > .htd-muted { flex:1; min-width:0; }
.ht-desktop-view .htd-file { position:relative; display:inline-grid; place-items:center; width:1.75rem; height:1.75rem; border-radius:3px; color:var(--htd-fg-2); cursor:pointer; }
.ht-desktop-view .htd-file:hover, .ht-desktop-view .htd-file:focus-within { color:var(--htd-fg); background:var(--htd-hover); }
.ht-desktop-view .htd-file input { position:absolute; inset:0; opacity:0; cursor:pointer; }
.ht-desktop-view .htd-attachments { display:flex; flex-wrap:wrap; gap:.375rem; margin:0; padding:0; list-style:none; }
.ht-desktop-view .htd-attachment { display:flex; align-items:center; gap:.375rem; max-width:100%; padding:.25rem .25rem .25rem .5rem; border:1px solid var(--htd-line-2); border-radius:4px; font-size:.75rem; }
.ht-desktop-view .htd-attachment img { width:1.5rem; height:1.5rem; object-fit:cover; border-radius:2px; }
.ht-desktop-view .htd-badges { display:flex; flex-wrap:wrap; gap:.25rem; }
.ht-desktop-view .htd-toggle { display:flex; align-items:center; justify-content:space-between; gap:.75rem; font-size:.75rem; font-weight:400; color:var(--htd-fg-2); cursor:pointer; }
.ht-desktop-view .htd-toggle input { appearance:none; position:relative; flex:none; width:1.75rem; height:1rem; margin:0; border-radius:999px; background:var(--htd-fill-2); cursor:pointer; transition:background-color .15s; }
.ht-desktop-view .htd-toggle input::after { content:""; position:absolute; top:2px; left:2px; width:.75rem; height:.75rem; border-radius:50%; background:var(--htd-fg-2); transition:transform .15s var(--htd-ease), background-color .15s; }
.ht-desktop-view .htd-toggle input:checked { background:var(--htd-accent); }
.ht-desktop-view .htd-toggle input:checked::after { transform:translateX(.75rem); background:#fff; }
.ht-desktop-view .htd-owner { font-size:.6875rem; color:var(--htd-fg-3); overflow-wrap:anywhere; }
.ht-desktop-view :focus-visible { outline:2px solid var(--htd-accent); outline-offset:2px; }
.ht-desktop-view[data-skin="contrast"] { --htd-fg:CanvasText; --htd-fg-2:CanvasText; --htd-fg-3:CanvasText; --htd-line:CanvasText; --htd-line-2:CanvasText; --htd-ink:CanvasText; --htd-paper:Canvas; --htd-accent:Highlight; color:CanvasText; background:Canvas; }
@keyframes htd-spin { to { transform:rotate(360deg); } }
@keyframes htd-ripple { 0% { transform:scale(.92); opacity:.7; } 100% { transform:scale(1.3); opacity:0; } }
@keyframes htd-pop { from { opacity:0; transform:scale(.85); } }
@keyframes htd-reveal { from { opacity:0; transform:translateY(-2px); } }
@media (prefers-reduced-motion:reduce) {
  .ht-desktop-view *, .ht-desktop-view *::before, .ht-desktop-view *::after { animation:none !important; transition:none !important; }
  .ht-desktop-view [data-state="connecting"] .htd-ring, .ht-desktop-view [data-state="thinking"] .htd-ring { border-style:dashed; border-color:var(--htd-accent); }
}
`;

function desktopTalkNotice(value, needsToken, lane) {
  if (!value && !needsToken) return null;
  const message = String(value?.message || value || '');
  const notice = desktopTalkNoticeText(message, needsToken, lane);
  const code = desktopTalkErrorCode(message);
  return code ? { ...notice, code } : notice;
}

// Desktop plugin requests travel over IPC, so devtools shows no request to
// inspect. A refused request arrives as "STATUS: body"; its code is a fixed
// identifier that names what failed. The free text stays out of the view.
function desktopTalkErrorCode(message) {
  const refused = /^(\d{3}):\s*(\{[\s\S]*\})$/.exec(String(message || ''));
  if (!refused) return '';
  try {
    const code = JSON.parse(refused[2])?.detail?.code;
    return typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code)
      ? refused[1] + ' ' + code : '';
  } catch (_) {
    return '';
  }
}

// Each notice has a one-line title. `text` is the full guidance; the view shows
// it behind Details only when it says more than the title.
function desktopTalkNoticeText(message, needsToken, lane) {
  const realtime = /^Realtime error \[([a-zA-Z0-9_.\[\]-]{1,96});([a-zA-Z0-9_.\[\]-]{1,96})\]:/.exec(message);
  if (realtime) {
    return { title: 'The voice provider refused a request.',
      text: 'The voice provider rejected a request (' + realtime[1] +
      (realtime[2] === 'unknown' ? '' : ', ' + realtime[2]) + '). Retry, or include this code in your bug report.', retry: true };
  }
  // Sentences Talk writes for the operator are already the notice.
  if (/^GPT-Live needs a Hermes host with task context support/.test(message)) {
    return { title: "GPT-Live can't attach on this host.", text: message, retry: false };
  }
  if (/^Send one message in this conversation first/.test(message)) {
    return { title: 'Send a message here first.', text: message, retry: true, tone: 'info' };
  }
  if (/^404\b/.test(message) && /target_missing/.test(message)) {
    return { title: 'Send a message here first.',
      text: 'Send one message in this conversation first, then try again. ' +
      'Hermes Desktop saves a conversation on its first message.', retry: true, tone: 'info' };
  }
  if (lane === 'stock' && (needsToken || /^(?:401|403)\b/.test(message))) {
    return { title: 'A dashboard token is blocking Talk.',
      text: 'This Hermes Desktop cannot send TALK_DASHBOARD_TOKEN. ' +
      'Unset it for local Desktop use, or use the dashboard Talk tab.', retry: false };
  }
  if (needsToken || /^(?:401|403)\b/.test(message)) {
    return { text: 'Reconnect to this Hermes connection and try again.' };
  }
  if (/NotAllowedError|PermissionDenied|permission denied|microphone.*(?:denied|blocked)|(?:denied|blocked).*microphone/i.test(message)) {
    return { title: 'Microphone access is blocked.',
      text: 'Allow microphone access for Hermes in your system settings, then try again.', retry: true };
  }
  if (/NotFoundError|DevicesNotFound|no microphone|microphone.*not found/i.test(message)) {
    return { title: 'No microphone found.', text: 'Connect a microphone, then try again.', retry: true };
  }
  if (/NotReadableError|TrackStartError|microphone.*(?:in use|busy)|other voice session/i.test(message)) {
    return { title: 'Your microphone is busy.',
      text: 'Stop the other voice session using your microphone, then try again.', retry: true };
  }
  if (/\b503\b|temporarily unavailable|service_unavailable/i.test(message)) {
    return { text: 'Talk is temporarily unavailable. Try again.', retry: true };
  }
  return { text: 'Talk could not complete this request. Try again.', retry: true };
}

function desktopTalkSource(source) {
  if (['codex-oauth', 'subscription'].includes(source)) return 'ChatGPT';
  if (['configured', 'env', 'api'].includes(source)) return 'OpenAI API';
  return 'Hermes voice';
}

function desktopTalkLiveLabel(live, audioActivity) {
  if (audioActivity?.output === true) return 'Hermes is speaking';
  if (audioActivity?.input === true) return 'Hearing you';
  if (/^Thinking|^Processing/i.test(live)) return 'Thinking…';
  if (/^Using /i.test(live)) return 'Working on it…';
  if (/checking the request/i.test(live)) return 'Checking your request…';
  if (/returned a task decision/i.test(live)) return 'Update ready';
  if (/^Listening/i.test(live)) return 'Listening…';
  return 'Connected';
}

function desktopTalkJobLabel(status) {
  return ({ queued: 'Queued', pending: 'Waiting', accepted: 'Accepted', running: 'In progress',
    completed: 'Complete', succeeded: 'Complete', failed: 'Failed', cancelled: 'Cancelled',
    waiting_approval: 'Needs approval', waiting_for_approval: 'Needs approval',
    approval_required: 'Needs approval', paused: 'Paused' })[status]
    || 'Waiting for an update';
}

function desktopTalkJobTone(status) {
  if (['completed', 'succeeded'].includes(status)) return 'ok';
  if (['failed', 'cancelled'].includes(status)) return 'bad';
  if (['queued', 'pending', 'accepted', 'running', 'waiting_approval', 'waiting_for_approval',
    'approval_required'].includes(status)) return 'live';
  return '';
}

function desktopTalkRecipientLabel(recipient) {
  const app = ({ codex_desktop: 'Codex Desktop', claude_code: 'Claude Code',
    codex_worker: 'Codex worker', hermes_task: 'Hermes' })[recipient.app] || recipient.app;
  return [recipient.title || 'Untitled task', app, recipient.host_id, recipient.task_id,
    recipient.recipient_id].filter(Boolean).join(' · ');
}

function desktopTalkPresentationLabel(presentation) {
  return ({ result_ready: 'Result ready', unclaimed: 'Waiting for a summary',
    claimed: 'Summary queued', submitting: 'Submitting summary',
    context_submitted: 'Summary context submitted', playback_started: 'Summary playback started',
    playback_finished: 'Summary playback finished', interrupted: 'Summary interrupted',
    unknown: 'Summary audio state unknown', deferred: 'Summary waiting for a quiet moment'
  })[presentation?.state] || 'Summary audio state unknown';
}

function desktopTalkBytes(size) {
  if (size < 1024) return size + ' B';
  if (size < 1024 * 1024) return (size / 1024).toFixed(size < 10240 ? 1 : 0) + ' KB';
  return (size / 1024 / 1024).toFixed(1) + ' MB';
}

export function DesktopTalkView(props) {
  const h = React.createElement;
  const { status, loading, ready, active, starting, live, error, catalogError, needsToken,
    selectedTask, taskState, typed = '', sending, switching, returnDepth, voice = '',
    startTalk, stopTalk, refresh, refreshCatalog, setTyped, sendTyped, switchTarget,
    showResult, saveUpdatePreference, setVoice, voiceOwner, selectedRecipient = '',
    recipientOperation = 'message', recipientQuery = '', setRecipientQuery, setRecipient,
    setRecipientOperation, readRecipient, recipientHistory, recipientLoading,
    attachments = [], addAttachments, removeAttachment, canSendTyped = active,
    muted, sleeping, setMuted, setSleeping, collapse, appearance = {}, setAppearance,
    cancelJob, steerJob, answerApproval, replayResult, pendingActions = {}, lane, hostNote } = props;
  const tasks = (props.tasks || []).filter(task => typeof task?.target_id === 'string' && task.target_id);
  const selected = tasks.find(task => task.target_id === selectedTask);
  const conversation = selected?.label || taskState?.task?.label || 'This conversation';
  const allJobs = (taskState?.jobs || []).filter(job => job?.run_id != null);
  const jobs = allJobs.slice().sort((left, right) =>
    Number(!left.approval?.approvals?.length) - Number(!right.approval?.approvals?.length))
    .slice(0, 8);
  const visibleJobIds = new Set(jobs.map(job => String(job.run_id)));
  const results = Object.entries(props.results || {}).sort(([left], [right]) =>
    Number(!visibleJobIds.has(left)) - Number(!visibleJobIds.has(right))).slice(0, 8);
  const captions = (props.transcript || []).filter(row => typeof row?.text === 'string' && row.text.length);
  const voices = (status?.voices || []).filter(name => typeof name === 'string');
  const notice = desktopTalkNotice(error, needsToken, lane);
  // A host without task context cannot bind a conversation, list recipients or
  // switch conversations. Its refusals are expected, so they are not errors here.
  const taskContext = status?.taskContinuity?.supported !== false;
  const catalogNotice = taskContext ? desktopTalkNotice(catalogError, false, lane) : null;
  const button = (label, onClick, options = {}) => h(HermesSDK.Button,
    { ...options, type: 'button', onClick }, label);
  // Icon buttons keep their full label as the only child, so it stays the
  // accessible name; the glyph comes from the icon class.
  const iconButton = (label, icon, onClick, options = {}) => h(HermesSDK.Button,
    { variant: 'ghost', size: 'icon-sm', ...options, type: 'button', title: label, onClick,
      className: ['htd-i', 'htd-i-' + icon, options.className].filter(Boolean).join(' ') },
    h('span', { className: 'htd-sr' }, label));
  const Badge = HermesSDK.Badge || 'span';
  const busy = starting || switching;
  const inputOperations = props.inputCapabilities?.operations || [];
  const recipients = (props.recipients || []).filter(row => typeof row?.recipient_id === 'string');
  const recipient = recipients.find(row => row.recipient_id === selectedRecipient);
  // start_worker starts a Talk-owned worker on the pinned task. It is never
  // addressed to a recipient, so the send must not display one.
  const addressed = recipientOperation === 'start_worker' ? null : recipient;
  const matchingRecipients = recipients.filter(row => desktopTalkRecipientLabel(row).toLowerCase()
    .includes(recipientQuery.trim().toLowerCase()));
  const recipientChoices = recipient && !matchingRecipients.includes(recipient)
    ? [recipient, ...matchingRecipients] : matchingRecipients;
  const recipientAvailable = !!recipient && recipient.available !== false;
  const readable = recipientAvailable && recipient.operations?.includes('history');
  const messageable = !selectedRecipient || (recipientAvailable && !recipient.read_only &&
    recipient.send_agent_message === 'direct' && recipient.proven_control !== 'none');
  const selectedJob = allJobs.find(job => job.run_id === props.selectedJob);
  const steerable = inputOperations.includes('steer') && (selectedJob?.steering?.supported === true ||
    (recipientAvailable && recipient.app === 'codex_worker' && !recipient.read_only &&
      recipient.operations?.includes('steer_work')));
  const operations = { read: readable, message: messageable,
    start_worker: inputOperations.includes('start_worker'), steer: steerable };
  const operationAvailable = operations[recipientOperation] === true;
  // The action list matters only when there is a choice to make or a
  // non-default action is already chosen.
  const showOperations = setRecipientOperation && (recipientOperation !== 'message' ||
    !!selectedRecipient || Object.values(operations).filter(Boolean).length > 1);
  const sendDisabled = !canSendTyped || !operationAvailable || recipientOperation === 'read' ||
    sending || busy || (!typed.trim() && !attachments.length) ||
    (attachments.length > 0 &&
      (props.attachmentsSupported !== true || recipientOperation !== 'start_worker'));
  const receiveFiles = files => {
    const selectedFiles = Array.from(files || []);
    if (selectedFiles.length && addAttachments && !sending) addAttachments(selectedFiles);
    return selectedFiles.length > 0;
  };
  const actionError = (key, message) => pendingActions[key]?.error &&
    h('p', { className: 'htd-muted', role: 'alert' }, message);
  const audioState = sleeping ? 'sleeping' : muted ? 'muted' : active ? 'active' : 'off';
  const liveAudio = Boolean(active && !muted && !sleeping);
  const orbState = loading ? 'checking' : starting ? 'connecting' : active
    ? (sleeping ? 'sleeping' : muted ? 'muted' : props.audioActivity?.output === true ? 'speaking'
      : props.audioActivity?.input === true ? 'hearing'
        : /^Thinking|^Processing|^Using |checking the request/i.test(live || '') ? 'thinking' : 'listening')
    : notice ? 'error' : ready ? 'idle' : 'unavailable';
  const statusText = loading ? 'Checking connection…' : starting ? 'Connecting…'
    : sleeping ? 'Sleeping' : muted ? 'Muted' +
      (active && props.audioActivity?.output === true ? ' · Hermes is speaking' : '')
    : active ? desktopTalkLiveLabel(live, props.audioActivity)
    : ready && !needsToken ? 'Tap to talk' : 'Not ready';
  const orbLabel = active ? 'Stop talking' : starting ? 'Cancel connection' : 'Start talking';
  const routeLabel = addressed ? addressed.title || 'Untitled task'
    : recipientOperation === 'start_worker' ? 'New worker' : 'This conversation';
  const showRecipients = Boolean(setRecipient && taskContext);
  // Open the typed section on mount when it already holds something to finish.
  const openWithDraft = element => {
    if (!element || element.dataset.htdInit) return;
    element.dataset.htdInit = '1';
    if (typed || attachments.length || selectedRecipient || props.inputError) element.open = true;
  };

  return h('section', { className: 'ht-desktop-view', 'aria-label': 'Talk in this conversation',
    'data-skin': appearance.skin || 'system', 'data-animate': String(appearance.animate === true),
    'data-audio': audioState },
    h('style', null, DESKTOP_TALK_VIEW_CSS),
    h('header', { className: 'htd-head' },
      h('h2', { title: conversation }, conversation),
      h(Badge, { variant: 'outline', className: 'htd-badge htd-provider',
        'data-live': String(Boolean(active)), title: 'Voice provider' },
      desktopTalkSource(status?.source)),
      collapse && iconButton('Collapse', 'minimize', collapse, { className: 'htd-icon-btn' })),

    h('div', { className: 'htd-hero' },
      h('div', { className: 'htd-orb-row' },
        active && setMuted && iconButton(muted ? 'Unmute microphone' : 'Mute microphone', 'mic',
          () => setMuted(!muted), { className: 'htd-side', variant: 'ghost',
            disabled: !active || busy || sleeping, 'aria-pressed': !!muted }),
        h('div', { className: 'htd-orb-wrap', 'data-state': orbState, 'data-live': String(liveAudio) },
          h('span', { className: 'htd-halo', 'aria-hidden': true }),
          h('span', { className: 'htd-ring', 'aria-hidden': true }),
          h('button', { type: 'button', className: 'htd-orb', title: orbLabel,
            disabled: !(active || starting) && Boolean(!ready || loading || switching || needsToken),
            onClick: active || starting ? stopTalk : () => void startTalk() },
          h('span', { className: 'htd-sr' }, orbLabel))),
        active && setSleeping && iconButton(sleeping ? 'Wake' : 'Sleep', sleeping ? 'play' : 'pause',
          () => setSleeping(!sleeping), { className: 'htd-side', variant: 'ghost',
            disabled: !active || busy, 'aria-pressed': !!sleeping })),
      h('p', { className: 'htd-status-line', role: 'status', 'aria-live': 'polite',
        'data-quiet': String(!active && !starting) }, statusText)),

    notice && h('div', { className: 'htd-alert', role: 'alert', 'data-tone': notice.tone || 'danger' },
      h('p', { className: 'htd-alert-title' }, notice.title || notice.text),
      button(notice.retry ? 'Try again' : 'Check connection',
        () => void (notice.retry && ready && !active ? startTalk() : refresh()),
        { variant: 'outline', size: 'sm', disabled: loading || busy }),
      ((notice.title && notice.title !== notice.text) || notice.code) && h('details', null,
        h('summary', null, 'Details'),
        notice.title && notice.title !== notice.text && h('p', { className: 'htd-text' }, notice.text),
        notice.code && h('p', { className: 'htd-text' }, 'Error code: ' + notice.code))),
    !ready && !loading && !notice && h('div', { className: 'htd-alert', 'data-tone': 'info' },
      h('p', { className: 'htd-alert-title' }, "Talk isn't ready on this connection."),
      button('Check connection', () => void refresh(),
        { variant: 'outline', size: 'sm', disabled: busy })),

    captions.length > 0 && h('section', { className: 'htd-section', 'aria-label': 'Live captions' },
      h('h3', { className: 'htd-section-label' }, 'Captions'),
      h('div', { className: 'htd-captions', role: 'log', 'aria-live': 'polite' },
        captions.slice().reverse().map((row, index) => h('div', { className: 'htd-caption',
          key: row.id ?? captions.length - index, 'data-role': row.role === 'user' ? 'user' : 'assistant' },
        h('span', null, row.role === 'user' ? 'You' : 'Hermes'),
        h('p', { className: 'htd-text' }, row.text))))),

    jobs.length > 0 && h('section', { className: 'htd-section', 'aria-label': 'Background work' },
      h('h3', { className: 'htd-section-label',
        title: 'Accepted work keeps running after you stop talking.' }, 'Background work'),
      allJobs.length > jobs.length && h('p', { className: 'htd-muted' },
        'Showing ', String(jobs.length), ' of ', String(allJobs.length), ' jobs.'),
      h('div', { className: 'htd-jobs' }, jobs.map(job => {
        const observations = (taskState.events?.events || []).filter(event =>
          event.run_id === job.run_id && event.action_id === job.action_id);
        const latest = observations.slice().sort((left, right) =>
          right.observed_index - left.observed_index)[0];
        const presentation = job.presentation || latest?.presentation;
        const terminal = ['completed', 'succeeded', 'failed', 'cancelled'].includes(job.status);
        const showResultButton = job.result_available && !props.results?.[job.run_id];
        return h('article', { className: 'htd-card htd-job', key: job.run_id,
          title: 'Job ' + job.run_id + (job.action_id ? ' · action ' + job.action_id : '') },
        h('div', { className: 'htd-card-head' },
          h('p', { className: 'htd-text' }, job.goal || 'Background task'),
          h('span', { className: 'htd-chip', 'data-tone': desktopTalkJobTone(job.status) },
            desktopTalkJobLabel(job.status),
            job.status_source === 'last_observation' && ' · last seen')),
        latest?.label && h('p', { className: 'htd-muted' }, latest.label),
        presentation && h('p', { className: 'htd-muted' }, desktopTalkPresentationLabel(presentation)),
        (!terminal && (steerJob || cancelJob) || (presentation?.replay_eligible && replayResult) ||
          showResultButton) && h('div', { className: 'htd-controls' },
          !terminal && steerJob && button('Steer owned job', () => steerJob(job.run_id),
            { variant: 'ghost', size: 'xs', disabled: !inputOperations.includes('steer') ||
              job.steering?.supported !== true || sending || busy }),
          !terminal && cancelJob && button('Cancel job', () => void cancelJob(job.run_id),
            { variant: 'ghost', size: 'xs', disabled: !inputOperations.includes('cancel') ||
              !!pendingActions['cancel:' + job.run_id]?.pending || busy }),
          presentation?.replay_eligible && replayResult && button('Replay summary',
            () => void replayResult(presentation.event_id), { variant: 'ghost', size: 'xs',
              disabled: props.replaySupported === false || !active || sleeping || busy ||
                !!pendingActions['replay:' + presentation.event_id]?.pending }),
          showResultButton && button('Show result', () => void showResult(job.run_id),
            { variant: 'outline', size: 'xs', disabled: !showResult || switching ||
              props.resultsReadable === false ||
              !!pendingActions['result:' + job.run_id]?.pending })),
        terminal && presentation?.replay_eligible && props.replaySupported === false &&
          h('p', { className: 'htd-muted' }, 'Summary replay is unavailable on this connection.'),
        (job.approval?.approvals || []).slice(0, 4).map(approval =>
          h('section', { className: 'htd-card htd-approval', key: approval.request_id,
            'aria-label': 'Pending approval' },
            h('h3', null, 'Approval required'),
            h('p', { className: 'htd-text' }, approval.description || 'Review this pending operation.'),
            h('p', { className: 'htd-muted' }, 'Request ', approval.request_id),
            h('div', { className: 'htd-controls' },
              (approval.choices || []).filter(choice => ['once', 'session', 'always', 'deny'].includes(choice))
                .map(choice => button(({ once: 'Allow once', session: 'Allow for session',
                  always: 'Always allow', deny: 'Deny' })[choice], () => void answerApproval({
                    run_id: job.run_id, action_id: job.action_id, request_id: approval.request_id, choice
                  }), { key: choice, variant: choice === 'deny' ? 'ghost' : 'outline', size: 'sm',
                    disabled: !answerApproval || !inputOperations.includes('approval') ||
                      job.approval.actionable !== true || busy ||
                      !!pendingActions['approval:' + approval.request_id]?.pending }))),
            pendingActions['approval:' + approval.request_id]?.pending &&
              h('p', { role: 'status', className: 'htd-muted' }, 'Submitting approval…'),
            (job.approval.actionable !== true || !inputOperations.includes('approval')) && h('p', { className: 'htd-muted' },
              'Approval controls are unavailable for this request.'),
            actionError('approval:' + approval.request_id, 'The approval could not be submitted. Try again.'))),
        showResultButton && props.resultsReadable === false &&
          h('p', { className: 'htd-muted' }, 'Start talking to open the stored result.'),
        actionError('cancel:' + job.run_id, 'The job could not be cancelled. Try again.'),
        actionError('result:' + job.run_id, 'The result could not be loaded. Try again.'),
        presentation && actionError('replay:' + presentation.event_id, 'The summary could not be replayed. Try again.'));
      }))),

    results.length > 0 && h('section', { className: 'htd-section', 'aria-label': 'Task results' },
      h('h3', { className: 'htd-section-label' }, 'Results'),
      results.map(([runId, result]) => h('article', { className: 'htd-card', key: runId },
        h('p', { className: 'htd-muted' },
          'Job ', runId, ' · ', allJobs.find(job => String(job.run_id) === runId)?.goal || 'Task result'),
        h('p', { className: 'htd-text htd-result' }, typeof result?.output === 'string' && result.output
          ? result.output : result?.error ? 'This task could not return a result.' : 'No result text was supplied.'),
        (Array.isArray(result?.artifacts) ? result.artifacts : []).map((artifact, index) =>
          h('pre', { className: 'htd-text htd-result htd-muted', key: index }, JSON.stringify(artifact, null, 2))),
        result?.truncated && h('p', { className: 'htd-muted' },
          'Only part of this result is available here.')))),

    h('details', { className: 'htd-more', ref: openWithDraft },
      h('summary', null, 'Type a message',
        showRecipients && h('span', { className: 'htd-summary-hint' }, 'To: ' + routeLabel)),
      h('div', { className: 'htd-body' },
        showRecipients && h('section', { className: 'htd-section', 'aria-label': 'Addressed recipient' },
          recipients.length > 0 || selectedRecipient
            ? h(React.Fragment, null,
              recipients.length > 0 && h(HermesSDK.Input, { type: 'search', value: recipientQuery,
                placeholder: 'Find an app or task', 'aria-label': 'Find an app or task',
                disabled: !setRecipientQuery, onChange: event => setRecipientQuery?.(event.target.value) }),
              h('div', { className: 'htd-route' },
                h('span', { className: 'htd-route-label' }, 'To'),
                h('select', { value: selectedRecipient, disabled: sending || busy,
                  'aria-label': 'Addressed recipient',
                  onChange: event => setRecipient(event.target.value) },
                h('option', { value: '' }, 'This conversation'),
                selectedRecipient && !recipient && h('option', { value: selectedRecipient, disabled: true },
                  'Recipient unavailable · ' + selectedRecipient),
                recipientChoices.map(row => h('option', { value: row.recipient_id, key: row.recipient_id,
                  disabled: row.available === false }, desktopTalkRecipientLabel(row)))),
                props.refreshRecipients && iconButton(
                  recipientLoading ? 'Refreshing recipients…' : 'Refresh recipients', 'refresh',
                  () => void props.refreshRecipients(), { className: 'htd-icon-btn',
                    disabled: recipientLoading || sending || busy })))
            : h('div', { className: 'htd-route' },
              h('span', { className: 'htd-route-label' }, 'To'),
              h('span', { className: 'htd-route-value' }, 'This conversation'),
              props.refreshRecipients && button(recipientLoading ? 'Searching…' : 'Find other apps',
                () => void props.refreshRecipients(), { variant: 'ghost', size: 'xs',
                  disabled: recipientLoading || sending || busy })),
          addressed && h('p', { className: 'htd-muted' }, desktopTalkRecipientLabel(addressed)),
          addressed && h('div', { className: 'htd-badges' },
            h('span', { className: 'htd-chip' }, recipient.read_only ? 'Read-only history'
              : messageable ? 'Can message' : 'Message unavailable'),
            readable && h('span', { className: 'htd-chip' }, 'History'),
            steerable && h('span', { className: 'htd-chip' }, 'Can steer')),
          !matchingRecipients.length && recipientQuery && h('p', { className: 'htd-muted' },
            'No recipients match this search.'),
          (props.recipientSources || []).filter(source => source.available === false).map(source =>
            h('p', { className: 'htd-muted', key: source.app }, source.app + ': history unavailable')),
          props.recipientError && h('p', { role: 'alert', className: 'htd-muted' },
            'The recipient could not be loaded. Refresh the list and try again.' +
            (desktopTalkErrorCode(props.recipientError) &&
              ' Error code: ' + desktopTalkErrorCode(props.recipientError)))),

        showOperations && h('label', null, 'Action',
          h('select', { value: recipientOperation, disabled: sending || busy,
            'aria-label': 'Recipient action', onChange: event => setRecipientOperation(event.target.value) },
          h('option', { value: 'read', disabled: !readable }, 'Read conversation'),
          h('option', { value: 'message', disabled: !messageable },
            selectedRecipient ? 'Message existing task' : 'Message this conversation'),
          h('option', { value: 'start_worker', disabled: !inputOperations.includes('start_worker') },
            'Start a new worker'),
          h('option', { value: 'steer', disabled: !steerable }, 'Steer owned job'))),
        recipientOperation === 'steer' && selectedJob && h('p', { className: 'htd-muted' },
          'Steering job ', String(selectedJob.run_id), ' · ', selectedJob.goal),
        recipientOperation === 'start_worker' && h('p', { className: 'htd-muted' },
          'Starts a new worker on this task.'),
        recipientOperation === 'read' && h('div', null,
          button(recipientLoading ? 'Reading…' : 'Read conversation', () => void readRecipient(),
            { variant: 'outline', size: 'sm',
              disabled: !readable || !readRecipient || recipientLoading || sending || busy })),
        recipientHistory && recipientHistory.recipient_id === selectedRecipient &&
          recipientHistory.app === recipient?.app && recipientHistory.task_id === recipient?.task_id &&
          recipientHistory.host_id === recipient?.host_id &&
          h('section', { className: 'htd-section', 'aria-label': 'Recipient history' },
            h('h3', { className: 'htd-section-label' }, 'Conversation history · read-only'),
            h('p', { className: 'htd-muted' }, 'Observed ', recipientHistory.observed_at || 'unknown',
              ' · source updated ', recipientHistory.source?.modified_at || 'unknown'),
            h('div', { className: 'htd-captions htd-history' },
              (recipientHistory.messages || []).slice(-50).reverse().map((row, index) =>
                h('article', { className: 'htd-caption', key: row.id ?? index,
                  'data-role': row.role === 'user' ? 'user' : 'assistant' },
                  h('span', null, row.role === 'user' ? 'User' : 'Assistant'),
                  h('p', { className: 'htd-text' }, row.text),
                  row.truncated && h('p', { className: 'htd-muted' }, 'Message shortened by source.')))),
            recipientHistory.truncated && h('p', { className: 'htd-muted' },
              'This bounded snapshot omits some conversation history.')),

        h('form', { className: 'htd-section', 'aria-label': 'Typed message',
          onDragOver: event => event.preventDefault(), onDrop: event => {
            event.preventDefault(); event.stopPropagation(); receiveFiles(event.dataTransfer?.files);
          }, onSubmit: event => {
            event.preventDefault();
            event.stopPropagation();
            if (!sendDisabled) void sendTyped();
          } },
        h(HermesSDK.Textarea || 'textarea', { value: typed, disabled: sending || switching, rows: 3,
          placeholder: 'Type a message…', 'aria-label': 'Message Hermes',
          onChange: event => setTyped(event.target.value), onPaste: event => {
            if (receiveFiles(event.clipboardData?.files)) event.preventDefault();
          } }),
        attachments.length > 0 && h('ul', { className: 'htd-attachments', 'aria-label': 'Local attachments' },
          attachments.map(file => h('li', { className: 'htd-attachment', key: file.id },
            typeof file.previewUrl === 'string' && file.previewUrl.startsWith('blob:') &&
              file.type?.startsWith('image/') && h('img', { src: file.previewUrl, alt: file.name }),
            h('span', { className: 'htd-text' }, file.name),
            Number.isFinite(file.size) && h('span', { className: 'htd-muted' }, desktopTalkBytes(file.size)),
            removeAttachment && iconButton('Remove ' + file.name, 'close', () => removeAttachment(file.id),
              { disabled: sending, size: 'icon-xs' })))),
        h('div', { className: 'htd-composer-row' },
          addAttachments && h('label', { className: 'htd-file htd-i htd-i-attach', title: 'Attach files' },
            h('input', { type: 'file', multiple: true, disabled: sending,
              'aria-label': 'Attach files', onChange: event => {
                receiveFiles(event.target.files); event.target.value = '';
              } })),
          h('p', { className: 'htd-muted' }, active ? '' : 'Microphone off · sends as text'),
          h(HermesSDK.Button, { type: 'submit', size: 'sm', disabled: sendDisabled }, sending ? 'Sending…' : 'Send')),
        attachments.length > 0 && props.attachmentsSupported !== true &&
          h('p', { className: 'htd-muted' }, 'This recipient cannot receive attachments. Files remain local.'),
        attachments.length > 0 && props.attachmentsSupported === true &&
          recipientOperation !== 'start_worker' &&
          h('p', { className: 'htd-muted' },
            'Only a worker start can carry attachments. Files remain local.'),
        props.inputError && h('p', { className: 'htd-muted', role: 'alert' },
          'Not sent. Your draft and attachments are kept.'),
        !operationAvailable && h('p', { className: 'htd-muted' }, 'This action is unavailable for the selected recipient.')),

        props.actionReceipt && h('p', { className: 'htd-muted', role: 'status' },
          ({ read: 'Read conversation', message: 'Message existing task', start_worker: 'Start worker',
            steer: 'Steer owned job' })[props.actionReceipt.operation] || 'Request',
          ' · ', props.actionReceipt.state || 'Unknown',
          props.actionReceipt.recipient_id && ' · recipient ' + props.actionReceipt.recipient_id,
          props.actionReceipt.run_id != null && ' · job ' + props.actionReceipt.run_id))),

    h('details', { className: 'htd-more' }, h('summary', null, 'Settings'),
      h('div', { className: 'htd-body' },
        voices.length > 0 && h('label', null, 'Voice',
          h('select', { value: voice, disabled: !ready || active || busy,
            onChange: event => setVoice(event.target.value) },
          h('option', { value: '' }, 'Use configured voice'),
          voices.map(name => h('option', { value: name, key: name }, name)))),
        taskState && h('label', null, 'Spoken updates',
          h('select', { value: taskState.preferences?.update_mode || 'important',
            disabled: !active || busy,
            onChange: event => void saveUpdatePreference(event.target.value) },
          h('option', { value: 'important' }, 'Completion and important updates'),
          h('option', { value: 'completion' }, 'Completion only'),
          h('option', { value: 'frequent' }, 'Include meaningful milestones'))),
        tasks.length > 0 && h('label', null, 'Switch conversation',
          h('select', { value: selectedTask || '', disabled: !!voiceOwner || !active || busy,
            onChange: event => {
              if (event.target.value && event.target.value !== selectedTask)
                void switchTarget({ target_id: event.target.value });
            } },
          h('option', { value: '', disabled: true }, 'Choose a conversation'),
          tasks.map((task, index) => h('option', { value: task.target_id, key: task.target_id },
            (task.label || 'Conversation ' + (index + 1)) +
            (tasks.filter(other => other.label === task.label).length > 1
              ? ' · ' + String(task.session_id || task.target_id).slice(-6) : ''))))),
        catalogNotice && h('p', { className: 'htd-muted', role: 'status' },
          'The conversation list is unavailable. ' + catalogNotice.text +
          (catalogNotice.code ? ' Error code: ' + catalogNotice.code : '')),
        h('div', { className: 'htd-controls' },
          returnDepth > 0 && button('Return to previous conversation', () => void switchTarget({ back: true }),
            { variant: 'outline', size: 'xs', disabled: !!voiceOwner || !active || busy }),
          button('Refresh conversations', () => void refreshCatalog(),
            { variant: 'outline', size: 'xs', disabled: loading || busy })),
        setAppearance && h('label', null, 'Appearance',
          h('select', { value: appearance.skin || 'system', 'aria-label': 'Appearance',
            onChange: event => setAppearance({ ...appearance, skin: event.target.value }) },
          h('option', { value: 'system' }, 'Follow Hermes'),
          h('option', { value: 'quiet' }, 'Quiet'),
          h('option', { value: 'contrast' }, 'High contrast'))),
        setAppearance && h('label', { className: 'htd-toggle' }, 'Animate the voice button',
          h('input', { type: 'checkbox', role: 'switch', checked: appearance.animate === true,
            onChange: event => setAppearance({ ...appearance, animate: event.target.checked }) })),
        setAppearance && collapse && h('label', { className: 'htd-toggle' },
          'Shrink to the Talk button after connecting',
          h('input', { type: 'checkbox', role: 'switch', checked: appearance.collapseOnConnect !== false,
            onChange: event => setAppearance({ ...appearance, collapseOnConnect: event.target.checked }) })),
        setAppearance && collapse && h('label', { className: 'htd-toggle' },
          'Expand while the pointer is over the Talk button',
          h('input', { type: 'checkbox', role: 'switch', checked: appearance.hoverExpand !== false,
            onChange: event => setAppearance({ ...appearance, hoverExpand: event.target.checked }) })),
        h('p', { className: 'htd-owner' }, 'Voice owner: ', conversation,
          voiceOwner && ' · ' + [voiceOwner.connectionId, voiceOwner.profile,
            voiceOwner.sessionId, voiceOwner.storedSessionId].filter(Boolean).join(' · ')),
        hostNote && h('p', { className: 'htd-muted' }, hostNote))));
}
