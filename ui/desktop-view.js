const DESKTOP_TALK_VIEW_CSS = `
.ht-desktop-view { display:grid; gap:1rem; min-width:0; color:inherit; font:inherit; }
.ht-desktop-view p, .ht-desktop-view h2, .ht-desktop-view h3 { margin:0; }
.ht-desktop-view h2 { font-size:1rem; font-weight:600; overflow-wrap:anywhere; }
.ht-desktop-view h3 { font-size:.875rem; font-weight:600; }
.ht-desktop-view .htd-row { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; }
.ht-desktop-view .htd-header { justify-content:space-between; }
.ht-desktop-view .htd-stack { display:grid; gap:.5rem; min-width:0; }
.ht-desktop-view .htd-muted { opacity:.7; font-size:.8125rem; }
.ht-desktop-view .htd-text { white-space:pre-wrap; overflow-wrap:anywhere; font-size:.875rem; }
.ht-desktop-view .htd-notice, .ht-desktop-view .htd-job { padding:.75rem; border:1px solid color-mix(in srgb,currentColor 18%,transparent); border-radius:.5rem; }
.ht-desktop-view .htd-captions { display:grid; gap:.75rem; max-height:15rem; overflow-y:auto; }
.ht-desktop-view details { border-top:1px solid color-mix(in srgb,currentColor 18%,transparent); padding-top:.75rem; }
.ht-desktop-view summary { cursor:pointer; font-size:.8125rem; }
.ht-desktop-view details > .htd-stack { margin-top:.75rem; }
.ht-desktop-view label { display:grid; gap:.375rem; font-size:.8125rem; }
.ht-desktop-view select { width:100%; min-width:0; padding:.5rem; color:inherit; background:inherit; border:1px solid color-mix(in srgb,currentColor 25%,transparent); border-radius:.375rem; font:inherit; }
.ht-desktop-view select:disabled { opacity:.5; }
.ht-desktop-view[data-skin="quiet"] .htd-job { border-color:transparent; background:color-mix(in srgb,currentColor 4%,transparent); }
.ht-desktop-view[data-skin="contrast"] { color:CanvasText; background:Canvas; }
.ht-desktop-view .htd-owner { font-size:.75rem; overflow-wrap:anywhere; }
.ht-desktop-view .htd-status { display:inline-block; width:.5rem; height:.5rem; border-radius:50%; background:currentColor; margin-right:.375rem; }
.ht-desktop-view[data-animate="true"][data-audio="active"] .htd-status { animation:htd-pulse 2s ease-in-out infinite; }
.ht-desktop-view textarea { width:100%; min-height:4.5rem; resize:vertical; color:inherit; background:inherit; border:1px solid color-mix(in srgb,currentColor 25%,transparent); border-radius:.375rem; padding:.5rem; font:inherit; }
.ht-desktop-view .htd-controls { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
.ht-desktop-view .htd-attachments { display:flex; gap:.5rem; flex-wrap:wrap; list-style:none; padding:0; margin:0; }
.ht-desktop-view .htd-attachment { border:1px solid color-mix(in srgb,currentColor 18%,transparent); border-radius:.375rem; padding:.5rem; max-width:100%; }
.ht-desktop-view .htd-attachment img { max-width:6rem; max-height:5rem; object-fit:contain; }
.ht-desktop-view .htd-file input { width:100%; font:inherit; }
.ht-desktop-view .htd-history, .ht-desktop-view .htd-result { max-height:20rem; overflow:auto; }
.ht-desktop-view .htd-jobs { max-height:28rem; overflow:auto; }
.ht-desktop-view :focus-visible { outline:2px solid currentColor; outline-offset:3px; }
@keyframes htd-pulse { 50% { opacity:.4; } }
@media (prefers-reduced-motion:reduce) { .ht-desktop-view .htd-status { animation:none !important; } }
`;

