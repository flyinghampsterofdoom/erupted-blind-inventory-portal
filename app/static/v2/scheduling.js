(() => {
  'use strict';

  const root = document.querySelector('[data-schedule-board]');
  if (!root) return;

  let board = JSON.parse(root.querySelector('[data-board-json]').textContent);
  let drag = null;
  let openTool = null;
  let toolReturnFocus = null;

  const $ = (selector, context = root) => context.querySelector(selector);
  const $$ = (selector, context = root) => [...context.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);
  const announce = (message) => {
    const region = $('[data-schedule-announcer]');
    region.textContent = '';
    requestAnimationFrame(() => { region.textContent = message; });
  };
  const csrf = () => document.cookie.split('; ').find((value) => value.startsWith('csrf_token='))?.split('=').slice(1).join('=') || '';
  const query = () => location.search || `?start=${board.week.start}`;

  async function api(path, method = 'POST', payload) {
    const response = await fetch(path + query(), {
      method,
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf())},
      body: payload ? JSON.stringify(payload) : undefined,
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* handled below */ }
    if (!response.ok) {
      throw Object.assign(new Error(data.message || 'The schedule could not be updated.'), {
        status: response.status,
        data,
      });
    }
    return data;
  }

  const errorBox = $('[data-board-error]');
  function showError(error, errorRegion = null) {
    if (errorRegion) {
      const fields = error.data?.field_errors || {};
      const details = Object.values(fields).map(escapeHtml).join(' ');
      errorRegion.innerHTML = `<strong>${escapeHtml(error.message)}</strong>${details ? `<span>${details}</span>` : ''}`;
      errorRegion.hidden = false;
      errorRegion.focus();
    } else {
      errorBox.hidden = false;
      $('[data-board-error-message]').textContent = error.message;
      $('[data-refresh-board]').hidden = error.status !== 409;
    }
    announce(error.message);
  }
  function clearError(errorRegion = null) {
    if (errorRegion) {
      errorRegion.hidden = true;
      errorRegion.textContent = '';
    } else {
      errorBox.hidden = true;
    }
  }

  function shiftPayload(shift, changes = {}) {
    return {
      expected_version: Number(board.period.version),
      employee_id: shift.employee_id,
      store_id: shift.store_id,
      shift_date: shift.shift_date,
      start_time: shift.start_time,
      end_time: shift.end_time,
      unpaid_break_minutes: shift.unpaid_break_minutes || 0,
      shift_type_id: null,
      is_opener: false,
      is_closer: false,
      employee_note: shift.employee_note || '',
      override_hard_unavailability: false,
      override_reason: '',
      ...changes,
    };
  }

  function cellFor(shift) {
    const who = shift.employee_id ?? 'open';
    return document.getElementById(`schedule-cell-${who}${shift.employee_id ? '' : `-${shift.store_id}`}-${shift.shift_date}`);
  }

  function cardMarkup(shift) {
    const warning = shift.has_warning ? '<span class="schedule-warning-symbol" aria-label="Shift has warning">▲</span>' : '';
    return `<article class="schedule-shift${shift.has_warning ? ' has-warning' : ''}${shift.is_open ? ' is-open' : ''}" id="shift-card-${shift.id}" tabindex="0" data-shift-card data-shift-id="${shift.id}" aria-label="${shift.is_open ? 'Open shift' : 'Shift'} ${escapeHtml(shift.time_label)} at ${escapeHtml(shift.store_name)}${shift.has_warning ? ', has warning' : ''}">
      <div class="schedule-shift__top"><strong>${escapeHtml(shift.time_label)}</strong>${warning}</div>
      <span>${escapeHtml(shift.store_name)}</span><small>${Number(shift.paid_hours).toFixed(2)}h paid</small>
      ${board.editable ? `<div class="schedule-shift__actions" aria-label="Shift actions"><button type="button" data-shift-edit>Edit</button><button type="button" data-shift-move>Move</button><button type="button" data-shift-duplicate>Duplicate</button>${board.actions.delete_shifts ? '<button type="button" data-shift-delete>Delete</button>' : ''}</div>` : ''}
    </article>`;
  }

  function render(snapshot, focusShiftId = null) {
    board = snapshot;
    root.dataset.periodId = board.period?.id || '';
    root.dataset.periodVersion = board.period?.version || '';
    ['assigned_hours', 'open_hours', 'unique_employee_count', 'open_shift_count', 'coverage_warning_count', 'conflict_count', 'serious_warning_count'].forEach((key) => {
      const element = $(`[data-summary="${key}"]`);
      if (element) element.textContent = typeof board.summary[key] === 'number' && key.includes('hours') ? board.summary[key].toFixed(2) : board.summary[key];
    });
    const laborCost = $('[data-labor-cost]');
    const missingRates = $('[data-missing-rates]');
    if (laborCost && missingRates && board.labor) {
      laborCost.textContent = `$${Number(board.labor.estimated_cost).toFixed(2)}`;
      missingRates.textContent = board.labor.missing_rate_shift_count ? `${board.labor.missing_rate_shift_count} shift(s) missing rates` : 'All assigned shifts costed';
    }
    board.employees.forEach((employee) => {
      const element = $(`[data-employee-hours="${employee.id}"]`);
      if (element) element.textContent = Number(employee.scheduled_hours).toFixed(2);
    });
    $$('[data-shift-card]').forEach((element) => element.remove());
    board.shifts.forEach((shift) => cellFor(shift)?.querySelector('.schedule-cell__shifts')?.insertAdjacentHTML('beforeend', cardMarkup(shift)));
    renderWarnings();
    renderStoreShifts();
    bindCards();
    if (focusShiftId) {
      const card = document.getElementById(`shift-card-${focusShiftId}`);
      card?.focus({preventScroll: true});
    }
  }

  function renderWarnings() {
    const list = $('[data-warning-list]');
    if (!list) return;
    const total = $('[data-warning-total]');
    if (total) total.textContent = board.warnings.length;
    list.innerHTML = board.warnings.length ? board.warnings.map((warning) => `<li data-warning-severity="${warning.severity}" data-warning-store="${warning.store_id}"><button type="button" data-warning-target="${escapeHtml(warning.target_id)}"><span class="schedule-warning-badge schedule-warning-badge--${warning.severity.toLowerCase()}">${warning.severity === 'INFO' ? '●' : '▲'} ${warning.severity}</span><strong>${escapeHtml(warning.store_name)} · ${escapeHtml(warning.date_label)}</strong><span>${escapeHtml(warning.message)}</span>${warning.start_time ? `<small>${escapeHtml(warning.start_time)}${warning.end_time ? `–${escapeHtml(warning.end_time)}` : ''}${warning.required_count != null ? ` · ${warning.actual_count}/${warning.required_count} staffed` : ''}</small>` : ''}</button></li>`).join('') : '<li class="schedule-warning-empty">No warnings for the selected stores and week.</li>';
    bindWarnings();
  }

  function fillStateMarkup(storeShift) {
    const states = Object.values(storeShift.fill_states || {});
    const count = (state) => states.filter((value) => value === state).length;
    return `<span class="schedule-fill-state schedule-fill-state--assigned">${count('assigned')} assigned</span><span class="schedule-fill-state schedule-fill-state--open">${count('open')} open</span><span class="schedule-fill-state schedule-fill-state--not-placed">${count('not_placed')} not placed</span>`;
  }

  function storeShiftMarkup(storeShift) {
    const disabled = !storeShift.active;
    const note = board.actions.manage_store_shifts && storeShift.manager_note ? `<p class="schedule-store-shift-note"><strong>Private:</strong> ${escapeHtml(storeShift.manager_note)}</p>` : '';
    return `<article class="schedule-store-shift${disabled ? ' is-inactive' : ''}" tabindex="0" data-store-shift-card data-store-shift-id="${storeShift.id}" data-store-id="${storeShift.store_id}" aria-label="${escapeHtml(storeShift.label)}, ${escapeHtml(storeShift.time_label)}, ${escapeHtml(storeShift.store_name)}${disabled ? ', inactive' : ''}">
      <div class="schedule-store-shift__heading"><strong>${escapeHtml(storeShift.label)}</strong>${disabled ? '<span>Inactive</span>' : ''}</div>
      <span>${escapeHtml(storeShift.store_name)} · ${escapeHtml(storeShift.time_label)}</span>
      <small>${escapeHtml(storeShift.active_day_summary)}</small>
      <div class="schedule-fill-states" aria-label="Weekly fill state">${fillStateMarkup(storeShift)}</div>${note}
      <div class="schedule-store-shift__actions">${board.actions.place_store_shifts && storeShift.active ? '<button type="button" data-store-shift-place>Place</button>' : ''}${board.actions.manage_store_shifts ? '<button type="button" data-store-shift-edit>Edit</button><button type="button" data-store-shift-copy>Copy</button><button type="button" data-store-shift-up aria-label="Move definition earlier">↑</button><button type="button" data-store-shift-down aria-label="Move definition later">↓</button>' : ''}</div>
    </article>`;
  }

  function renderStoreShifts() {
    const list = $('[data-store-shift-list]');
    if (!list) return;
    const filter = $('[data-store-shift-filter]')?.value || '';
    const rows = (board.store_shifts || []).filter((row) => !filter || String(row.store_id) === filter);
    const grouped = new Map();
    rows.forEach((row) => {
      if (!grouped.has(row.store_name)) grouped.set(row.store_name, []);
      grouped.get(row.store_name).push(row);
    });
    list.innerHTML = rows.length ? [...grouped.entries()].map(([storeName, storeShifts]) => `<section class="schedule-store-shift-group"><h3>${escapeHtml(storeName)}</h3>${storeShifts.map(storeShiftMarkup).join('')}</section>`).join('') : '<p class="v2-muted">No Store Shifts match this store.</p>';
    const total = $('[data-store-shift-total]');
    if (total) total.textContent = (board.store_shifts || []).filter((row) => row.active).length;
    $$('[data-store-shift-card]').forEach((card) => { card.onpointerdown = startStoreShiftDrag; });
  }

  const shiftByCard = (card) => board.shifts.find((shift) => shift.id === Number(card.dataset.shiftId));
  const storeShiftByCard = (card) => board.store_shifts.find((shift) => shift.id === Number(card.dataset.storeShiftId));
  const shiftDialog = $('[data-shift-dialog]');
  const shiftForm = $('[data-shift-form]');
  const moveDialog = $('[data-move-dialog]');
  const moveForm = $('[data-move-form]');
  const storeShiftDialog = $('[data-store-shift-dialog]');
  const storeShiftForm = $('[data-store-shift-form]');
  const placeDialog = $('[data-store-shift-place-dialog]');
  const placeForm = $('[data-store-shift-place-form]');
  const copyDialog = $('[data-store-shift-copy-dialog]');
  const copyForm = $('[data-store-shift-copy-form]');

  function openEditor(shift = {}, cell = null) {
    shiftForm.reset();
    shiftForm.elements.shift_id.value = shift.id || '';
    shiftForm.elements.employee_id.value = shift.employee_id ?? cell?.dataset.employeeId ?? '';
    shiftForm.elements.store_id.value = shift.store_id ?? cell?.dataset.storeId ?? board.stores[0]?.id ?? '';
    shiftForm.elements.shift_date.value = shift.shift_date ?? cell?.dataset.shiftDate ?? board.week.start;
    shiftForm.elements.start_time.value = shift.start_time || '09:00';
    shiftForm.elements.end_time.value = shift.end_time || '17:00';
    shiftForm.elements.unpaid_break_minutes.value = shift.unpaid_break_minutes || 0;
    shiftForm.elements.employee_note.value = shift.employee_note || '';
    $('[data-shift-dialog-title]').textContent = shift.id ? 'Edit shift' : 'Add shift';
    $('[data-override-fields]').hidden = !board.actions.override_hard_unavailability;
    shiftDialog.showModal();
  }

  function openMove(shift) {
    moveForm.elements.shift_id.value = shift.id;
    moveForm.elements.employee_id.value = shift.employee_id ?? '';
    moveForm.elements.shift_date.value = shift.shift_date;
    moveForm.elements.store_id.value = shift.store_id;
    $('[data-move-summary]').textContent = `${shift.time_label} at ${shift.store_name}`;
    moveDialog.showModal();
  }

  function openStoreShiftEditor(storeShift = {}) {
    storeShiftForm.reset();
    storeShiftForm.elements.store_shift_id.value = storeShift.id || '';
    storeShiftForm.elements.label.value = storeShift.label || '';
    storeShiftForm.elements.store_id.value = storeShift.store_id || board.stores[0]?.id || '';
    storeShiftForm.elements.start_time.value = storeShift.start_time || '09:00';
    storeShiftForm.elements.end_time.value = storeShift.end_time || '17:00';
    storeShiftForm.elements.display_order.value = storeShift.display_order || 0;
    storeShiftForm.elements.active.checked = storeShift.id ? Boolean(storeShift.active) : true;
    storeShiftForm.elements.manager_note.value = storeShift.manager_note || '';
    const activeDays = storeShift.active_weekdays || [0, 1, 2, 3, 4, 5, 6];
    $$('input[name="active_weekdays"]', storeShiftForm).forEach((input) => { input.checked = activeDays.includes(Number(input.value)); });
    $('[data-store-shift-dialog-title]').textContent = storeShift.id ? 'Edit Store Shift' : 'New Store Shift';
    clearError($('[data-store-shift-errors]'));
    storeShiftDialog.showModal();
  }

  function openPlacement(storeShift, employeeId = null, shiftDate = null) {
    placeForm.reset();
    placeForm.elements.store_shift_id.value = storeShift.id;
    placeForm.elements.employee_id.value = employeeId ?? '';
    placeForm.elements.shift_date.value = shiftDate || firstActiveDate(storeShift) || board.week.start;
    $('[data-store-shift-place-summary]').textContent = `${storeShift.label} · ${storeShift.time_label} · ${storeShift.store_name}. Break defaults to 0 minutes.`;
    placeDialog.showModal();
  }

  function firstActiveDate(storeShift) {
    return board.week.days.find((day, index) => storeShift.active_weekdays.includes(index))?.iso || null;
  }

  function employeeStoreConflict(employeeId, shiftDate, storeId, excludingShiftId = null) {
    if (employeeId == null) return null;
    return board.shifts.find((shift) => shift.id !== excludingShiftId && shift.employee_id === employeeId && shift.shift_date === shiftDate && shift.store_id !== storeId) || null;
  }

  function activeForDate(storeShift, isoDate) {
    const index = board.week.days.findIndex((day) => day.iso === isoDate);
    return index >= 0 && storeShift.active_weekdays.includes(index);
  }

  async function saveMove(shift, changes) {
    clearError();
    const conflict = employeeStoreConflict(changes.employee_id, changes.shift_date, changes.store_id, shift.id);
    if (conflict) {
      showError(new Error(`This employee is already scheduled at ${conflict.store_name} on this day. Employees may work at only one store per day.`));
      return;
    }
    try {
      const targetCell = $$('[data-drop-cell]').find((cell) => (cell.dataset.employeeId || null) === (changes.employee_id == null ? null : String(changes.employee_id)) && cell.dataset.shiftDate === changes.shift_date && (changes.employee_id != null || Number(cell.dataset.storeId) === Number(changes.store_id)));
      const hard = targetCell?.querySelector('.schedule-indicator--hard_unavailable,.schedule-indicator--time_off');
      if (hard) {
        if (!board.actions.override_hard_unavailability) throw new Error('This destination requires override permission.');
        if (!confirm('This destination conflicts with hard unavailability or approved time off. Continue with an explicit override?')) return;
        changes.override_hard_unavailability = true;
        changes.override_reason = prompt('Enter an override reason:') || '';
        if (!changes.override_reason) throw new Error('An override reason is required.');
      }
      const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}`, 'PATCH', shiftPayload(shift, changes));
      render(data.board, shift.id);
      moveDialog.close();
      announce(data.message);
    } catch (error) { showError(error); }
  }

  async function placeStoreShift(storeShift, employeeId, shiftDate) {
    clearError();
    if (!activeForDate(storeShift, shiftDate)) {
      showError(new Error(`${storeShift.label} is not active on the selected day.`));
      return;
    }
    const conflict = employeeStoreConflict(employeeId, shiftDate, storeShift.store_id);
    if (conflict) {
      showError(new Error(`This employee is already scheduled at ${conflict.store_name} on this day. Employees may work at only one store per day.`));
      return;
    }
    try {
      const data = await api(`/v2/scheduling/api/periods/${board.period.id}/store-shifts/${storeShift.id}/place`, 'POST', {
        expected_version: Number(board.period.version),
        shift_date: shiftDate,
        employee_id: employeeId,
        destination_store_id: storeShift.store_id,
      });
      render(data.board, data.shift?.id);
      placeDialog.close();
      announce(data.message);
    } catch (error) { showError(error); }
  }

  shiftForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = new FormData(shiftForm);
    const id = Number(values.get('shift_id')) || null;
    const payload = {
      expected_version: Number(board.period.version),
      employee_id: values.get('employee_id') ? Number(values.get('employee_id')) : null,
      store_id: Number(values.get('store_id')),
      shift_date: values.get('shift_date'),
      start_time: values.get('start_time'),
      end_time: values.get('end_time'),
      unpaid_break_minutes: Number(values.get('unpaid_break_minutes') || 0),
      shift_type_id: null,
      is_opener: false,
      is_closer: false,
      employee_note: values.get('employee_note') || '',
      override_hard_unavailability: values.has('override_hard_unavailability'),
      override_reason: values.get('override_reason') || '',
    };
    try {
      const data = await api(id ? `/v2/scheduling/api/periods/${board.period.id}/shifts/${id}` : `/v2/scheduling/api/periods/${board.period.id}/shifts`, id ? 'PATCH' : 'POST', payload);
      render(data.board, data.shift?.id);
      shiftDialog.close();
      announce(data.message);
    } catch (error) { showError(error, $('[data-shift-errors]')); }
  });

  moveForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const shift = board.shifts.find((row) => row.id === Number(moveForm.elements.shift_id.value));
    saveMove(shift, {
      employee_id: moveForm.elements.employee_id.value ? Number(moveForm.elements.employee_id.value) : null,
      shift_date: moveForm.elements.shift_date.value,
      store_id: Number(moveForm.elements.store_id.value),
    });
  });

  storeShiftForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = new FormData(storeShiftForm);
    const id = Number(values.get('store_shift_id')) || null;
    const payload = {
      label: values.get('label'), store_id: Number(values.get('store_id')),
      start_time: values.get('start_time'), end_time: values.get('end_time'),
      active_weekdays: values.getAll('active_weekdays').map(Number),
      active: values.has('active'), display_order: Number(values.get('display_order') || 0),
      manager_note: values.get('manager_note') || '',
    };
    try {
      const data = await api(id ? `/v2/scheduling/api/store-shifts/${id}` : '/v2/scheduling/api/store-shifts', id ? 'PATCH' : 'POST', payload);
      board.store_shifts = data.store_shifts;
      renderStoreShifts();
      storeShiftDialog.close();
      announce(data.message);
    } catch (error) { showError(error, $('[data-store-shift-errors]')); }
  });

  placeForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const storeShift = board.store_shifts.find((row) => row.id === Number(placeForm.elements.store_shift_id.value));
    placeStoreShift(storeShift, placeForm.elements.employee_id.value ? Number(placeForm.elements.employee_id.value) : null, placeForm.elements.shift_date.value);
  });

  copyForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = Number(copyForm.elements.store_shift_id.value);
    try {
      const data = await api(`/v2/scheduling/api/store-shifts/${id}/copy`, 'POST', {
        destination_store_id: Number(copyForm.elements.destination_store_id.value),
        label: copyForm.elements.label.value,
      });
      board.store_shifts = data.store_shifts;
      renderStoreShifts();
      copyDialog.close();
      announce(data.message);
    } catch (error) { showError(error); }
  });

  function bindCards() {
    $$('[data-shift-card]').forEach((card) => { card.onpointerdown = startShiftDrag; });
  }

  function beginDrag(event, kind, item, card) {
    if (event.button !== 0 || event.target.closest('button') || matchMedia('(max-width:620px)').matches) return;
    drag = {kind, item, card, x: event.clientX, y: event.clientY, started: false, target: null};
    card.setPointerCapture(event.pointerId);
    card.onpointermove = moveDrag;
    card.onpointerup = endDrag;
    card.onpointercancel = cancelDrag;
  }
  function startShiftDrag(event) { beginDrag(event, 'scheduled', shiftByCard(event.currentTarget), event.currentTarget); }
  function startStoreShiftDrag(event) {
    const item = storeShiftByCard(event.currentTarget);
    if (!item.active || !board.actions.place_store_shifts) return;
    beginDrag(event, 'store-shift', item, event.currentTarget);
  }
  function invalidDrop(target) {
    if (!target || target.closest('.is-inactive')) return true;
    const employeeId = target.dataset.employeeId ? Number(target.dataset.employeeId) : null;
    const shiftDate = target.dataset.shiftDate;
    if (drag.kind === 'store-shift') {
      if (!activeForDate(drag.item, shiftDate)) return true;
      if (employeeId == null && Number(target.dataset.storeId) !== drag.item.store_id) return true;
      return Boolean(employeeStoreConflict(employeeId, shiftDate, drag.item.store_id));
    }
    const storeId = drag.item.store_id;
    if (employeeId == null && Number(target.dataset.storeId) !== storeId) return true;
    return Boolean(employeeStoreConflict(employeeId, shiftDate, storeId, drag.item.id));
  }
  function moveDrag(event) {
    if (!drag) return;
    if (!drag.started && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 8) return;
    drag.started = true;
    drag.card.classList.add('is-grabbed');
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-drop-cell]');
    $$('.is-valid-drop,.is-invalid-drop').forEach((element) => element.classList.remove('is-valid-drop', 'is-invalid-drop'));
    if (!target) return;
    const invalid = invalidDrop(target);
    target.classList.add(invalid ? 'is-invalid-drop' : 'is-valid-drop');
    drag.target = invalid ? null : target;
  }
  function endDrag() {
    if (!drag) return;
    const completed = drag;
    cleanupDrag();
    if (!completed.started || !completed.target) return;
    const employeeId = completed.target.dataset.employeeId ? Number(completed.target.dataset.employeeId) : null;
    if (completed.kind === 'store-shift') placeStoreShift(completed.item, employeeId, completed.target.dataset.shiftDate);
    else saveMove(completed.item, {employee_id: employeeId, shift_date: completed.target.dataset.shiftDate, store_id: completed.item.store_id});
  }
  function cancelDrag() {
    if (!drag) return;
    cleanupDrag();
    announce('Drag cancelled.');
  }
  function cleanupDrag() {
    $$('.is-valid-drop,.is-invalid-drop,.is-grabbed').forEach((element) => element.classList.remove('is-valid-drop', 'is-invalid-drop', 'is-grabbed'));
    drag = null;
  }

  function closeTools({restoreFocus = true} = {}) {
    $$('[data-tool-panel]').forEach((panel) => { panel.hidden = true; });
    $$('[data-tool-toggle]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
    openTool = null;
    if (restoreFocus && toolReturnFocus) toolReturnFocus.focus();
    toolReturnFocus = null;
  }
  function openTools(name, trigger = null) {
    if (openTool === name) { closeTools(); return; }
    closeTools({restoreFocus: false});
    openTool = name;
    toolReturnFocus = trigger || document.activeElement;
    const panel = $(`[data-tool-panel="${name}"]`);
    const toggle = $(`[data-tool-toggle="${name}"]`);
    if (!panel) return;
    panel.hidden = false;
    toggle?.setAttribute('aria-expanded', 'true');
    panel.focus();
  }

  function bindWarnings() {
    $$('[data-warning-target]').forEach((button) => {
      button.onclick = () => {
        const target = document.getElementById(button.dataset.warningTarget);
        if (!target) return;
        target.scrollIntoView({block: 'center', inline: 'center', behavior: matchMedia('(prefers-reduced-motion:reduce)').matches ? 'auto' : 'smooth'});
        target.classList.add('is-highlighted');
        target.focus({preventScroll: true});
        setTimeout(() => target.classList.remove('is-highlighted'), 1800);
      };
    });
  }

  async function reorderStoreShift(storeShift, direction) {
    const peers = board.store_shifts.filter((row) => row.store_id === storeShift.store_id);
    const index = peers.findIndex((row) => row.id === storeShift.id);
    const otherIndex = index + direction;
    if (index < 0 || otherIndex < 0 || otherIndex >= peers.length) return;
    [peers[index], peers[otherIndex]] = [peers[otherIndex], peers[index]];
    try {
      const data = await api('/v2/scheduling/api/store-shifts/reorder', 'POST', {ordered_ids: peers.map((row) => row.id)});
      board.store_shifts = data.store_shifts;
      renderStoreShifts();
      announce(data.message);
    } catch (error) { showError(error); }
  }

  root.addEventListener('click', async (event) => {
    const close = event.target.closest('[data-dialog-close]');
    if (close) { close.closest('dialog').close(); return; }
    const toggle = event.target.closest('[data-tool-toggle]');
    if (toggle) { openTools(toggle.dataset.toolToggle, toggle); return; }
    if (event.target.closest('[data-tool-close]')) { closeTools(); return; }
    const warningsTrigger = event.target.closest('[data-open-warnings]');
    if (warningsTrigger) {
      openTools('warnings', warningsTrigger);
      const requested = warningsTrigger.dataset.warningFilter;
      const filter = requested === 'conflict' ? 'CONFLICT' : requested === 'serious' ? 'SERIOUS' : 'all';
      $(`[data-warning-filter-button="${filter}"]`)?.click();
      return;
    }
    const cellAdd = event.target.closest('[data-cell-add]');
    if (cellAdd) { openEditor({}, cellAdd.closest('[data-drop-cell]')); return; }
    if (event.target.closest('[data-add-shift]')) { openEditor(); return; }
    if (event.target.closest('[data-store-shift-add]')) { openStoreShiftEditor(); return; }

    const storeShiftCard = event.target.closest('[data-store-shift-card]');
    if (storeShiftCard) {
      const storeShift = storeShiftByCard(storeShiftCard);
      if (event.target.closest('[data-store-shift-place]')) openPlacement(storeShift);
      else if (event.target.closest('[data-store-shift-edit]')) openStoreShiftEditor(storeShift);
      else if (event.target.closest('[data-store-shift-copy]')) {
        copyForm.reset(); copyForm.elements.store_shift_id.value = storeShift.id;
        copyForm.elements.label.value = storeShift.label; copyForm.elements.destination_store_id.value = storeShift.store_id;
        copyDialog.showModal();
      } else if (event.target.closest('[data-store-shift-up]')) reorderStoreShift(storeShift, -1);
      else if (event.target.closest('[data-store-shift-down]')) reorderStoreShift(storeShift, 1);
      return;
    }

    const card = event.target.closest('[data-shift-card]');
    if (card) {
      const shift = shiftByCard(card);
      if (event.target.closest('[data-shift-edit]')) openEditor(shift);
      else if (event.target.closest('[data-shift-move]')) openMove(shift);
      else if (event.target.closest('[data-shift-duplicate]')) {
        try {
          const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}/duplicate`, 'POST', {expected_version: Number(board.period.version)});
          render(data.board, data.shift?.id); announce(data.message);
        } catch (error) { showError(error); }
      } else if (event.target.closest('[data-shift-delete]') && confirm('Delete this shift?')) {
        try {
          const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}`, 'DELETE', {expected_version: Number(board.period.version)});
          render(data.board); announce(data.message);
        } catch (error) { showError(error); }
      }
      return;
    }

    if (event.target.closest('[data-create-draft]')) {
      try { await api('/v2/scheduling/api/periods', 'POST', {week_start_date: board.week.start, notes: ''}); location.reload(); } catch (error) { showError(error); }
    }
    if (event.target.closest('[data-clone-published]')) {
      try { await api(`/v2/scheduling/api/periods/${board.period.id}/clone-published`, 'POST', {expected_version: Number(board.period.version)}); location.reload(); } catch (error) { showError(error); }
    }
    if (event.target.closest('[data-refresh-board]')) location.reload();
  });

  $$('[data-warning-filter-button]').forEach((button) => {
    button.onclick = () => {
      $$('[data-warning-filter-button]').forEach((item) => item.classList.toggle('is-active', item === button));
      $$('[data-warning-severity]').forEach((item) => { item.hidden = button.dataset.warningFilterButton !== 'all' && item.dataset.warningSeverity !== button.dataset.warningFilterButton; });
    };
  });
  $('[data-store-shift-filter]')?.addEventListener('change', renderStoreShifts);
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (drag) cancelDrag();
    else if (openTool) closeTools();
  });
  document.addEventListener('pointerdown', (event) => {
    if (openTool && !event.target.closest('[data-schedule-tools]') && !event.target.closest('[data-open-warnings]')) closeTools({restoreFocus: false});
  });

  renderStoreShifts();
  bindCards();
  bindWarnings();
})();
