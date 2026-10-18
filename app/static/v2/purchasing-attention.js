(() => {
  'use strict';
  const root = document.getElementById('purchasing-attention');
  if (!root) return;
  const by = id => document.getElementById(id);
  const identity = row => `${row.kind}:${row.key}`;
  const locked = action => navigator.locks?.request ? navigator.locks.request(storageKey, action) : Promise.resolve().then(action);
  const storageKey = `erupted:funding-draft:v1:${root.dataset.owner}`;
  let view = {rows: [], resolved_rows: [], accounts: []};
  let changes = {}, pending = null, stored = null, mode = 'unresolved';
  let busy = false, external = false, storageFailed = false, loaded = false;
  const status = (message, error = false) => {
    by('attention-status').textContent = message;
    by('attention-status').className = error ? 'v2-error' : '';
  };
  const allRows = () => [...view.rows, ...(view.resolved_rows || [])];
  const rowMap = () => new Map(allRows().map(row => [identity(row), row]));
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function readDraft() {
    try {
      stored = localStorage.getItem(storageKey);
      const draft = stored ? JSON.parse(stored) : {};
      if (draft.version !== undefined && draft.version !== 1) throw new Error('Unsupported draft');
      changes = draft.changes || {};
      pending = draft.pending || null;
      external = false; storageFailed = false;
    } catch (_) {
      storageFailed = true;
      status('Draft storage is unavailable or unreadable. Keep this page open; selections cannot safely survive navigation.', true);
    }
  }
  function persist() {
    try {
      if (localStorage.getItem(storageKey) !== stored) {
        external = true;
        status('This draft changed in another tab. Refresh evidence to load that draft before editing or saving.', true);
        return false;
      }
      const next = Object.keys(changes).length || pending ? JSON.stringify({version: 1, changes, pending}) : null;
      if (next) localStorage.setItem(storageKey, next);
      else localStorage.removeItem(storageKey);
      stored = next; storageFailed = false;
      return true;
    } catch (_) {
      storageFailed = true;
      status('Selections remain on this page, but draft storage failed. Keep this page open until saved.', true);
      return false;
    }
  }
  function baseValue(row) {
    return row.kind === 'COST' ? String(row.saved_value?.unit_cost ?? '') : String(row.saved_value?.account_id ?? '');
  }
  function stale() {
    const rows = rowMap();
    return Object.entries(changes).some(([id, change]) => {
      const row = rows.get(id);
      return !row || row.version !== change.version || row.token !== change.token;
    });
  }
  function controls() {
    const values = Object.values(changes);
    const skus = new Set(values.filter(c => c.kind !== 'CARD').map(c => c.variation_id)).size;
    const configs = values.filter(c => c.kind === 'CARD').length;
    const label = `${skus} SKU${skus === 1 ? '' : 's'}${configs ? `, ${configs} configuration` : ''}`;
    by('attention-save').textContent = busy ? 'Saving / loading…' : pending ? `Retry Save Changes (${label})` : `Save Changes (${label})`;
    by('attention-save').disabled = busy || external || !loaded || !values.length || (!pending && stale());
    by('attention-discard').disabled = busy || external || !!pending || !values.length;
    by('attention-reload').disabled = busy || !!pending;
    by('attention-rebase').hidden = !loaded || !stale() || !!pending || external;
    by('attention-unresolved').setAttribute('aria-pressed', String(mode === 'unresolved'));
    by('attention-resolved').setAttribute('aria-pressed', String(mode === 'resolved'));
  }
  function edit(row, value) {
    if (external || pending || busy) return;
    // Check storage before changing memory, so an older tab cannot overwrite it.
    try { if (localStorage.getItem(storageKey) !== stored) { external = true; controls(); draw(); status('Another tab updated this draft. Refresh evidence before editing.', true); return; } } catch (_) { storageFailed = true; }
    const id = identity(row);
    if (value === baseValue(row) || value === '') delete changes[id];
    else changes[id] = {...(changes[id] || {kind: row.kind, key: row.key, version: row.version, token: row.token, variation_id: row.variation_id}),
      ...(row.kind === 'COST' ? {unit_cost: value} : {account_id: Number(value)})};
    const savedLocally = persist();
    if (savedLocally) status(Object.keys(changes).length ? 'Unsaved selections preserved in this browser. Choose Save Changes when ready.' : 'No unsaved changes.');
    controls();
    rowElementState(id);
  }
  function rowElementState(id) {
    document.querySelectorAll('[data-decision]').forEach(element => {
      if (element.dataset.decision === id) element.classList.toggle('attention-modified', !!changes[id]);
    });
  }
  function input(row) {
    const element = node(row.kind === 'COST' ? 'input' : 'select');
    element.setAttribute('aria-label', `${row.kind === 'COST' ? 'Verified unit cost' : 'Funding account'} for ${row.sku}`);
    element.disabled = busy || external || !!pending;
    if (row.kind === 'COST') { element.type = 'number'; element.min = '0'; element.step = '.0001'; }
    else {
      element.add(new Option('Select existing account', ''));
      view.accounts.filter(a => row.kind !== 'CARD' || a.type === 'CREDIT_CARD').forEach(a => element.add(new Option(a.name, String(a.id))));
    }
    const draft = changes[identity(row)];
    element.value = draft ? String(row.kind === 'COST' ? draft.unit_cost : draft.account_id) : baseValue(row);
    if (draft && element.value === '') status('A selected account is no longer available. Refresh and review the draft; no selection was discarded.', true);
    element.addEventListener('change', () => locked(() => edit(row, element.value)));
    return element;
  }
  function draw() {
    const query = by('attention-search').value.trim().toLocaleLowerCase();
    const matches = row => `${row.product} ${row.sku} ${row.variation_id}`.toLocaleLowerCase().includes(query);
    const source = mode === 'resolved' ? (view.resolved_rows || []) : view.rows.filter(r => r.kind === 'FINANCING');
    const rows = source.filter(matches), fragment = document.createDocumentFragment();
    rows.forEach(row => {
      const tr = node('tr'); tr.dataset.decision = identity(row);
      if (changes[identity(row)]) tr.className = 'attention-modified';
      tr.append(node('td', row.product), node('td', row.sku));
      const current = node('td'); current.append(node('span', row.current_assignment));
      if (row.prospective_only) current.append(node('small', 'Current ownership incomplete; selection applies to future purchases only.', 'v2-muted'));
      current.title = row.reason;
      const choice = node('td'); choice.append(input(row)); tr.append(current, choice); fragment.append(tr);
    });
    if (!rows.length) { const tr = node('tr'), td = node('td', loaded ? `No ${mode} funding SKUs match this view.` : 'Loading funding assignments…'); td.colSpan = 4; tr.append(td); fragment.append(tr); }
    by('attention-rows').replaceChildren(fragment);
    by('attention-count').textContent = `${rows.length} of ${source.length} ${mode} funding SKUs · ${Object.keys(changes).length} modified input(s) across views`;
    const other = view.rows.filter(r => r.kind !== 'FINANCING'), otherFragment = document.createDocumentFragment();
    other.filter(matches).forEach(row => {
      const tr = node('tr'); tr.dataset.decision = identity(row);
      if (changes[identity(row)]) tr.className = 'attention-modified';
      tr.append(node('td', `${row.product} · ${row.sku}`), node('td', row.reason));
      const td = node('td'); td.append(row.kind === 'WEEKLY_COGS' ? node('span', 'Review dated financing evidence; current SKU choices cannot change past sales.') : input(row)); tr.append(td); otherFragment.append(tr);
    });
    by('attention-other-rows').replaceChildren(otherFragment);
    by('attention-other-count').textContent = `Other data issues (${other.length})`;
    controls();
  }
  async function load() {
    if (busy || pending) return;
    if (external) readDraft();
    busy = true; draw();
    try {
      const response = await fetch('/management/purchasing/attention/workspace', {credentials: 'same-origin', cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Unable to load current evidence');
      view = data; loaded = true;
      if (!storageFailed) status(stale() ? 'Evidence changed. Your selections remain preserved; review current assignments before saving.' : Object.keys(changes).length ? 'Unsaved selections restored. Save Changes applies only modified inputs.' : 'Ready. Only modified inputs will be saved.');
    } catch (error) { status(error.message + '. Existing selections remain preserved.', true); }
    finally { busy = false; draw(); }
  }
  by('attention-save').onclick = () => locked(async () => {
    if (busy || external || !loaded || (!pending && stale())) return;
    if (!pending) {
      // One immutable request identity/body is retained across uncertain retries.
      pending = {request_key: crypto.randomUUID(), changes: Object.values(changes).map(({variation_id, ...change}) => change)};
      if (!persist() && external) { pending = null; draw(); return; }
    }
    busy = true; draw(); status('Saving changes…');
    try {
      const response = await fetch('/management/purchasing/attention', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': root.dataset.csrf}, body: JSON.stringify(pending)});
      const data = await response.json();
      if (!response.ok) {
        // Validation/conflict/permission responses from this endpoint roll back.
        if ([400, 403, 409, 422].includes(response.status)) { pending = null; persist(); }
        throw new Error(data.detail || 'Save failed');
      }
      changes = {}; pending = null;
      const cleared = persist();
      if (!cleared && external) { status('Saved on the server. Another tab has a newer draft; refresh to load it.', true); return; }
      busy = false; await load();
      if (!storageFailed) status(`Saved ${data.count} input(s). Resolved funding SKUs are in Resolved; mixed historical ownership remains unresolved.`);
    } catch (error) {
      status(`${error.message}. Selections are preserved. ${pending ? 'Retry Save Changes before further edits; the same request identity prevents duplicate saves.' : 'Refresh evidence to review conflicts before retrying.'}`, true);
    } finally { busy = false; draw(); }
  });
  by('attention-rebase-yes').onclick = () => locked(() => {
    if (busy || external || pending) return;
    const rows = rowMap();
    if (Object.keys(changes).some(id => !rows.has(id))) { status('Some draft inputs are no longer available. Discard explicitly and reselect the remaining inputs; nothing has been saved.', true); return; }
    Object.entries(changes).forEach(([id, change]) => { const row = rows.get(id); changes[id] = {...change, version: row.version, token: row.token}; });
    if (persist()) status('Selections retained against the reviewed current evidence. Save Changes to apply them.');
    draw();
  });
  by('attention-discard').onclick = () => { by('attention-discard-confirm').hidden = false; };
  by('attention-discard-no').onclick = () => { by('attention-discard-confirm').hidden = true; };
  by('attention-discard-yes').onclick = () => locked(() => {
    if (pending || busy || external) return;
    changes = {}; persist(); by('attention-discard-confirm').hidden = true; draw();
    if (!external && !storageFailed) status('Unsaved selections discarded. Saved assignments were not changed.');
  });
  by('attention-search').oninput = draw;
  by('attention-unresolved').onclick = () => { mode = 'unresolved'; draw(); };
  by('attention-resolved').onclick = () => { mode = 'resolved'; draw(); };
  by('attention-reload').onclick = load;
  addEventListener('beforeunload', event => { if (storageFailed && Object.keys(changes).length) { event.preventDefault(); event.returnValue = ''; } });
  addEventListener('storage', event => {
    if (event.key !== storageKey || event.newValue === stored) return;
    external = true; draw(); status('This draft changed in another tab. Refresh evidence to load it before editing or saving.', true);
  });
  readDraft();
  // An uncertain saved request must first load the workspace, then permit only retry.
  const retry = pending; pending = null;
  load().finally(() => { if (retry) { pending = retry; draw(); status('An interrupted save is preserved. Retry Save Changes using its original request identity.', true); } });
})();
