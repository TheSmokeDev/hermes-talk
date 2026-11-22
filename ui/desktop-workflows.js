// Read the owning session's catalog only when the caller asks about workflows.
// The realtime model proposes a lookup. Only captured user words supply a command.
function workflowWords(value) {
  return value.toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
}

async function desktopWorkflowCatalog(request, sessionId, protocol, searchText) {
  const scoped = { session_id: sessionId };
  const data = await request('commands.catalog', scoped);
  if (!data?.skills || !Array.isArray(data.pairs) || !Array.isArray(data.categories)) {
    throw new Error('This Hermes host could not provide its session command catalog.');
  }
  const rows = [];
  const reserved = new Set(Object.keys(data.canon || {}));
  const pluginRows = data.categories.find(row => row.name === 'Plugin commands')?.pairs || [];
  const visiblePlugins = new Set(pluginRows.map(([name]) => name));
  const descriptions = new Map(data.pairs);
  const add = (name, kind, detail = '') => {
    if (typeof name === 'string' && /^\/[\p{L}\p{N}_:.-]+$/u.test(name)) {
      rows.push({name, kind, description: String(detail).slice(0, 180)});
    }
  };
  for (const name of Object.keys(data.skills)) add(name, 'skill', descriptions.get(name));
  for (const [key, description] of Object.entries(data.plugin_commands || {})) {
    add('/' + key, 'plugin command', description);
  }
  for (const [name] of data.categories.find(row => row.name === 'User commands')?.pairs || []) {
    // Default quick-command descriptions can contain a shell command or credentials.
    add(name, 'quick command');
  }
  // commands.catalog currently omits bundles. Ask the host's read-only /bundles
  // handler; take command slugs only, never the report's configured descriptions.
  let bundleListing = 'unavailable';
  try {
    if (protocol === 'legacy') {
      const prefix = workflowWords(searchText)[0] || '';
      const reply = await request('complete.slash', {...scoped, text: '/' + prefix});
      for (const item of reply?.items || []) {
        const key = '/' + String(item.text).trim().replace(/^\/+/, '');
        if (item.kind === 'skill' && !Object.hasOwn(data.skills, key)) add(key, 'bundle');
      }
      bundleListing = 'matching host suggestions';
    } else {
      const reply = await request('slash.exec', { ...scoped, command: '/bundles' });
      if (reply?.type === 'exec' && typeof reply.output === 'string') {
        for (const line of reply.output.split('\n')) {
          const match = line.match(/^\s*(?:[•*-]\s+)?\x60?(\/[\p{L}\p{N}_:.-]+)\x60?\s+[—–-]\s/u);
          if (match) add(match[1], 'bundle');
        }
        bundleListing = 'report';
      }
    }
  } catch (_) { /* Older hosts can still supply skills and plugin commands. */ }
  rows.sort((a, b) => a.name.localeCompare(b.name) || a.kind.localeCompare(b.kind));
  return { rows, reserved, incomplete: Boolean(data.plugin_discovery_error || /discovery unavailable/i.test(data.warning || '')),
    bundleListing, visiblePlugins };
}

function findDesktopWorkflows(catalog, query, offset, kind) {
  if (typeof query !== 'string' || query.length > 200 || !Number.isSafeInteger(offset) || offset < 0) {
    throw new Error('Invalid workflow search.');
  }
  const words = workflowWords(query);
  const hits = catalog.rows.filter(row => (!kind || row.kind === kind) &&
    words.every(word => workflowWords(row.name + ' ' + row.description).some(token => token.includes(word))));
  const needle = words.join(' ');
  const rank = row => {
    const name = workflowWords(row.name).join(' ');
    return name === needle ? 0 : name.startsWith(needle) ? 1 : 2;
  };
  hits.sort((a, b) => rank(a) - rank(b));
  return { matches: hits.slice(offset, offset + 8), total: hits.length,
    next_offset: offset + 8 < hits.length ? offset + 8 : null,
    catalog_warning: catalog.incomplete, bundles: catalog.bundleListing };
}

