(() => {
  const form = document.querySelector('[data-lifecycle-bulk-form]');
  if (!form) return;

  const boxes = [...form.querySelectorAll('[data-row-selection]')];
  const selectAll = form.querySelector('[data-select-all]');
  const selectedCount = form.querySelector('[data-selected-count]');
  const clearButton = form.querySelector('[data-clear-selection]');
  const applyButton = form.querySelector('[data-apply-action]');
  const action = form.querySelector('[data-lifecycle-action]');
  const actionHint = form.querySelector('[data-action-hint]');
  const dialog = document.querySelector('[data-confirm-dialog]');
  const confirmTitle = dialog?.querySelector('[data-confirm-title]');
  const confirmCopy = dialog?.querySelector('[data-confirm-copy]');
  const confirmAction = dialog?.querySelector('[data-confirm-action]');
  let confirmed = false;

  const labels = {
    SET_ACTIVE: ['Set Active', 'return purchasing eligibility to'],
    SET_NO_FUTURE_REORDER: ['Set No Future Reorder', 'block future purchase quantities for'],
    ARCHIVE: ['Archive selected', 'remove from Ordering Intelligence'],
    RESTORE: ['Restore selected', 'return to the recorded pre-archive state for'],
  };

  function selectedBoxes() { return boxes.filter((box) => box.checked); }
  function selectedCommand() {
    return action?.value || form.querySelector('input[name="command"]')?.value || '';
  }
  function updateSelection() {
    const selected = selectedBoxes();
    const count = selected.length;
    const command = selectedCommand();
    const targetStatus = {SET_ACTIVE: 'ACTIVE', SET_NO_FUTURE_REORDER: 'NO_FUTURE_REORDER', ARCHIVE: 'ARCHIVED'}[command];
    const allAlreadyTarget = Boolean(targetStatus && count && selected.every((box) => box.closest('[data-lifecycle-row]')?.dataset.status === targetStatus));
    selectedCount.textContent = `${count} selected`;
    clearButton.disabled = count === 0;
    applyButton.disabled = count === 0 || allAlreadyTarget;
    if (selectAll) {
      selectAll.checked = boxes.length > 0 && count === boxes.length;
      selectAll.indeterminate = count > 0 && count < boxes.length;
    }
    actionHint.textContent = allAlreadyTarget
      ? 'The selected products already share that lifecycle state.'
      : count
        ? `${count} visible product${count === 1 ? '' : 's'} will be changed in one atomic batch.`
        : 'Select one or more visible products to continue.';
  }

  selectAll?.addEventListener('change', () => {
    boxes.forEach((box) => { box.checked = selectAll.checked; });
    updateSelection();
  });
  boxes.forEach((box) => box.addEventListener('change', updateSelection));
  clearButton?.addEventListener('click', () => {
    boxes.forEach((box) => { box.checked = false; });
    updateSelection();
  });
  action?.addEventListener('change', updateSelection);
  form.addEventListener('submit', (event) => {
    if (confirmed || !dialog) return;
    event.preventDefault();
    const count = selectedBoxes().length;
    if (!count) return;
    const [title, effect] = labels[selectedCommand()] || ['Confirm lifecycle change', 'change'];
    confirmTitle.textContent = title;
    confirmCopy.textContent = `This will ${effect} ${count} selected product${count === 1 ? '' : 's'}. The batch is audited and applies only to this visible-page selection.`;
    confirmAction.textContent = `${title} (${count})`;
    dialog.showModal();
  });
  dialog?.addEventListener('close', () => {
    if (dialog.returnValue !== 'confirm') return;
    confirmed = true;
    form.requestSubmit();
  });
  updateSelection();
})();
