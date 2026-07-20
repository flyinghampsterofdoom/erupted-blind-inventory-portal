(() => {
  const selectActive = document.querySelector('[data-select-active]');
  selectActive?.addEventListener('change', () => {
    document.querySelectorAll('[data-display-option][data-active="true"]').forEach((box) => { box.checked = selectActive.checked; });
  });
  const forever = document.querySelector('[data-forever]');
  const endDate = document.querySelector('[data-end-date]');
  const syncForever = () => { if (endDate) { endDate.disabled = Boolean(forever?.checked); if (forever?.checked) endDate.value = ''; } };
  forever?.addEventListener('change', syncForever); syncForever();
  document.querySelectorAll('[data-permanent]').forEach((box) => box.addEventListener('change', () => {
    const duration = box.form?.querySelector('[data-duration]'); if (duration) duration.disabled = box.checked;
  }));

  const input = document.querySelector('[data-file-input]');
  const zone = document.querySelector('[data-dropzone]');
  const pending = document.querySelector('[data-pending]');
  const preview = document.querySelector('[data-file-preview]');
  const showFile = () => {
    const file = input?.files?.[0]; if (!file) { if (pending) pending.hidden = true; return; }
    pending.hidden = false; pending.querySelector('[data-file-name]').textContent = file.name;
    pending.querySelector('[data-file-type]').textContent = file.type || 'Type detected on upload';
    if (file.type.startsWith('image/')) preview.src = URL.createObjectURL(file);
  };
  input?.addEventListener('change', showFile);
  zone?.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); } });
  ['dragenter', 'dragover'].forEach((name) => zone?.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add('is-dragging'); }));
  ['dragleave', 'drop'].forEach((name) => zone?.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove('is-dragging'); }));
  zone?.addEventListener('drop', (event) => { if (event.dataTransfer?.files?.length) { input.files = event.dataTransfer.files; showFile(); } });
  document.querySelector('[data-file-clear]')?.addEventListener('click', () => { input.value = ''; showFile(); });

  const list = document.querySelector('[data-item-list]');
  const syncOrder = () => { const out = document.querySelector('[data-item-order]'); if (out && list) out.value = [...list.querySelectorAll('[data-item-id]')].map((row) => row.dataset.itemId).join(','); };
  list?.addEventListener('click', (event) => { const button = event.target.closest('[data-move-up],[data-move-down]'); if (!button) return; const row = button.closest('[data-item-id]'); if (button.matches('[data-move-up]') && row.previousElementSibling) list.insertBefore(row, row.previousElementSibling); if (button.matches('[data-move-down]') && row.nextElementSibling) list.insertBefore(row.nextElementSibling, row); syncOrder(); });
})();