function desktopTalkNotice(value, needsToken, lane) {
  if (!value && !needsToken) return null;
  const message = String(value?.message || value || '');
  if (/^404\b/.test(message) && /target_missing/.test(message)) {
    return { text: 'Send one message in this conversation first, then Connect. ' +
      'Hermes Desktop saves a conversation on its first message.', retry: true };
  }
  if (lane === 'stock' && (needsToken || /^(?:401|403)\b/.test(message))) {
    return { text: 'This Hermes Desktop cannot send TALK_DASHBOARD_TOKEN. ' +
      'Unset it for local Desktop use, or use the dashboard Talk tab.', retry: false };
  }
  if (needsToken || /^(?:401|403)\b/.test(message)) {
    return { text: 'Reconnect to this Hermes connection and try again.' };
  }
  if (/NotAllowedError|PermissionDenied|permission denied|microphone.*(?:denied|blocked)|(?:denied|blocked).*microphone/i.test(message)) {
    return { text: 'Allow microphone access for Hermes in your system settings, then try again.', retry: true };
  }
  if (/NotFoundError|DevicesNotFound|no microphone|microphone.*not found/i.test(message)) {
    return { text: 'Connect a microphone, then try again.', retry: true };
  }
  if (/NotReadableError|TrackStartError|microphone.*(?:in use|busy)|other voice session/i.test(message)) {
    return { text: 'Stop the other voice session using your microphone, then try again.', retry: true };
  }
  if (/\b503\b|temporarily unavailable|service_unavailable/i.test(message)) {
    return { text: 'Talk is temporarily unavailable. Try again.', retry: true };
  }
  return { text: 'Talk could not complete this request. Try again.', retry: true };
}

function desktopTalkSource(source) {
  if (['codex-oauth', 'subscription'].includes(source)) return 'ChatGPT subscription';
  if (['configured', 'env', 'api'].includes(source)) return 'OpenAI API';
  return 'Hermes voice';
}

