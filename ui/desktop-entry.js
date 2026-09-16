import * as HermesSDK from '@hermes/plugin-sdk'
import * as React from 'react'

const TALK_CSS = __HERMES_TALK_CSS__;
const DESKTOP_API = '/api/plugins/hermes-talk';
const h = React.createElement;
let desktopContext = null;
const desktopOpeners = new Set();

function ownerKey(owner) {
  return JSON.stringify([owner?.connectionId, owner?.profile,
    owner?.sessionId ?? null, owner?.storedSessionId ?? null]);
}

export function desktopAvailability(controller) {
  if (!controller || controller.capabilities?.microphoneLease !== 1 ||
      controller.capabilities?.pinnedRest !== 1 || controller.capabilities?.prepareSession !== 1) {
    return 'Update Hermes Desktop to use Talk in this conversation.';
  }
  if (typeof controller.acquire !== 'function' ||
      typeof controller.owner?.connectionId !== 'string' || !controller.owner.connectionId ||
      typeof controller.owner?.profile !== 'string' || !controller.owner.profile) {
    return 'Open a connected Hermes conversation before starting Talk.';
  }
  return '';
}

export function createDesktopTalkSDK(context, controller, onPreparing = () => {}) {
  const currentController = typeof controller === 'function' ? controller : () => controller;
  const unavailable = desktopAvailability(currentController());
  if (unavailable) throw new Error(unavailable);
  let owner = Object.freeze({ ...currentController().owner });
  const scope = Object.freeze({ connectionId: owner.connectionId, profile: owner.profile });
  const sdk = {
    React,
    hooks: React,
    components: { Button: HermesSDK.Button, Input: HermesSDK.Input },
    managedAuthentication: true,
    get lifetimeSignal() { return currentController()?.signal; },
    get desktopOwner() { return owner; },
    stopHost() { currentController()?.stop?.(); },
    async prepareTask({ tabId, signal }) {
      const current = currentController();
      const original = owner;
      if (current?.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      if (ownerKey(current?.owner) !== ownerKey(original) || signal?.aborted) {
        throw new Error('The Hermes conversation changed. Reopen Talk in the selected conversation.');
      }
      if (current.capabilities?.prepareSession !== 1 || typeof current.prepareSession !== 'function') {
        throw new Error('Update Hermes Desktop to start Talk in this conversation.');
      }
      let prepared = null;
      onPreparing(true);
      try {
        prepared = await current.prepareSession();
        if (signal?.aborted || current.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
        if (!prepared?.storedSessionId || prepared.connectionId !== scope.connectionId ||
            prepared.profile !== scope.profile || (original.storedSessionId &&
              prepared.storedSessionId !== original.storedSessionId) ||
            (current.signal && ownerKey(prepared) !== ownerKey(original))) {
          throw new Error('The Hermes conversation changed. Reopen Talk in the selected conversation.');
        }
        // React must publish the prepared owner before its current lease can be used.
        const deadline = Date.now() + 1000;
        while (ownerKey(currentController()?.owner) !== ownerKey(prepared) && Date.now() < deadline) {
          if (signal?.aborted || current.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
          await new Promise(resolve => window.setTimeout(resolve, 10));
        }
        if (ownerKey(currentController()?.owner) !== ownerKey(prepared)) {
          throw new Error('The Hermes conversation changed. Reopen Talk in the selected conversation.');
        }
        owner = Object.freeze({ ...prepared });
        const response = await sdk.fetchJSON(DESKTOP_API + '/targets', {
          method: 'POST', signal,
          body: JSON.stringify({ peer_id: 'local', profile: owner.profile,
            session_id: owner.storedSessionId, tab_id: tabId }),
        });
        if (signal?.aborted || ownerKey(currentController()?.owner) !== ownerKey(owner)) {
          throw new DOMException('Request cancelled', 'AbortError');
        }
        const matches = (response?.ok && Array.isArray(response.targets) ? response.targets : [])
          .filter(target => target.peer_id === 'local' && target.profile === owner.profile &&
            target.session_id === owner.storedSessionId);
        if (matches.length !== 1) throw new Error('The current conversation is unavailable. Reopen it and try again.');
        return matches[0];
      } finally {
        onPreparing(false, owner);
      }
    },
    validateVoiceMode(status) {
      if (!status || !['live', 'native'].includes(status.voiceMode)) {
        throw new Error('Desktop Talk supports GPT-Live and OpenAI Realtime. ' +
          'Use the dashboard for cascade audio. Your voice settings have not been changed.');
      }
    },
    async acquireMicrophone({ signal } = {}) {
      const current = currentController();
      if (signal?.aborted || current?.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      if (desktopAvailability(current) || ownerKey(current.owner) !== ownerKey(owner)) {
        throw new Error('The Hermes conversation changed. Reopen Talk in the selected conversation.');
      }
      const lease = await current.acquire({ signal });
      if (!lease || typeof lease.release !== 'function' || !lease.signal) {
        lease?.release?.();
        throw new Error('Hermes could not grant microphone ownership. Stop its other voice session first.');
      }
      if (signal?.aborted || current.signal?.aborted || lease.signal.aborted ||
          ownerKey(currentController()?.owner) !== ownerKey(owner)) {
        lease.release();
        throw new DOMException('Request cancelled', 'AbortError');
      }
      return lease;
    },
    async fetchJSON(path, options = {}, timeoutMs) {
      if (!path.startsWith(DESKTOP_API + '/')) {
        throw new Error('Talk attempted to access a different plugin route.');
      }
      if (options.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      const suffix = path.slice(DESKTOP_API.length);
      const headers = new Headers(options.headers || {});
      // Keep in-flight receipts on their captured owner, including a late /close.
      // The shared UI's generation checks retire results after navigation.
      try {
        return await context.rest(suffix, {
          method: options.method,
          body: typeof options.body === 'string' ? JSON.parse(options.body) : options.body,
          timeoutMs: timeoutMs || 30000,
          scope,
          pluginToken: headers.get('x-talk-token') || undefined,
        });
      } catch (error) {
        const prefix = "Error invoking remote method 'hermes:api': Error: ";
        const normalized = typeof error?.message === 'string' && error.message.startsWith(prefix)
          ? new Error(error.message.slice(prefix.length)) : error;
        if (/^(?:401|403)\b/.test(normalized?.message || '')) sdk.stopHost();
        throw normalized;
      }
    },
  };
  return sdk;
}

function DesktopTalkPresentation(props) {
  const { active, starting, error, needsToken, popoverOpen, onPopoverOpenChange, stopTalk } = props;
  const previous = React.useRef({ active: false, starting: false, error: '', needsToken: false });
  React.useEffect(() => {
    const before = previous.current;
    previous.current = { active, starting, error, needsToken };
    const failed = Boolean(error || needsToken);
    if (failed && (error !== before.error || needsToken !== before.needsToken ||
        (before.starting && !starting && !active))) {
      onPopoverOpenChange(true);
    } else if (active && !before.active) {
      onPopoverOpenChange(false);
    }
  }, [active, starting, error, needsToken, onPopoverOpenChange]);

  return h(React.Fragment, null,
    (active || starting) && h('span', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '0.375rem' },
    },
    h('span', { role: 'status', 'aria-live': 'polite', style: { fontSize: '0.75rem' } },
      active ? 'Connected' : 'Connecting…'),
    h(HermesSDK.Button, {
      type: 'button', variant: 'ghost', size: 'sm',
      'aria-label': starting ? 'Cancel Talk connection' : 'Stop talking', onClick: stopTalk,
    }, starting ? 'Cancel' : 'Stop')),
    popoverOpen && h(HermesSDK.PopoverContent, {
      side: 'top', align: 'end', 'aria-label': 'Hermes Talk',
      style: { width: 'min(360px, calc(100vw - 24px))', maxHeight: '70vh',
        overflowY: 'auto', padding: '1rem' },
      onSubmit: event => event.stopPropagation(),
    }, h('p', { role: 'status', style: { fontSize: '.75rem', marginBottom: '.75rem' } },
      'Composer mode · update Hermes Desktop for a persistent floating Talk window.'),
    h(DesktopTalkView, props)));
}

const TALK_HUD_CSS = `
.ht-hud { display:grid; gap:.5rem; color:inherit; font:inherit; --ht-hud-surface:var(--background, Canvas); }
.ht-hud-compact { position:relative; display:flex; align-items:center; gap:.5rem; }
.ht-hud-toggle { min-width:3rem; min-height:3rem; border-radius:50%; font:inherit; color:inherit; background:var(--ht-hud-surface); border:1px solid currentColor; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.35); }
.ht-hud-toggle:focus-visible { outline:2px solid currentColor; outline-offset:3px; }
.ht-hud-preview { display:none; margin:0; padding:.375rem .625rem; border-radius:.5rem; background:var(--ht-hud-surface); font-size:.75rem; overflow-wrap:anywhere; }
.ht-hud-compact:hover .ht-hud-preview, .ht-hud-compact:focus-within .ht-hud-preview { display:block; }
.ht-hud-panel { width:min(380px,calc(100vw - 24px)); max-height:calc(100vh - 5rem); overflow:auto; padding:.75rem; border-radius:.75rem; background:var(--ht-hud-surface); }
.ht-hud[data-skin="contrast"] { color:CanvasText; --ht-hud-surface:Canvas; }
.ht-hud[data-animate="true"][data-active="true"] .ht-hud-toggle { animation:ht-hud-connected 2s ease-in-out infinite; }
@keyframes ht-hud-connected { 50% { border-color:transparent; } }
@media (prefers-reduced-motion:reduce) { .ht-hud-toggle { animation:none !important; } }
`;

function TalkHudPresentation(props) {
  const { active, starting, muted, sleeping, taskState, selectedRecipient, recipients = [],
    expanded, setExpanded, appearance = {} } = props;
  const toggleRef = React.useRef(null);
  // hoverOpen: the panel is showing only because the pointer is over it; a click pins it.
  // pinned: the operator opened or pinned the panel themselves; a reconnect must not take it away.
  const hoverOpen = React.useRef(false);
  const pinned = React.useRef(false);
  const wasActive = React.useRef(active);
  const collapseOnConnect = appearance.collapseOnConnect !== false;
  const hoverExpand = appearance.hoverExpand !== false;
  React.useEffect(() => {
    const connected = active && !wasActive.current;
    wasActive.current = active;
    if (connected && collapseOnConnect && !pinned.current) { hoverOpen.current = false; setExpanded(false); }
  }, [active, collapseOnConnect, setExpanded]);
  const toggle = () => {
    if (!expanded) { hoverOpen.current = false; pinned.current = true; setExpanded(true); return; }
    if (hoverOpen.current) { hoverOpen.current = false; pinned.current = true; return; }
    pinned.current = false;
    setExpanded(false);
  };
  const enter = () => {
    if (hoverExpand && !expanded) { hoverOpen.current = true; setExpanded(true); }
  };
  const leave = event => {
    if (!hoverOpen.current) return;
    const focusInside = typeof document !== 'undefined' && document.activeElement &&
      event?.currentTarget?.contains?.(document.activeElement);
    if (focusInside) return;
    hoverOpen.current = false;
    setExpanded(false);
  };
  const recipient = recipients.find(row => row.recipient_id === selectedRecipient);
  const addressed = recipient ? desktopTalkRecipientLabel(recipient)
    : selectedRecipient ? 'Unavailable recipient · ' + selectedRecipient : 'Hermes · voice owner';
  const activeWork = (taskState?.jobs || []).filter(job =>
    ['queued', 'pending', 'accepted', 'running', 'waiting_approval', 'waiting_for_approval',
      'approval_required', 'paused'].includes(job.status)).length;
  const state = starting ? 'Connecting…' : sleeping ? 'Sleeping · microphone off'
    : active && props.audioActivity?.output ? 'Audio playing'
    : muted && active ? 'Microphone muted'
    : active && props.audioActivity?.input ? 'Microphone audio detected'
    : active ? 'Connected · microphone on' : 'Microphone off';
  const status = state + ' · Addressed: ' + addressed + ' · Active work: ' + activeWork;
  const collapse = () => {
    hoverOpen.current = false; pinned.current = false; setExpanded(false); toggleRef.current?.focus();
  };
  return h('section', { className: 'ht-hud', 'aria-label': 'Hermes Talk floating control',
    'data-skin': appearance.skin || 'system', 'data-animate': String(appearance.animate === true),
    'data-active': String(Boolean(active && !muted && !sleeping)),
    'data-hover-open': String(expanded && hoverOpen.current),
    onPointerEnter: enter, onPointerLeave: leave,
    onKeyDown: event => {
      if (event.key === 'Escape' && expanded) { event.preventDefault(); collapse(); }
    } },
    h('style', null, TALK_HUD_CSS),
    h('div', { className: 'ht-hud-compact' },
      h('button', { type: 'button', className: 'ht-hud-toggle', ref: toggleRef,
        // The host moves the window as soon as a press on this button travels; a still tap toggles.
        'data-hud-drag': 'move',
        'aria-label': (expanded ? 'Collapse Talk' : 'Expand Talk') + ' · ' + status,
        'aria-expanded': expanded, 'aria-controls': 'hermes-talk-hud-panel',
        'aria-describedby': 'hermes-talk-hud-status', onClick: toggle }, 'Talk'),
      h('p', { id: 'hermes-talk-hud-status', className: 'ht-hud-preview', role: 'status' }, status)),
    expanded && h('div', { id: 'hermes-talk-hud-panel', className: 'ht-hud-panel',
      onSubmit: event => event.stopPropagation() }, h(DesktopTalkView, { ...props, collapse })));
}

function TalkHudRuntime({ context, controller }) {
  const controllerRef = React.useRef(controller);
  const pinnedOwner = React.useRef(ownerKey(controller.owner));
  const [expanded, setExpanded] = React.useState(true);
  controllerRef.current = controller;
  const surface = React.useMemo(() => createTalkSurface(
    createDesktopTalkSDK(context, () => controllerRef.current)), [context]);
  React.useEffect(() => {
    if (ownerKey(controller.owner) !== pinnedOwner.current) controller.stop();
  }, [controller]);
  return h(React.Fragment, null, h('style', null, TALK_CSS),
    h(surface.TalkPage, { presentation: TalkHudPresentation,
      presentationProps: { expanded, setExpanded } }));
}

function persistentTalkAvailable(context) {
  return context?.voice?.available === true && typeof context.voice.open === 'function' &&
    typeof context.voice.register === 'function';
}

function DesktopTalkLauncher() {
  const useController = HermesSDK.useComposerVoiceController || (() => null);
  const controller = useController();
  const controllerRef = React.useRef(controller);
  controllerRef.current = controller;
  const pendingRef = React.useRef(false);
  const mountedRef = React.useRef(true);
  const [opening, setOpening] = React.useState(false);
  const [error, setError] = React.useState('');
  const unavailable = desktopAvailability(controller);
  const open = async () => {
    const current = controllerRef.current;
    if (pendingRef.current || desktopAvailability(current) || !persistentTalkAvailable(desktopContext)) return;
    const owner = { ...current.owner };
    const context = desktopContext;
    pendingRef.current = true;
    setOpening(true);
    setError('');
    try {
      const prepared = await current.prepareSession();
      if (!prepared || ['connectionId', 'profile', 'sessionId', 'storedSessionId']
        .some(key => typeof prepared[key] !== 'string' || !prepared[key]) ||
          prepared.connectionId !== owner.connectionId || prepared.profile !== owner.profile ||
          (owner.storedSessionId && prepared.storedSessionId !== owner.storedSessionId)) {
        throw new Error('The Hermes conversation changed.');
      }
      const deadline = Date.now() + 1000;
      while (mountedRef.current && ownerKey(controllerRef.current?.owner) !== ownerKey(prepared) &&
          Date.now() < deadline) {
        await new Promise(resolve => window.setTimeout(resolve, 10));
      }
      if (!mountedRef.current || ownerKey(controllerRef.current?.owner) !== ownerKey(prepared) ||
          context !== desktopContext) {
        throw new Error('The Hermes conversation changed.');
      }
      await context.voice.open(Object.freeze({ ...prepared }));
    } catch (_) {
      if (mountedRef.current) {
        setError('Talk could not open. Stop any other floating voice session, then reopen this conversation and try again.');
      }
    } finally {
      pendingRef.current = false;
      if (mountedRef.current) setOpening(false);
    }
  };
  React.useEffect(() => {
    mountedRef.current = true;
    const entry = { owner: () => controllerRef.current?.owner, open };
    desktopOpeners.add(entry);
    return () => { mountedRef.current = false; desktopOpeners.delete(entry); };
  }, []);
  return h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '.5rem' } },
    h(HermesSDK.Button, { type: 'button', variant: 'ghost', size: 'sm', disabled: opening || !!unavailable,
      title: unavailable || 'Open the floating Talk window. Audio stays off until Connect.',
      'aria-label': 'Open floating Hermes Talk', onClick: () => void open() }, opening ? 'Opening…' : 'Talk'),
    error && h('span', { role: 'alert', style: { fontSize: '.75rem' } }, error));
}

