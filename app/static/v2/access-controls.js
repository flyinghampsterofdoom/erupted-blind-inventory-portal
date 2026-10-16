(() => {
  const search = document.querySelector('#access-search');
  if (search) search.addEventListener('input', () => {
    let count = 0;
    document.querySelectorAll('.access-user').forEach(row => {
      row.hidden = !row.dataset.search.includes(search.value.trim().toLowerCase());
      if (!row.hidden) count++;
    });
    document.querySelector('#access-search-status').textContent = `${count} users shown`;
  });
  const form = document.querySelector('[data-access-editor]');
  if (!form) return;
  let dirty = false;
  const initial = new URLSearchParams(new FormData(form)).toString();
  form.addEventListener('input', () => {
    dirty = new URLSearchParams(new FormData(form)).toString() !== initial;
    form.querySelector('[data-save-status]').textContent = dirty ? 'Unsaved changes — select Save Permissions to apply.' : 'No unsaved changes.';
    form.querySelectorAll('select').forEach(select => {
      const status = select.parentElement.querySelector('[data-override-status]');
      if (status) {
        const inherited = select.value === 'DEFAULT';
        const allowed = select.value === 'ALLOW' || (inherited && select.dataset.roleAllowed === 'true');
        status.textContent = `Effective: ${allowed ? 'Allowed' : 'Denied'} · ${inherited ? 'Inherited from role' : 'Individual override'}`;
      }
    });
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  form.addEventListener('submit', async event => {
    if (event.submitter?.hasAttribute('data-reset-permissions') && !window.confirm('Reset this user’s permissions to role defaults?')) { event.preventDefault(); return; }
    event.preventDefault();
    const data = new FormData(form);
    if (event.submitter?.name) data.set(event.submitter.name, event.submitter.value);
    const status = form.querySelector('[data-save-status]');
    const buttons = [...form.querySelectorAll('button[type=submit]')];
    buttons.forEach(button => button.disabled = true);
    status.textContent = 'Saving permissions…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: data, credentials: 'same-origin'});
      if (!response.ok || !response.redirected || new URL(response.url).pathname === '/login') {
        throw new Error('Permissions were not saved. Check your session and try again; your selections remain on this page.');
      }
      dirty = false;
      window.location.assign(response.url);
    } catch (error) {
      dirty = true;
      status.textContent = error.message || 'Could not save. Your selections remain on this page.';
      status.setAttribute('role', 'alert');
      buttons.forEach(button => button.disabled = false);
    }
  });
})();