function matchSpokenWorkflow(catalog, text) {
  const intent = text.trim().match(/^(?:(?:hey(?:\s+hermes)?|hermes)[,\s]+)?(?:please\s+)?(?:(?:run|start|invoke)\s+(?:the\s+)?)?(?:slash(?:\s+command)?\s+|\/)(.+)$/iu);
  if (!intent || /\b(?:actually|scratch that|never\s?mind|cancel that|don't run|do not run|no[,\s]+cancel|hold on|wait a (?:second|minute))\b/i.test(intent[1])) {
    throw new Error('Say “run slash” followed by the command name and its arguments in one fresh request.');
  }
  const body = intent[1].trim();
  const separator = /\s+with arguments(?:\s+|$)/i.exec(body);
  const requested = separator ? body.slice(0, separator.index) : body;
  const tokens = Array.from(requested.matchAll(/[\p{L}\p{N}]+/gu));
  const literal = requested.match(/^([\p{L}\p{N}]+(?:[-_:.][\p{L}\p{N}]+)+)(?=\s|$)/u)?.[1];
  const hits = catalog.rows.flatMap(row => {
    if (literal && row.name.slice(1).toLowerCase() !== literal.toLowerCase()) return [];
    const spelled = row.name.replaceAll('-', ' dash ').replaceAll('_', ' underscore ').replaceAll(':', ' colon ');
    for (const words of [workflowWords(spelled), workflowWords(row.name)]) {
      if (separator && words.length !== tokens.length) continue;
      if (!words.length || !words.every((word, i) => tokens[i]?.[0].toLowerCase() === word)) continue;
      const last = tokens[words.length - 1];
      return [{row, end: last.index + last[0].length}];
    }
    return [];
  });
  // A shared prefix can mean a longer command or arguments to a shorter one.
  if (hits.length !== 1) {
    return { status: hits.length ? 'ambiguous' : 'unknown', matches: hits.slice(0, 8).map(hit => hit.row.name) };
  }
  const {row, end} = hits[0];
  if (row.kind === 'quick command' || row.name === '/talk' ||
      (row.kind === 'plugin command' ? !catalog.visiblePlugins.has(row.name) : catalog.reserved.has(row.name))) {
    return { status: 'use_typed_command', command: row.name };
  }
  if (separator) return { row, argument: body.slice(separator.index + separator[0].length) };
  const tail = body.slice(end).replace(/^\s+command(?=[,.:!?\s]|$)/i, '');
  if (tail && !/^(?:\s|[,.:!?](?:\s|$))/.test(tail)) return {status: 'unknown'};
  return { row, argument: tail.replace(/^[,:]\s+/, '').trim().replace(/^[.!?]$/, '') };
}

async function desktopSlashWorkflow(args, {request, sessionId, sessionProtocol, takeUtterance, safeText}) {
  const running = args?.run === true;
  if (!running && args?.kind === 'plugin') {
    const result = await request('plugins.manage', {action: 'list'});
    if (!Array.isArray(result?.plugins)) throw new Error('Plugin inventory unavailable on this host.');
    const rows = result.plugins.filter(row => typeof row?.name === 'string').map(row => ({
      name: safeText(row.name).slice(0, 120), kind: 'plugin', enabled: row.enabled === true,
      description: safeText(row.description || '').slice(0, 180),
    }));
    return findDesktopWorkflows({rows}, args.query ?? '', args.offset ?? 0, 'plugin');
  }
  // Capture before the first asynchronous read, and recheck just before dispatch.
  const spoken = running ? await takeUtterance() : null;
  const protocol = await sessionProtocol();
  const searchText = running ? spoken.text.replace(/^.*?\bslash(?:\s+command)?\s+/i, '') : args?.query || '';
  const catalog = await desktopWorkflowCatalog(request, sessionId, protocol, searchText);
  if (!running) {
    const result = findDesktopWorkflows(catalog, args?.query ?? '', args?.offset ?? 0, args?.kind);
    result.matches = result.matches.map(row => ({...row, description: safeText(row.description)}));
    return result;
  }
  if (catalog.incomplete) return {status: 'catalog_warning', detail: 'Check the native command picker before running.'};
  const selected = matchSpokenWorkflow(catalog, spoken.text);
  if (!selected.row) return selected;
  const {name, kind} = selected.row;
  spoken.check();
  // No retry after an unknown command outcome. A plugin handler may already have run.
  let result;
  try {
    result = await request('slash.exec', {session_id: sessionId, command: name +
      (selected.argument ? ' ' + selected.argument : '')});
  } catch (error) {
    if (protocol === 'legacy' && kind === 'skill' && error?.code === 4018 &&
        String(error.message).includes('skill command: use command.dispatch for ' + name)) {
      spoken.check();
      try { result = await request('command.dispatch', {session_id: sessionId, name: name.slice(1), arg: selected.argument}); }
      catch (_) { return {status: 'unconfirmed', command: name}; }
    } else if (String(error?.message).includes('unsupported_command')) {
      return {status: 'unsupported_by_host', command: name};
    } else { return {status: 'unconfirmed', command: name, detail: 'Check the session before retrying.'}; }
  }
  if (result?.type === 'exec' || result?.type === 'plugin' ||
      (protocol === 'legacy' && result?.type == null && typeof result?.output === 'string')) {
    return {status: 'handler_returned', command: name, report: safeText(result.output ?? '').slice(0, 1200)};
  }
  if (!['skill', 'send'].includes(result?.type) || typeof result.message !== 'string' || !result.message) {
    return {status: 'needs_native_control', command: name, detail: 'Inspect the command in Hermes.'};
  }
  spoken.check();
  let receipt;
  try {
    receipt = await request('prompt.submit', {session_id: sessionId, text: result.message, queued: true,
      ...(protocol === 'canonical' ? {submission_id: 'talk-slash-' + spoken.id} : {})});
  } catch (_) { return {status: 'unconfirmed', command: name, detail: 'Check the session before retrying.'}; }
  const accepted = protocol === 'canonical' ? receipt?.ref?.session_id === sessionId && receipt.admission_id &&
    ['started', 'queued', 'terminal'].includes(receipt.status) : ['streaming', 'queued'].includes(receipt?.status);
  return {status: accepted ? receipt.status : 'unconfirmed', command: name, kind,
    admission_id: accepted ? receipt.admission_id : undefined, notice: safeText(result.notice || '').slice(0, 400)};
}