function DesktopTalkPanel({ context, controller, onPreparing, presentationProps }) {
  const controllerRef = React.useRef(controller);
  controllerRef.current = controller;
  const surface = React.useMemo(() => createTalkSurface(
    createDesktopTalkSDK(context, () => controllerRef.current, onPreparing)), [context]);
  return h(React.Fragment, null,
    h('style', null, TALK_CSS),
    h(surface.TalkPage, { presentation: DesktopTalkPresentation, presentationProps }));
}

export function openFocusedTalk() {
  const state = HermesSDK.host?.state;
  const focused = state?.focusedSessionOwner?.get();
  const stored = state?.focusedStoredSessionId?.get() || null;
  const runtime = state?.focusedSessionId?.get() || null;
  const matches = [...desktopOpeners].filter(entry => {
    const candidate = entry.owner();
    return focused && candidate?.connectionId === focused.connectionId &&
      candidate?.profile === focused.profile && (candidate?.storedSessionId || null) === stored &&
      (candidate?.sessionId || null) === runtime;
  });
  if (matches.length === 1) matches[0].open();
  else HermesSDK.host?.notify('Open a connected conversation, then choose Talk beside its message box.');
}

function DesktopTalkAction() {
  if (persistentTalkAvailable(desktopContext)) return h(DesktopTalkLauncher);
  const useController = HermesSDK.useComposerVoiceController || (() => null);
  const controller = useController();
  const [attached, setAttached] = React.useState(null);
  const [popoverOpen, setPopoverOpen] = React.useState(false);
  const controllerRef = React.useRef(controller);
  controllerRef.current = controller;
  const preparingRef = React.useRef(false);
  const currentOwner = ownerKey(controller?.owner);
  const unavailable = desktopAvailability(controller);
  const attachedHere = attached !== null &&
    (attached.expectedKey === currentOwner || preparingRef.current);
  const openPanel = () => {
    const key = ownerKey(controllerRef.current?.owner);
    setAttached(previous => previous && (previous.expectedKey === key || preparingRef.current)
      ? previous : { initialKey: key, expectedKey: key });
    setPopoverOpen(true);
  };
  const onPopoverOpenChange = value => {
    if (value) openPanel();
    else setPopoverOpen(false);
  };
  React.useEffect(() => {
    const entry = { owner: () => controllerRef.current?.owner, open: openPanel };
    desktopOpeners.add(entry);
    return () => desktopOpeners.delete(entry);
  }, []);
  React.useEffect(() => {
    if (attached && attached.expectedKey !== currentOwner && !preparingRef.current) {
      setAttached(null);
      setPopoverOpen(false);
    }
  }, [currentOwner, attached]);
  const onPreparing = (value, nextOwner) => {
    preparingRef.current = value;
    if (!value) {
      const expectedKey = ownerKey(nextOwner);
      setAttached(previous => previous && expectedKey === ownerKey(controllerRef.current?.owner)
        ? { ...previous, expectedKey } : null);
    }
  };

  return h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '0.25rem' } },
    h(HermesSDK.Popover, {
      modal: false, open: popoverOpen && attachedHere, onOpenChange: onPopoverOpenChange,
    },
    h(HermesSDK.PopoverTrigger, { asChild: true },
      h(HermesSDK.Button, {
        type: 'button', variant: 'ghost', size: 'sm', title: 'Open Hermes Talk',
        'aria-label': 'Open Hermes Talk',
      }, 'Talk')),
    attachedHere && (unavailable || !desktopContext
      ? popoverOpen && h(HermesSDK.PopoverContent, {
        side: 'top', align: 'end', 'aria-label': 'Hermes Talk',
        style: { width: 'min(360px, calc(100vw - 24px))' },
      }, h('p', { role: 'status' }, unavailable || 'The Talk plugin is not ready.'))
      : h(DesktopTalkPanel, {
        key: attached.initialKey, context: desktopContext, controller, onPreparing,
        presentationProps: { popoverOpen, onPopoverOpenChange },
      }))));
}

export default {
  id: 'hermes-talk',
  name: 'Hermes Talk',
  description: 'GPT-Live subscription or explicit API voice, with Hermes task delegation.',
  register(context) {
    desktopContext = context;
    if (persistentTalkAvailable(context)) {
      context.voice.register(({ controller }) => h(TalkHudRuntime, { context, controller }));
    }
    context.register({
      id: 'talk', area: 'composer.actions', order: 45,
      render: () => h(DesktopTalkAction),
    });
    context.register({
      id: 'talk-topbar', area: 'titleBar.tools.right', order: 45,
      data: { id: 'hermes-talk', label: 'Talk', title: 'Talk to Hermes',
        icon: h(HermesSDK.Codicon, { name: 'mic' }), onSelect: openFocusedTalk },
    });
    context.onDispose(() => {
      if (desktopContext === context) desktopContext = null;
    });
  },
};