function desktopTalkLiveLabel(live, audioActivity) {
  if (audioActivity?.output === true) return 'Audio playing';
  if (audioActivity?.input === true) return 'Microphone audio detected';
  if (/^Thinking|^Processing/i.test(live)) return 'Thinking…';
  if (/^Using /i.test(live)) return 'Hermes is working on your request.';
  if (/checking the request/i.test(live)) return 'Hermes is checking your request.';
  if (/returned a task decision/i.test(live)) return 'Hermes returned an update.';
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
    cancelJob, steerJob, answerApproval, replayResult, pendingActions = {}, lane } = props;
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
  const catalogNotice = desktopTalkNotice(catalogError, false, lane);
  const button = (label, onClick, options = {}) => h(HermesSDK.Button,
    { ...options, type: 'button', onClick }, label);
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
  const operationAvailable = ({ read: readable, message: messageable,
    start_worker: inputOperations.includes('start_worker'), steer: steerable
  })[recipientOperation] === true;
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

  return h('section', { className: 'ht-desktop-view', 'aria-label': 'Talk in this conversation',
    'data-skin': appearance.skin || 'system', 'data-animate': String(appearance.animate === true),
    'data-audio': audioState },
    h('style', null, DESKTOP_TALK_VIEW_CSS),
    h('div', { className: 'htd-row htd-header' },
      h('div', { className: 'htd-stack' },
        h('h2', null, conversation),
        h('p', { className: 'htd-owner' }, 'Voice owner: ', conversation,
          voiceOwner && ' · ' + [voiceOwner.connectionId, voiceOwner.profile,
            voiceOwner.sessionId, voiceOwner.storedSessionId].filter(Boolean).join(' · ')),
        h('p', { className: 'htd-muted', role: 'status' },
          h('span', { className: 'htd-status', 'aria-hidden': true }),
          loading ? 'Checking connection…' : starting ? 'Connecting…'
            : sleeping ? 'Sleeping · microphone off' : muted ? 'Microphone muted' +
              (active && props.audioActivity?.output === true ? ' · Audio playing' : '')
            : active ? desktopTalkSource(status?.source) + ' · ' + desktopTalkLiveLabel(live, props.audioActivity)
            : ready ? desktopTalkSource(status?.source) : 'Talk is not ready on this connection.')),
      h('div', { className: 'htd-controls' }, active || starting
        ? button(starting ? 'Cancel connection' : 'Stop talking', stopTalk)
        : button('Connect', () => void startTalk(),
          { disabled: !ready || loading || switching || needsToken }),
      collapse && button('Collapse', collapse, { variant: 'outline', size: 'sm' }))),

    (setMuted || setSleeping) && h('div', { className: 'htd-controls', 'aria-label': 'Audio controls' },
      setMuted && button(muted ? 'Unmute microphone' : 'Mute microphone', () => setMuted(!muted),
        { variant: 'outline', size: 'sm', disabled: !active || busy || sleeping,
          'aria-pressed': !!muted }),
      setSleeping && button(sleeping ? 'Wake' : 'Sleep', () => setSleeping(!sleeping),
        { variant: 'outline', size: 'sm', disabled: !active || busy, 'aria-pressed': !!sleeping })),

    !active && !starting && ready && !notice && h('p', { className: 'htd-muted' },
      'Talk to Hermes in this conversation. You can interrupt at any time.'),
    notice && h('div', { className: 'htd-stack htd-notice', role: 'alert' },
      h('p', { className: 'htd-text' }, notice.text),
      h('div', null, button(notice.retry ? 'Try again' : 'Check connection',
        () => void (notice.retry && ready && !active ? startTalk() : refresh()),
        { variant: 'outline', size: 'sm', disabled: loading || busy }))),
    !ready && !loading && !notice && h('div', { className: 'htd-stack' },
      h('p', { className: 'htd-muted' }, 'Reconnect to this Hermes connection and try again.'),
      h('div', null, button('Check connection', () => void refresh(),
        { variant: 'outline', size: 'sm', disabled: busy }))),

    captions.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Live captions' },
      h('h3', null, 'Live captions'),
      h('div', { className: 'htd-captions', role: 'log', 'aria-live': 'polite' },
        captions.map((row, index) => h('div', { className: 'htd-stack', key: row.id ?? index },
          h('span', { className: 'htd-muted' }, row.role === 'user' ? 'You' : 'Hermes'),
          h('p', { className: 'htd-text' }, row.text))))),

    setRecipient && h('section', { className: 'htd-stack', 'aria-label': 'Addressed recipient' },
      h('h3', null, 'Addressed recipient'),
      h('label', null, 'Find an app or task',
        h(HermesSDK.Input, { type: 'search', value: recipientQuery,
          'aria-label': 'Find an app or task', disabled: !setRecipientQuery,
          onChange: event => setRecipientQuery?.(event.target.value) })),
      h('label', null, 'Recipient',
        h('select', { value: selectedRecipient, disabled: sending || busy,
          'aria-label': 'Addressed recipient',
          onChange: event => setRecipient(event.target.value) },
        h('option', { value: '' }, 'Hermes · voice owner'),
        selectedRecipient && !recipient && h('option', { value: selectedRecipient, disabled: true },
          'Recipient unavailable · ' + selectedRecipient),
        recipientChoices.map(row => h('option', { value: row.recipient_id, key: row.recipient_id,
          disabled: row.available === false }, desktopTalkRecipientLabel(row))))),
      addressed && h('p', { className: 'htd-text' }, desktopTalkRecipientLabel(addressed)),
      addressed && h('p', { className: 'htd-muted' },
        recipient.read_only ? 'Read-only history' : messageable ? 'Existing-app message available' : 'Message unavailable',
        ' · Read history: ', readable ? 'available' : 'unavailable',
        ' · Owned-job steering: ', steerable ? 'available' : 'unavailable'),
      !matchingRecipients.length && recipientQuery && h('p', { className: 'htd-muted' },
        'No recipients match this search.'),
      (props.recipientSources || []).filter(source => source.available === false).map(source =>
        h('p', { className: 'htd-muted', key: source.app }, source.app + ': history unavailable')),
      props.refreshRecipients && h('div', null, button(recipientLoading ? 'Refreshing recipients…' : 'Refresh recipients',
        () => void props.refreshRecipients(), { variant: 'outline', size: 'sm',
          disabled: recipientLoading || sending || busy })),
      props.recipientError && h('p', { role: 'alert', className: 'htd-muted' },
        'The recipient could not be loaded. Refresh the list and try again.')),

    setRecipientOperation && h('label', null, 'Action',
      h('select', { value: recipientOperation, disabled: sending || busy,
        'aria-label': 'Recipient action', onChange: event => setRecipientOperation(event.target.value) },
      h('option', { value: 'read', disabled: !readable }, 'Read conversation'),
      h('option', { value: 'message', disabled: !messageable }, 'Message existing task'),
      h('option', { value: 'start_worker', disabled: !inputOperations.includes('start_worker') },
        'Start a new worker'),
      h('option', { value: 'steer', disabled: !steerable }, 'Steer owned job'))),
    recipientOperation === 'steer' && selectedJob && h('p', { className: 'htd-text' },
      'Steering job ', String(selectedJob.run_id), ' · ', selectedJob.goal),
    recipientOperation === 'start_worker' && h('p', { className: 'htd-text' },
      'Starts a new worker on this task. This send is not addressed to a recipient.'),
    recipientOperation === 'read' && h('div', null,
      button(recipientLoading ? 'Reading…' : 'Read conversation', () => void readRecipient(),
        { disabled: !readable || !readRecipient || recipientLoading || sending || busy })),
    recipientHistory && recipientHistory.recipient_id === selectedRecipient &&
      recipientHistory.app === recipient?.app && recipientHistory.task_id === recipient?.task_id &&
      recipientHistory.host_id === recipient?.host_id &&
      h('section', { className: 'htd-stack', 'aria-label': 'Recipient history' },
        h('h3', null, 'Read-only conversation history'),
        h('p', { className: 'htd-muted' }, 'Observed: ', recipientHistory.observed_at || 'unknown',
          ' · Source updated: ', recipientHistory.source?.modified_at || 'unknown'),
        h('div', { className: 'htd-stack htd-history' },
          (recipientHistory.messages || []).slice(-50).map((row, index) =>
            h('article', { className: 'htd-stack', key: row.id ?? index },
              h('p', { className: 'htd-muted' }, row.role === 'user' ? 'User' : 'Assistant'),
              h('p', { className: 'htd-text' }, row.text),
              row.truncated && h('p', { className: 'htd-muted' }, 'Message shortened by source.')))),
        recipientHistory.truncated && h('p', { className: 'htd-muted' },
          'This bounded snapshot omits some conversation history.')),

    h('form', { className: 'htd-stack', 'aria-label': 'Typed message',
      onDragOver: event => event.preventDefault(), onDrop: event => {
        event.preventDefault(); event.stopPropagation(); receiveFiles(event.dataTransfer?.files);
      }, onSubmit: event => {
      event.preventDefault();
      event.stopPropagation();
      if (!sendDisabled) void sendTyped();
    } },
    !active && h('p', { className: 'htd-muted' }, 'Microphone off · type and send without connecting audio.'),
    h('textarea', { value: typed, disabled: sending || switching,
      placeholder: 'Type a message…', 'aria-label': 'Message Hermes',
      onChange: event => setTyped(event.target.value), onPaste: event => {
        if (receiveFiles(event.clipboardData?.files)) event.preventDefault();
      } }),
    attachments.length > 0 && h('ul', { className: 'htd-attachments', 'aria-label': 'Local attachments' },
      attachments.map(file => h('li', { className: 'htd-stack htd-attachment', key: file.id },
        typeof file.previewUrl === 'string' && file.previewUrl.startsWith('blob:') &&
          file.type?.startsWith('image/') && h('img', { src: file.previewUrl, alt: file.name }),
        h('span', { className: 'htd-text' }, file.name),
        Number.isFinite(file.size) && h('span', { className: 'htd-muted' }, file.size + ' bytes · local'),
        removeAttachment && button('Remove ' + file.name, () => removeAttachment(file.id),
          { disabled: sending, variant: 'outline', size: 'sm' })))),
    h('div', { className: 'htd-controls' },
      addAttachments && h('label', { className: 'htd-file' }, 'Attach files',
        h('input', { type: 'file', multiple: true, disabled: sending,
          'aria-label': 'Attach files', onChange: event => {
            receiveFiles(event.target.files); event.target.value = '';
          } })),
      h(HermesSDK.Button, { type: 'submit', disabled: sendDisabled }, sending ? 'Sending…' : 'Send')),
    attachments.length > 0 && props.attachmentsSupported !== true &&
      h('p', { className: 'htd-muted' }, 'This recipient cannot receive attachments. Files remain local.'),
    attachments.length > 0 && props.attachmentsSupported === true &&
      recipientOperation !== 'start_worker' &&
      h('p', { className: 'htd-muted' },
        'Only a worker start can carry attachments. Files remain local.'),
    props.inputError && h('p', { className: 'htd-muted', role: 'alert' },
      'This input could not be sent. Your draft and attachments are retained.'),
    !operationAvailable && h('p', { className: 'htd-muted' }, 'This action is unavailable for the selected recipient.')),

    props.actionReceipt && h('p', { className: 'htd-text', role: 'status' },
      ({ read: 'Read conversation', message: 'Message existing task', start_worker: 'Start worker',
        steer: 'Steer owned job' })[props.actionReceipt.operation] || 'Request',
      ' · ', props.actionReceipt.state || 'Unknown',
      props.actionReceipt.recipient_id && ' · recipient ' + props.actionReceipt.recipient_id,
      props.actionReceipt.run_id != null && ' · job ' + props.actionReceipt.run_id),

    jobs.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Background work' },
      h('h3', null, 'Background work'),
      h('p', { className: 'htd-muted' }, 'Accepted work keeps running after you stop talking.'),
      allJobs.length > jobs.length && h('p', { className: 'htd-muted' },
        'Showing ', String(jobs.length), ' of ', String(allJobs.length), ' jobs.'),
      h('div', { className: 'htd-stack htd-jobs' }, jobs.map(job => {
        const observations = (taskState.events?.events || []).filter(event =>
          event.run_id === job.run_id && event.action_id === job.action_id);
        const latest = observations.slice().sort((left, right) =>
          right.observed_index - left.observed_index)[0];
        const presentation = job.presentation || latest?.presentation;
        const terminal = ['completed', 'succeeded', 'failed', 'cancelled'].includes(job.status);
        return h('article', { className: 'htd-stack htd-job', key: job.run_id },
        h('p', { className: 'htd-text' }, job.goal || 'Background task'),
        h('p', { className: 'htd-muted' }, desktopTalkJobLabel(job.status),
          ' · Job ', String(job.run_id), ' · Action ', job.action_id || 'unknown',
          job.status_source === 'last_observation' && ' · last observed status'),
        latest?.label && h('p', { className: 'htd-text' }, latest.label),
        presentation && h('p', { className: 'htd-muted' }, desktopTalkPresentationLabel(presentation)),
        h('div', { className: 'htd-controls' },
          !terminal && steerJob && button('Steer owned job', () => steerJob(job.run_id),
            { variant: 'outline', size: 'sm', disabled: !inputOperations.includes('steer') ||
              job.steering?.supported !== true || sending || busy }),
          !terminal && cancelJob && button('Cancel job', () => void cancelJob(job.run_id),
            { variant: 'outline', size: 'sm', disabled: !inputOperations.includes('cancel') ||
              !!pendingActions['cancel:' + job.run_id]?.pending || busy }),
          presentation?.replay_eligible && replayResult && button('Replay summary',
            () => void replayResult(presentation.event_id), { variant: 'outline', size: 'sm',
              disabled: props.replaySupported === false || !active || sleeping || busy ||
                !!pendingActions['replay:' + presentation.event_id]?.pending })),
        terminal && presentation?.replay_eligible && props.replaySupported === false &&
          h('p', { className: 'htd-muted' }, 'Summary replay is unavailable on this connection.'),
        (job.approval?.approvals || []).slice(0, 4).map(approval =>
          h('section', { className: 'htd-stack htd-notice', key: approval.request_id,
            'aria-label': 'Pending approval' },
            h('h3', null, 'Approval required'),
            h('p', { className: 'htd-text' }, approval.description || 'Review this pending operation.'),
            h('p', { className: 'htd-muted' }, 'Request ', approval.request_id),
            h('div', { className: 'htd-controls' },
              (approval.choices || []).filter(choice => ['once', 'session', 'always', 'deny'].includes(choice))
                .map(choice => button(({ once: 'Allow once', session: 'Allow for session',
                  always: 'Always allow', deny: 'Deny' })[choice], () => void answerApproval({
                    run_id: job.run_id, action_id: job.action_id, request_id: approval.request_id, choice
                  }), { key: choice, variant: 'outline', size: 'sm',
                    disabled: !answerApproval || !inputOperations.includes('approval') ||
                      job.approval.actionable !== true || busy ||
                      !!pendingActions['approval:' + approval.request_id]?.pending }))),
            pendingActions['approval:' + approval.request_id]?.pending &&
              h('p', { role: 'status', className: 'htd-muted' }, 'Submitting approval…'),
            (job.approval.actionable !== true || !inputOperations.includes('approval')) && h('p', { className: 'htd-muted' },
              'Approval controls are unavailable for this request.'),
            actionError('approval:' + approval.request_id, 'The approval could not be submitted. Try again.'))),
        job.result_available && !props.results?.[job.run_id] && h('div', null,
          button('Show result', () => void showResult(job.run_id),
            { variant: 'outline', size: 'sm', disabled: !showResult || switching ||
              props.resultsReadable === false ||
              !!pendingActions['result:' + job.run_id]?.pending })),
        job.result_available && !props.results?.[job.run_id] && props.resultsReadable === false &&
          h('p', { className: 'htd-muted' },
            'Connect this conversation to open the stored result.'),
        actionError('cancel:' + job.run_id, 'The job could not be cancelled. Try again.'),
        actionError('result:' + job.run_id, 'The result could not be loaded. Try again.'),
        presentation && actionError('replay:' + presentation.event_id, 'The summary could not be replayed. Try again.'));
      }))),

    results.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Task results' },
      h('h3', null, 'Results'),
      results.map(([runId, result]) => h('article', { className: 'htd-stack htd-job', key: runId },
        h('p', { className: 'htd-muted' },
          'Job ', runId, ' · ', allJobs.find(job => String(job.run_id) === runId)?.goal || 'Task result'),
        h('p', { className: 'htd-text htd-result' }, typeof result?.output === 'string' && result.output
          ? result.output : result?.error ? 'This task could not return a result.' : 'No result text was supplied.'),
        (Array.isArray(result?.artifacts) ? result.artifacts : []).map((artifact, index) =>
          h('pre', { className: 'htd-text htd-result', key: index }, JSON.stringify(artifact, null, 2))),
        result?.truncated && h('p', { className: 'htd-muted' },
          'Only part of this result is available here.')))),

    h('details', null, h('summary', null, 'Advanced'),
      h('div', { className: 'htd-stack' },
        setAppearance && h('label', null, 'Appearance',
          h('select', { value: appearance.skin || 'system', 'aria-label': 'Appearance',
            onChange: event => setAppearance({ ...appearance, skin: event.target.value }) },
          h('option', { value: 'system' }, 'Follow Hermes'),
          h('option', { value: 'quiet' }, 'Quiet'),
          h('option', { value: 'contrast' }, 'High contrast'))),
        setAppearance && h('label', { className: 'htd-row' },
          h('input', { type: 'checkbox', checked: appearance.animate === true,
            onChange: event => setAppearance({ ...appearance, animate: event.target.checked }) }),
          'Animate active audio state'),
        setAppearance && collapse && h('label', { className: 'htd-row' },
          h('input', { type: 'checkbox', checked: appearance.collapseOnConnect !== false,
            onChange: event => setAppearance({ ...appearance, collapseOnConnect: event.target.checked }) }),
          'Shrink to the Talk button after connecting'),
        setAppearance && collapse && h('label', { className: 'htd-row' },
          h('input', { type: 'checkbox', checked: appearance.hoverExpand !== false,
            onChange: event => setAppearance({ ...appearance, hoverExpand: event.target.checked }) }),
          'Expand while the pointer is over the Talk button'),
        voices.length > 0 && h('label', null, 'Voice',
          h('select', { value: voice, disabled: !ready || active || busy,
            onChange: event => setVoice(event.target.value) },
          h('option', { value: '' }, 'Use configured voice'),
          voices.map(name => h('option', { value: name, key: name }, name)))),
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
        returnDepth > 0 && h('div', null,
          button('Return to previous conversation', () => void switchTarget({ back: true }),
            { variant: 'outline', size: 'sm', disabled: !!voiceOwner || !active || busy })),
        catalogNotice && h('p', { className: 'htd-muted', role: 'status' },
          'The conversation list is unavailable. ' + catalogNotice.text),
        h('div', null, button('Refresh conversations', () => void refreshCatalog(),
          { variant: 'outline', size: 'sm', disabled: loading || busy })),
        taskState && h('label', null, 'Spoken updates',
          h('select', { value: taskState.preferences?.update_mode || 'important',
            disabled: !active || busy,
            onChange: event => void saveUpdatePreference(event.target.value) },
          h('option', { value: 'important' }, 'Completion and important updates'),
          h('option', { value: 'completion' }, 'Completion only'),
          h('option', { value: 'frequent' }, 'Include meaningful milestones'))))));
}
