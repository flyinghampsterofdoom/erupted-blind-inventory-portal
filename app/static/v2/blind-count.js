/* Current-round values only. No prior observations, expected inventory or variances. */
(() => {
  const form = document.getElementById('blind-count-form');
  if (!form) return;
  const status = document.getElementById('count-save-status');
  const login = document.getElementById('count-login'), recover = document.getElementById('count-recover');
  const inputs = [...form.querySelectorAll('[data-field]')];
  const base = `/store/sessions/${form.dataset.sessionId}`;
  const key = `blind-count:${form.dataset.principalId}:${form.dataset.storeId}:${form.dataset.sessionId}`;
  let revision = Number(form.dataset.revision), generation = 0, acknowledged = 0;
  let running = null, timer, pending = null, conflict = false, submitting = false, pendingSubmission = null, retryDelay = 1000;
  const values = () => Object.fromEntries([...form.querySelectorAll('[data-variation-id]')].map(row => [row.dataset.variationId,
    Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input => [input.dataset.field, input.value]))]));
  const csrf = () => decodeURIComponent((document.cookie.split('; ').find(row => row.startsWith('csrf_token=')) || '').slice(11));
  function buffer() {
    try { sessionStorage.setItem(key, JSON.stringify({savedAt: Date.now(), revision, values: values()})); }
    catch (_) { status.textContent = 'Unsaved — browser recovery storage unavailable. Keep this tab open.'; }
  }
  function totals() {
    for (const row of form.querySelectorAll('[data-variation-id]')) {
      const [front, back] = row.querySelectorAll('[data-field]');
      row.querySelector('[data-total]').textContent = front.value !== '' && back.value !== '' && front.validity.valid && back.validity.valid
        ? String(Math.round((Number(front.value) + Number(back.value)) * 1000) / 1000) : '—';
    }
  }
  function pause(message) { status.textContent = message; buffer(); login.hidden = false; }
  async function probe() {
    const response = await fetch('/session-status', {credentials:'same-origin', cache:'no-store', headers:{'X-Requested-With':'autosave'}});
    if (!response.ok) { pause('Session expired. Pending entries remain in this tab. Sign in, then retry.'); return null; }
    const data = await response.json();
    if (String(data.principal_id) !== form.dataset.principalId) { pause('Sign in with the same account before resuming this count.'); return null; }
    if (data.store_id !== null && data.store_id !== undefined && String(data.store_id) !== form.dataset.storeId) { pause('Your store assignment changed. Pending entries are retained; access must be revalidated.'); return null; }
    login.hidden = true; return data;
  }
  async function post(path, payload) {
    const response = await fetch(path, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf(), 'X-Requested-With':'autosave'}, body:JSON.stringify(payload)});
    if (response.status === 401 || response.redirected) { pause('Session expired. Sign in again, then retry.'); throw new Error('Authentication required'); }
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 409) { conflict = true; status.textContent = 'This count changed elsewhere. Pending values are retained; reload and reconcile.'; }
      const error = new Error(data.error || 'Save failed'); error.status = response.status; throw error;
    }
    return data;
  }
  async function drain() {
    if (conflict || pendingSubmission !== null) return false;
    while (acknowledged < generation || pending) {
      if (!await probe()) return false;
      if (!pending) pending = {generation, payload:{revision, operation_id:crypto.randomUUID(), changes:values()}};
      status.textContent = 'Saving…';
      const result = await post(`${base}/draft`, pending.payload);
      revision = result.revision;
      const sent = pending.generation; pending = null;
      if (Object.keys(result.errors || {}).length) {
        status.textContent = 'Valid entries saved. Correct invalid quantities before submitting or logging out.';
        buffer(); return false;
      }
      acknowledged = sent;
      if (acknowledged < generation) { buffer(); continue; }
      retryDelay = 1000; status.textContent = 'Saved';
      try { sessionStorage.removeItem(key); } catch (_) { /* server acknowledgment remains authoritative */ }
    }
    return true;
  }
  async function save() {
    clearTimeout(timer);
    if (running) return running;
    running = drain().catch(() => {
      if (!conflict && login.hidden) status.textContent = 'Save failed — retrying. Keep this tab open.';
      buffer();
      if (!conflict) { timer = setTimeout(() => void save(), retryDelay); retryDelay = Math.min(retryDelay * 2, 15000); }
      return false;
    }).finally(() => { running = null; });
    return running;
  }
  function dirty() {
    if (pendingSubmission !== null) return;
    generation++; totals(); buffer(); status.textContent = 'Unsaved changes';
    clearTimeout(timer); timer = setTimeout(() => void save(), 800);
  }
  inputs.forEach(input => input.addEventListener('input', dirty));
  document.getElementById('count-save').addEventListener('click', () => void save());
  document.querySelector('form[action="/logout"]')?.addEventListener('submit', async event => {
    event.preventDefault();
    inputs.forEach(input => { input.disabled = true; });
    try {
      // Flush even untouched fields, including explicit clears; never treat an attempted save as success.
      if (!pending) generation++;
      const ok = await save();
      if (ok && acknowledged === generation && await probe()) {
        try { sessionStorage.removeItem(key); } catch (_) {}
        event.target.submit();
      } else if (!conflict && login.hidden) status.textContent = 'Logout paused. Save or correct entries, then retry logout.';
    } finally { inputs.forEach(input => { input.disabled = pendingSubmission !== null; }); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    inputs.forEach(input => { input.disabled = true; });
    try {
      if (pendingSubmission === null) {
        if (!pending) generation++;
        if (!await save() || !await probe()) return;
        pendingSubmission = revision;
      } else if (!await probe()) return;
      const result = await post(`${base}/submit`, {revision:pendingSubmission});
      if (result.submitted) { pendingSubmission = null; acknowledged = generation; sessionStorage.removeItem(key); window.location.assign(result.redirect); }
    } catch (error) {
      if (error.status === 400 || error.status === 409) pendingSubmission = null;
      status.textContent = pendingSubmission === null ? error.message : 'Submission unconfirmed. Retry Submit Count; your saved entries are retained.';
      buffer();
    }
    finally { submitting = false; inputs.forEach(input => { input.disabled = pendingSubmission !== null; }); }
  });
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (saved && Date.now() - saved.savedAt < 86400000) {
      recover.hidden = false;
      status.textContent = 'Pending entries found in this tab. Restore after checking this count.';
      recover.addEventListener('click', () => {
        // Explicit recovery against freshly loaded server revision; do not silently overwrite another tab.
        if (!window.confirm('Restore this tab’s pending Front/Back entries onto the displayed count? Check for changes made by another counter first.')) return;
        for (const row of form.querySelectorAll('[data-variation-id]')) for (const input of row.querySelectorAll('[data-field]')) {
          const raw = saved.values?.[row.dataset.variationId]?.[input.dataset.field];
          if (raw !== undefined) input.value = raw;
        }
        recover.hidden = true; dirty();
      });
    } else sessionStorage.removeItem(key);
  } catch (_) { /* leave server-rendered values intact */ }
  totals();
  document.addEventListener('visibilitychange', () => { if (document.hidden && generation > acknowledged) { buffer(); void save(); } });
  window.addEventListener('beforeunload', event => { if (generation > acknowledged || pending || pendingSubmission !== null) { buffer(); event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('online', () => { if (generation > acknowledged) void save(); });
  setInterval(async () => {
    try {
      const data = await probe();
      if (data && Date.parse(data.expires_at) - Date.now() < 30000 && generation > acknowledged) void save();
    } catch (_) { if (generation > acknowledged) buffer(); }
  }, 15000);
})();
