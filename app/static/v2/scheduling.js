(() => {
  'use strict';

  const root = document.querySelector('[data-schedule-board]');
  if (!root) return;

  let board = JSON.parse(root.querySelector('[data-board-json]').textContent);
  let drag = null;
  let openTool = null;
  let toolReturnFocus = null;
  let pendingMove = null;
  let writeInFlight = false;
  let refreshRequired = false;
  window.addEventListener('beforeunload', (event) => {
    if ((pendingMove && !pendingMove.acknowledged) || writeInFlight) { event.preventDefault(); event.returnValue = ''; }
  });


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

  let manualAcknowledged = root.dataset.manualAcknowledged === 'true';
  let manualAcknowledgement = null;
  function isManualAssignment(path) {
    return /\/periods\/\d+\/(shifts(?:\/|$)|store-shifts\/\d+\/place)/.test(path) || path.endsWith('/assignment-change');
  }
  async function ensureManualEditing() {
    if (manualAcknowledged || !board.period?.generated_origin || board.period.is_historical) return;
    if (manualAcknowledgement) return manualAcknowledgement;
    manualAcknowledgement = (async () => {
      // Another tab may already have acknowledged this same login session.
      const status = await fetch('/v2/scheduling/api/manual-edit-session', {credentials:'same-origin',cache:'no-store'});
      if (!status.ok) throw new Error('Could not check the editing session. Refresh and try again.');
      if ((await status.json()).acknowledged) { manualAcknowledged = true; return; }
      const dialog = $('[data-manual-edit-dialog]'), form = $('[data-manual-edit-form]');
      const error = $('[data-manual-edit-error]');
      await new Promise((resolve,reject) => {
        form.onsubmit = async event => {
          event.preventDefault();
          const submit = form.querySelector('[type="submit"]'); submit.disabled = true; error.hidden = true;
          try {
            const response = await fetch('/v2/scheduling/api/manual-edit-session', {
              method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':decodeURIComponent(csrf())},
            });
            if (!response.ok || !(await response.json()).acknowledged) throw new Error('Could not confirm the editing session. Try again.');
            manualAcknowledged = true; dialog.close(); resolve();
          } catch (failure) { error.textContent = failure.message; error.hidden = false; }
          finally { submit.disabled = false; }
        };
        dialog.onclose = () => { if (!manualAcknowledged) reject(new Error('Manual edit cancelled. Nothing was saved.')); };
        $('[data-manual-edit-cancel]').onclick = () => dialog.close();
        dialog.showModal();
      });
    })().finally(() => { manualAcknowledgement = null; });
    return manualAcknowledgement;
  }

  function confirmOperationalException(codes, message) {
    if (!board.actions.override_operational || !confirm(`${message}\n\nAuthorize an Owner operational exception for this change?`)) return null;
    const pto = codes.includes('APPROVED_TIME_OFF');
    if (pto && !confirm('Separately confirm: authorize this assignment during approved PTO?')) return null;
    const reason = (prompt('Enter the Owner reason for this specific exception:') || '').trim();
    if (!reason || reason.length > 2000) { showError(new Error('Enter an exception reason of 1–2,000 characters.')); return null; }
    return {operational_override:true,override_pto:pto,override_reason:reason};
  }

  async function api(path, method = 'POST', payload, move = null) {
    const writing = method !== 'GET';
    if (writing && (refreshRequired || writeInFlight || (pendingMove && pendingMove !== move))) {
      throw new Error(refreshRequired ? 'Refresh the board to confirm the last save before editing again.' : 'A schedule change is still saving. Please wait before making another change.');
    }
    if (writing && board.period?.generated_origin && isManualAssignment(path)) await ensureManualEditing();
    // Recheck after an asynchronous first-session acknowledgement.
    if (writing && (writeInFlight || refreshRequired || (pendingMove && pendingMove !== move))) throw new Error('A schedule change is still saving.');
    if (writing) writeInFlight = true;
    const controller = writing ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), 15000) : null;
    try {
      const scope = query();
      const requestPath = path + (scope ? (path.includes('?') ? '&' + scope.replace(/^\?/, '') : scope) : '');
      const response = await fetch(requestPath, {
        method,
        credentials: 'same-origin',
        signal: controller?.signal,
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf()), ...(move ? {'X-Scheduling-Response':'delta'} : {})},
        body: payload ? JSON.stringify(payload) : undefined,
      });
      let data = {};
      try { data = await response.json(); } catch (_) {
        throw Object.assign(new Error('The save could not be confirmed. Refresh to check whether it was saved before trying again.'), {requiresRefresh: writing});
      }
      if (!response.ok) {
        throw Object.assign(new Error(data.message || 'The schedule could not be updated.'), {
          status: response.status,
          data,
          requiresRefresh: writing && (response.status === 409 || response.status >= 500),
        });
      }
      if (move && (data.save_outcome !== 'local_saved' || (!data.board && !data.delta))) {
        throw Object.assign(new Error('The save could not be confirmed. Refresh before editing again.'), {requiresRefresh:true});
      }
      return data;
    } catch (error) {
      if (writing && (!error.status || error.requiresRefresh)) {
        refreshRequired = true;
        error.requiresRefresh = true;
        if (!error.status || error.status >= 500) error.message = 'The save could not be confirmed. Refresh to check whether it was saved before trying again.';
      }
      throw error;
    } finally {
      if (timeout !== null) clearTimeout(timeout);
      if (writing) writeInFlight = false;
    }
  }

  const publishDialog = $('[data-publish-dialog]');
  const publishForm = $('[data-publish-form]');
  let publishContext = null;
  let publishing = false;
  function openPublication() {
    if (publishing || !board.actions.publish) return;
    publishContext = {id:board.period.id, version:Number(board.period.version), approved:!!board.period.approval};
    const label = window.SchedulePresentation.weekLabel(board);
    $('#schedule-publish-title').textContent = publishContext.approved ? 'Publish this schedule?' : 'Approve and publish this schedule?';
    $('[data-publish-description]').textContent = publishContext.approved
      ? `This will publish the schedule for ${label} for employees to view. Its existing approval will be retained.`
      : `This will approve the schedule for ${label}, and publish it for employees to view.`;
    $('[data-publish-submit]').textContent = publishContext.approved ? 'Publish Schedule' : 'Approve & Publish';
    clearError($('[data-publish-error]')); $('[data-publish-refresh]').hidden=true;
    if ($('[data-publish-override]')) { $('[data-publish-override]').checked=false; $('[data-publish-override-reason]').value=''; if ($('[data-publish-pto]')) $('[data-publish-pto]').checked=false; }
    publishDialog.showModal();
  }
  publishDialog.addEventListener('cancel', event => { if (publishing) event.preventDefault(); });
  $('[data-publish-cancel]').addEventListener('click', () => { if (!publishing) publishDialog.close(); });
  $('[data-publish-refresh]').addEventListener('click', () => location.reload());
  publishForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (publishing || !publishContext) return;
    publishing=true;
    const submit=$('[data-publish-submit]'), cancel=$('[data-publish-cancel]');
    submit.disabled=cancel.disabled=true; submit.textContent='Publishing…';
    publishForm.setAttribute('aria-busy','true'); clearError($('[data-publish-error]'));
    try {
      const override = !!$('[data-publish-override]')?.checked;
      const reason = $('[data-publish-override-reason]')?.value.trim() || '';
      if (override && !reason) throw new Error('Enter a reason for the Owner publication override.');
      await api(`/v2/scheduling/api/periods/${publishContext.id}/approve-and-publish`, 'POST', {expected_version:publishContext.version, confirm_serious_warnings:override, override_reason:reason, override_pto:!!$('[data-publish-pto]')?.checked});
      location.reload();
    } catch (error) {
      showError(error,$('[data-publish-error]'));
      $('[data-publish-refresh]').hidden=error.status!==409;
      publishing=false; submit.disabled=cancel.disabled=false;
      submit.textContent=publishContext.approved ? 'Publish Schedule' : 'Approve & Publish';
      publishForm.setAttribute('aria-busy','false');
    }
  });

  const historyDialog = $('[data-history-dialog]');
  const historyForm = $('[data-history-form]');
  function openHistoryAction() {
    clearError($('[data-history-errors]'));
    historyDialog.showModal();
  }
  historyForm.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const result = await api('/v2/scheduling/api/history/edit', 'POST', {
        week_start_date: board.week.start, expected_version: board.period?.version || null,
        reason: 'Admin entered previous-week editing; source and correction note recorded on final save',
      });
      const target = new URL(location.href);
      target.searchParams.set('period_id', result.period_id);
      target.searchParams.set('start', result.week_start);
      target.searchParams.set('history', 'edit');
      location.assign(target);
    } catch (error) { showError(error, $('[data-history-errors]')); }
  });
  $('[data-history-save-form]')?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api(`/v2/scheduling/api/history/${board.period.id}/lock`, 'POST', {
        expected_version: Number(board.period.version), reason: event.target.elements.reason.value.trim(),
      });
      const target = new URL(location.href);
      target.searchParams.delete('history');
      target.searchParams.delete('period_id');
      location.assign(target);
    } catch (error) { showError(error); }
  });
  $('[data-include-former]')?.addEventListener('change', event => {
    const target = new URL(location.href);
    if (event.target.checked) target.searchParams.set('include_former', 'true');
    else target.searchParams.delete('include_former');
    location.assign(target);
  });
  $('[data-employee-search]')?.addEventListener('input', event => {
    const term = event.target.value.trim().toLocaleLowerCase();
    for (const select of root.querySelectorAll('select[name="employee_id"], select[name="replacement_employee_id"]')) {
      for (const option of select.options) option.hidden = !!option.value && !option.selected && !option.textContent.toLocaleLowerCase().includes(term);
    }
    let visible = 0;
    const rows = $$('[data-employee-row]');
    for (const row of rows) {
      row.hidden = !!term && !row.querySelector('.schedule-employee strong').textContent.toLocaleLowerCase().includes(term);
      if (!row.hidden) visible++;
    }
    $('[data-search-feedback]').hidden = !term;
    $('[data-search-feedback]').textContent = term ? (visible ? `${visible} of ${rows.length} employee rows · weekly totals unchanged` : 'No employees match. Try another name or clear Find employee to restore the full schedule.') : '';

  });

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
      $('[data-board-error-title]').textContent = error.requiresRefresh ? 'Refresh required to confirm schedule' : 'Schedule not updated';
      $('[data-board-error-message]').textContent = error.message;
      $('[data-refresh-board]').hidden = error.status !== 409 && !error.requiresRefresh;
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
      shift_type_id: shift.shift_type_id ?? null,
      is_opener: Boolean(shift.is_opener),
      is_closer: Boolean(shift.is_closer),
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
    const locked = shift.manually_locked ? `<span title="${escapeHtml(shift.lock_reason || 'Manual assignment lock')}" aria-label="Manually locked">🔒</span>` : '';
    const role = shift.shift_type_name ? `<b class="schedule-shift__role">${escapeHtml(shift.shift_type_name)}</b>` : '';
    const attendance = (shift.attendance_statuses || []).length ? `<small class="schedule-shift__attendance">Attendance: ${escapeHtml(shift.attendance_statuses.join(', '))}</small>` : '';
    return `<article class="schedule-shift${shift.has_warning ? ' has-warning' : ''}${shift.is_open ? ' is-open' : ''}${shift.is_coverage ? ' is-coverage' : ''}" id="shift-card-${shift.id}" tabindex="0" data-shift-card data-shift-id="${shift.id}" aria-label="${shift.is_open ? 'Open shift' : 'Shift'} ${escapeHtml(shift.time_label)} at ${escapeHtml(shift.store_name)}${shift.has_warning ? ', has warning' : ''}">
      <div class="schedule-shift__top">${shift.is_coverage ? '<strong class="schedule-coverage-badge">COVERAGE</strong>' : ''}<strong>${escapeHtml(shift.time_label)}</strong><span>${shift.is_shift_lead ? '<strong class="schedule-shift__badge" title="Lead for this location and shift">LEAD</strong>' : ''}${shift.is_lead_of_day ? `<strong class="schedule-shift__badge"${shift.lead_of_day_manually_assigned ? ' title="Manager override"' : ''}>Lead${shift.lead_of_day_manually_assigned ? ' · Manager' : ''}</strong>` : ''}${shift.is_double_coverage ? '<strong class="schedule-shift__badge">Double Coverage</strong>' : ''}${role}${locked}${warning}</span></div>
      <span class="schedule-shift__store">${escapeHtml(shift.store_name)}</span><small>${escapeHtml(shift.paid_duration_label)} ${shift.is_coverage ? 'coverage' : 'originally scheduled'}</small>
      ${shift.is_coverage ? `<small>Covering: ${escapeHtml(shift.covering_employee_name)}</small><a href="#shift-card-${shift.source_shift_id}" class="schedule-coverage-source">Original shift</a>` : `<small>${shift.effective_hours == null ? 'Effective hours not established' : Number(shift.effective_hours).toFixed(2) + ' effective hrs'} · ${shift.actual_hours == null ? 'Actual hours not established' : Number(shift.actual_hours).toFixed(2) + ' actual hrs'}</small>`}
      ${board.editable && !shift.is_coverage && board.period?.is_historical && shift.employee_id ? `<button type="button" class="schedule-unlock" data-history-lead="${shift.id}" data-assigned="${!shift.is_lead_of_day}">${shift.is_lead_of_day ? 'Clear historical Lead duty' : 'Attest Lead duty'}</button>` : ''}
      ${shift.is_open ? `<small>${shift.business_closed ? 'Business closed · no coverage required' : shift.intentionally_unfilled ? 'Intentionally unfilled · coverage required · manual removal preserved' : 'Open shift · requires coverage'}</small>` : ''}
      ${attendance}
      ${shift.exception?.status ? `<small class="schedule-shift__attendance"><strong>${escapeHtml(shift.exception.status)}</strong><br>Originally published: ${escapeHtml(shift.exception.original.employee_name || 'Unknown / ambiguous')}${shift.exception.commitment ? `<br>Committed: ${escapeHtml(shift.exception.commitment.employee_name)}` : ''}<br>Actual coverage: ${escapeHtml(shift.exception.actual_worker_names.join(', ') || 'Not established')}</small>` : ''}
      ${shift.is_lead_of_day && shift.lead_duty ? `<small class="schedule-shift__attendance">Company-wide Lead: ${escapeHtml(shift.lead_duty.display_outcome || shift.lead_duty.outcome)}${shift.lead_duty.employee_name ? ` · ${escapeHtml(shift.lead_duty.employee_name)}` : ''}</small>` : ''}
      ${shift.can_resolve_lead_duty ? '<div class="schedule-shift__actions"><button type="button" data-lead-duty-open>Company-wide Lead</button></div>' : ''}
      ${board.editable && !shift.is_coverage ? `<details class="schedule-shift__menu"><summary aria-label="Actions for shift ${escapeHtml(shift.time_label)}">•••</summary><div class="schedule-shift__actions" aria-label="Shift actions">${board.actions.remove_assignments && shift.employee_id && !shift.manually_locked ? '<button type="button" data-shift-remove>Remove assignment</button>' : ''}<button type="button" data-shift-edit>Edit</button><button type="button" data-shift-move>Move</button><button type="button" data-shift-duplicate>Duplicate</button>${board.actions.delete_shifts ? '<button type="button" data-shift-delete>Delete</button>' : ''}</div></details>` : ''}
      ${board.editable && !shift.is_coverage && !board.period?.is_historical && shift.manually_locked ? '<button type="button" class="schedule-unlock" data-shift-unlock>Unlock for regeneration</button>' : ''}
      ${shift.can_record_attendance ? `<div class="schedule-shift__actions" aria-label="Attendance actions"><button type="button" data-callout-open>${shift.is_coverage ? 'Manage coverage' : 'Call-out / coverage'}</button></div>` : ''}
    </article>`;
  }

  function mergeMoveDelta(previous, delta) {
    const fail = () => { throw Object.assign(new Error('The shift was saved, but this view needs a full refresh.'), {requiresRefresh:true}); };
    if (delta.period?.id !== previous.period?.id || delta.base_version !== previous.period.version || delta.period.version !== delta.base_version + 1) fail();
    const sameIds = (left,right) => left.length === right.length && left.every(id => right.includes(id));
    if (!sameIds(previous.employees.map(e=>e.id),delta.layout_employee_ids) ||
        !sameIds(previous.shifts.map(s=>s.id),delta.shift_warning_states.map(s=>s.id))) fail();
    const updates = new Map(delta.shifts.map(s=>[s.id,s]));
    const warnings = new Map(delta.shift_warning_states.map(s=>[s.id,s]));
    const changed = new Set(updates.keys());
    const shifts = previous.shifts.map(old => {
      const warning = warnings.get(old.id);
      if (old.has_warning !== warning.has_warning) changed.add(old.id);
      return {...old,...updates.get(old.id),...warning};
    });
    const counters = new Map(delta.employees.map(e=>[e.id,e]));
    const days = delta.days;
    const employees = previous.employees.map(old => ({...old,...counters.get(old.id),
      days:old.days.map(day=>({...day,...days.find(d=>d.iso===day.iso),
        shifts:shifts.filter(s=>s.employee_id===old.id && s.shift_date===day.iso)})),
    }));
    const people = new Map(employees.map(e=>[e.id,e]));
    const groups = previous.groups.map(group=>({...group,
      employees:group.employees.map(e=>people.get(e.id)),
      warning_count:group.store_id == null ? group.employees.reduce((sum,e)=>sum+people.get(e.id).warning_count,0) : delta.warnings.filter(w=>w.store_id===group.store_id).length,
      open_days:group.open_days.map(day=>({...day,...days.find(d=>d.iso===day.iso),
        shifts:shifts.filter(s=>s.employee_id==null && s.store_id===group.store_id && s.shift_date===day.iso)})),
    }));
    const {base_version,layout_employee_ids,shift_warning_states,days:unusedDays,shifts:unusedShifts,employees:unusedEmployees,...fields}=delta;
    return {snapshot:{...previous,...fields,week:{...previous.week,days},employees,groups,shifts},
      changedIds:JSON.stringify(previous.actions)===JSON.stringify(delta.actions) ? changed : null};
  }

  function render(snapshot, focusShiftId = null, changedIds = null) {
    if (snapshot.period?.id === board.period?.id && Number(snapshot.period.version) < Number(board.period.version)) return;
    if (pendingMove && !pendingMove.acknowledged) {
      if (!pendingMove.deferredBoard || snapshot.period?.version >= pendingMove.deferredBoard.period?.version) pendingMove.deferredBoard = snapshot;
      return;
    }
    board = snapshot;
    window.SchedulePresentation?.update(root, board);
    root.dispatchEvent(new CustomEvent('schedule:updated', {detail: {board}}));
    const hoursCard = $('[data-business-hours]');
    if (hoursCard) {
      hoursCard.hidden = !(board.business_hours || []).length;
      $('[data-business-hours-list]').innerHTML = (board.business_hours || []).map(item =>
        `<p><strong>${escapeHtml(item.date)} · ${escapeHtml(item.store_name)} · ${item.closed ? 'Business Closed' : 'Different hours · ' + escapeHtml(item.hours)}</strong> · ${escapeHtml(item.event_name)}</p>`).join('');
    }
    root.dataset.periodId = board.period?.id || '';
    root.dataset.periodVersion = board.period?.version || '';
    ['assigned_hours'].forEach((key) => {
      const element = $(`[data-summary="${key}"]`);
      if (element) element.textContent = Number(board.summary[key]).toFixed(2);
    });
    board.summary.stores.forEach((store) => {
      const element = $(`[data-store-shift-summary="${store.store_id}"]`);
      if (element) element.textContent = store.assigned_shift_count;
    });
    board.employees.forEach((employee) => {
      const hours = $(`[data-employee-hours="${employee.id}"]`);
      if (hours) hours.textContent = `${Number(employee.scheduled_hours).toFixed(2)} effective hrs`;
      const originalHours = $(`[data-employee-original-hours="${employee.id}"]`);
      if (originalHours) originalHours.textContent = `${Number(employee.original_scheduled_hours || 0).toFixed(2)} originally scheduled hrs`;
      const shifts = $(`[data-employee-shifts="${employee.id}"]`);
      if (shifts) {
        shifts.textContent = employee.assigned_shift_count;
        shifts.closest('.schedule-employee__shift-count')?.classList.toggle('is-off-target', employee.assigned_shift_count !== employee.target_shift_count);
      }
    });
    $$('[data-shift-card]').forEach((element) => { if (!changedIds || changedIds.has(Number(element.dataset.shiftId))) element.remove(); });
    const missingCell = board.shifts.some((shift) => !cellFor(shift));
    if (missingCell) { location.reload(); return; }
    board.shifts.filter(shift => !changedIds || changedIds.has(shift.id)).forEach((shift) => cellFor(shift).querySelector('.schedule-cell__shifts')?.insertAdjacentHTML('beforeend', cardMarkup(shift)));
    renderWarnings();
    if (!changedIds) renderStoreShifts();
    bindCards();
    root.dispatchEvent(new CustomEvent('schedule:rendered', {detail: {board}}));
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
  const attendanceDialog = $('[data-attendance-dialog]');
  const attendanceForm = $('[data-attendance-form]');
  const leadDialog = $('[data-lead-duty-dialog]');
  const leadForm = $('[data-lead-duty-form]');
  function openLeadDuty(shift) {
    leadForm.reset(); leadForm.elements.shift_id.value = shift.id;
    clearError($('[data-lead-duty-errors]'));
    const fact = shift.lead_duty;
    const current = !shift.exception_timing.historical;
    leadForm.elements.outcome.querySelector('[value="PERFORMED"]').textContent = current ? 'Employee has assumed company-wide Lead responsibility' : 'Employee performed Lead responsibility';
    for (const option of leadForm.elements.employee_id.options) {
      option.disabled = current && option.value && !board.current_lead_employee_ids.includes(Number(option.value));
    }
    $('[data-lead-duty-summary]').textContent = `${shift.shift_date} · Scheduled Lead: ${fact?.scheduled_employee_name || 'Unknown'}. Outcome: ${fact?.outcome || 'UNRESOLVED'}${fact?.employee_name ? ' · ' + fact.employee_name : ''}.`;
    $('[data-lead-duty-history]').innerHTML = (shift.lead_duty_history || []).map(row => `<article class="schedule-attendance-event"><strong>${escapeHtml(row.outcome)} ${escapeHtml(row.employee_name || '')}</strong><span>${escapeHtml(row.reason)}</span><small>${escapeHtml(new Date(row.recorded_at).toLocaleString())} · recorded by ${escapeHtml(row.recorded_by)}</small>${row.voided ? `<small>Voided: ${escapeHtml(row.void_reason)}</small>` : `<button type="button" data-lead-duty-void="${row.id}">Void resolution</button>`}</article>`).join('');
    updateLeadFields(); leadDialog.showModal();
  }
  function updateLeadFields() {
    const named = leadForm.elements.outcome.value === 'PERFORMED';
    leadForm.elements.employee_id.disabled = !named;
    leadForm.elements.employee_id.required = named;
  }
  leadForm?.elements.outcome.addEventListener('change', updateLeadFields);
  $('[data-lead-duty-close]')?.addEventListener('click', () => leadDialog.close());
  leadForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const values = new FormData(leadForm), shiftId = Number(values.get('shift_id'));
    try {
      const data = await api(`/v2/scheduling/api/shifts/${shiftId}/lead-duty`, 'POST', {
        outcome: values.get('outcome'), employee_id: values.get('employee_id') ? Number(values.get('employee_id')) : null,
        reason: values.get('reason')});
      render(data.board, shiftId); leadDialog.close(); announce(data.message);
    } catch (error) { showError(error, $('[data-lead-duty-errors]')); }
  });
  leadDialog?.addEventListener('click', async event => {
    const button = event.target.closest('[data-lead-duty-void]');
    if (!button) return;
    const reason = leadForm.elements.reason.value.trim();
    if (!reason) { showError(new Error('Enter a reason before voiding.'), $('[data-lead-duty-errors]')); return; }
    try {
      const data = await api(`/v2/scheduling/api/lead-duty/${button.dataset.leadDutyVoid}/void`, 'POST', {reason});
      render(data.board); leadDialog.close(); announce(data.message);
    } catch (error) { showError(error, $('[data-lead-duty-errors]')); }
  });

  function localDateTimeValue(value = new Date()) {
    const offset = value.getTimezoneOffset() * 60000;
    return new Date(value.getTime() - offset).toISOString().slice(0, 16);
  }

  function renderAttendanceHistory(shift) {
    const history = $('[data-attendance-history]');
    history.innerHTML = (shift.attendance_events || []).length ? shift.attendance_events.map((event) => `<article class="schedule-attendance-event${event.voided ? ' is-voided' : ''}">
      <strong>${escapeHtml(event.event_label)}${event.replacement_employee_name ? ` · ${escapeHtml(event.replacement_employee_name)}` : ''}</strong>
      <span>${escapeHtml(new Date(event.event_at).toLocaleString())} · recorded by ${escapeHtml(event.recorded_by)}</span>
      ${event.note ? `<span>${escapeHtml(event.note)}</span>` : ''}
      ${event.voided ? `<small>Voided by ${escapeHtml(event.voided_by || 'unknown')}: ${escapeHtml(event.void_reason || '')}</small>` : `<button type="button" data-attendance-void="${event.id}">Void / correct</button>`}
      ${event.point_reconciliation_required ? '<small class="schedule-roster__warning">Active point entry linked to this voided event needs management reconciliation.</small>' : ''}
      ${board.actions.manage_attendance_points ? `<a class="v2-button v2-button--secondary" href="/v2/scheduling/employees/${event.original_employee_id}?attendance_event_id=${event.id}#attendance-points">${event.point_entry_count ? `Review ${event.point_entry_count} point entr${event.point_entry_count === 1 ? 'y' : 'ies'}` : 'Add point entry'}</a>` : ''}
    </article>`).join('') : '<p class="v2-muted">Normal shifts need no entry. After completion, published work is presumed unless exception evidence contradicts it.</p>';
  }

  let exceptionShift = null;
  let commitmentCandidates = [];
  function attendancePayload(values) {
    const person = values.get('commitment_employee_id') ? Number(values.get('commitment_employee_id')) : null;
    return {
      event_type: values.get('event_type') || null,
      commitment_employee_id: person,
      cancel_coverage: !person,
      replace_existing: true,
      coverage_start_time: person ? values.get('coverage_start_time') || null : null,
      coverage_end_time: person ? values.get('coverage_end_time') || null : null,
      coverage_break_minutes: person && values.get('coverage_break_minutes') !== '' ? Number(values.get('coverage_break_minutes')) : null,
      note: values.get('note') || '',
    };
  }
  function updateAttendanceFields() {
    if (!exceptionShift) return;
    const person = Number(attendanceForm.elements.commitment_employee_id.value);
    const candidate = commitmentCandidates.find(row => row.id === person);
    const warnings = candidate?.warnings || candidate?.warning_messages || [];
    const messages = warnings.map(row => typeof row === 'string' ? row : row.message).filter(Boolean);
    const warningBox = $('[data-coverage-warnings]');
    warningBox.hidden = !messages.length;
    warningBox.textContent = messages.length ? `⚠ ${messages.join(' · ')}` : '';
    $('[data-coverage-hours]').hidden = !person;
  }

  let coverageCandidateRequest = 0;
  async function refreshCoverageCandidates() {
    if (!exceptionShift) return;
    const shiftId = exceptionShift.id, requestId = ++coverageCandidateRequest;
    const params = new URLSearchParams();
    for (const key of ['coverage_start_time', 'coverage_end_time', 'coverage_break_minutes']) {
      const value = attendanceForm.elements[key].value;
      if (value !== '') params.set(key, value);
    }
    try {
      const data = await api(`/v2/scheduling/api/shifts/${shiftId}/coverage-candidates?${params}`, 'GET');
      if (exceptionShift?.id !== shiftId || requestId !== coverageCandidateRequest) return;
      commitmentCandidates = data.candidates;
      const select = attendanceForm.elements.commitment_employee_id, previous = select.value;
      const previousName = select.selectedOptions[0]?.textContent;
      select.innerHTML = '<option value="">No replacement</option>' + commitmentCandidates.filter(row => row.id !== exceptionShift.employee_id).map(row => `<option value="${row.id}">${escapeHtml(row.name)}</option>`).join('');
      // A changed candidate list must never silently cancel existing coverage.
      if (previous && !commitmentCandidates.some(row => row.id === Number(previous))) select.add(new Option(previousName || 'Current replacement', previous));
      select.value = previous;
      updateAttendanceFields();
    } catch (error) { if (requestId === coverageCandidateRequest) showError(error, $('[data-attendance-errors]')); }
  }

  async function openAttendance(selectedShift) {
    const shift = selectedShift.is_coverage ? board.shifts.find(row => row.id === selectedShift.source_shift_id) : selectedShift;
    if (!shift) return;
    exceptionShift = shift;
    commitmentCandidates = [];
    attendanceForm.reset();
    attendanceForm.elements.shift_id.value = shift.id;
    attendanceForm.elements.commitment_employee_id.innerHTML = '<option value="">No replacement</option>' + board.attendance_replacements.filter(row => row.id !== shift.employee_id).map(row => `<option value="${row.id}">${escapeHtml(row.name)}</option>`).join('');
    const current = (shift.attendance_events || []).filter(row => !row.voided && ['CALLED_OUT', 'LATE', 'LEFT_EARLY', 'NO_CALL_NO_SHOW'].includes(row.event_type)).at(-1);
    attendanceForm.elements.event_type.value = current?.event_type || '';
    const coverage = shift.exception?.commitment;
    attendanceForm.elements.commitment_employee_id.value = coverage?.employee_id || '';
    attendanceForm.elements.coverage_start_time.value = coverage?.start_time?.slice(0, 5) || shift.start_time;
    attendanceForm.elements.coverage_end_time.value = coverage?.end_time?.slice(0, 5) || shift.end_time;
    attendanceForm.elements.coverage_break_minutes.value = coverage?.unpaid_break_minutes ?? shift.unpaid_break_minutes ?? 0;
    attendanceForm.elements.note.value = current?.note || '';
    $('[data-coverage-hours]').open = !!coverage && (attendanceForm.elements.coverage_start_time.value !== shift.start_time || attendanceForm.elements.coverage_end_time.value !== shift.end_time);
    $('[data-attendance-employee]').textContent = shift.employee_name || shift.exception?.original?.employee_name || 'Unassigned';
    $('[data-attendance-summary]').textContent = `${shift.shift_date} · ${shift.time_label} · ${shift.store_name}`;
    clearError($('[data-attendance-errors]'));
    renderAttendanceHistory(shift);
    $('[data-commitment-history]').innerHTML = (shift.exception?.commitment_history || []).map(row => `<article class="schedule-attendance-event"><strong>Coverage: ${escapeHtml(row.employee_name)}</strong>${row.note ? `<span>${escapeHtml(row.note)}</span>` : ''}<small>${escapeHtml(row.recorded_at)} · ${escapeHtml(row.recorded_by)}</small>${row.voided ? `<small>Canceled/superseded: ${escapeHtml(row.void_reason || '')}</small>` : `<button type="button" data-commitment-void="${row.id}">Cancel coverage</button>`}</article>`).join('');
    updateAttendanceFields();
    attendanceDialog.showModal();
    attendanceDialog.scrollTop = 0;
    await refreshCoverageCandidates();
  }

  $('[data-commitment-history]')?.addEventListener('click', async event => {
    const button = event.target.closest('[data-commitment-void]');
    if (!button) return;
    try {
      const data = await api(`/v2/scheduling/api/coverage-commitments/${button.dataset.commitmentVoid}/void`, 'POST', {});
      render(data.board); attendanceDialog.close(); announce(data.message);
    } catch (error) { showError(error, $('[data-attendance-errors]')); }
  });

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
    if (pendingMove || writeInFlight || refreshRequired) {
      showError(Object.assign(new Error(refreshRequired ? 'Refresh the board to confirm the last save before editing again.' : 'A schedule change is still saving. Please wait before making another move.'), {requiresRefresh: refreshRequired}));
      return;
    }
    const conflict = employeeStoreConflict(changes.employee_id, changes.shift_date, changes.store_id, shift.id);
    if (conflict) {
      showError(new Error(`This employee is already scheduled at ${conflict.store_name} on this day. Employees may work at only one store per day.`));
      return;
    }
    let restore = null;
    let move = null;
    let acknowledged = false;
    let rejected = null;
    const baseVersion = board.period.version;
    try {
      if (board.period?.generated_origin) await ensureManualEditing();
      const targetCell = $$('[data-drop-cell]').find((cell) => (cell.dataset.employeeId || null) === (changes.employee_id == null ? null : String(changes.employee_id)) && cell.dataset.shiftDate === changes.shift_date && (changes.employee_id != null || Number(cell.dataset.storeId) === Number(changes.store_id)));
      const hard = targetCell?.querySelector('.schedule-indicator--hard_unavailable,.schedule-indicator--time_off');
      if (hard && !changes.operational_override) {
        if (board.actions.override_operational) {
          const codes = targetCell.querySelector('.schedule-indicator--time_off') ? ['APPROVED_TIME_OFF'] : ['HARD_WEEKDAY_LOCKOUT'];
          const exception = confirmOperationalException(codes, 'This destination conflicts with availability or approved PTO.');
          if (!exception) return;
          Object.assign(changes, exception);
        } else {
          if (!board.actions.override_hard_unavailability || targetCell.querySelector('.schedule-indicator--time_off')) throw new Error('This destination requires an explicit Owner exception.');
          if (!confirm('This destination conflicts with hard unavailability. Continue with an explicit override?')) return;
          changes.override_hard_unavailability = true;
          changes.override_reason = (prompt('Enter an override reason:') || '').trim();
          if (!changes.override_reason) throw new Error('An override reason is required.');
        }
      }
      if (pendingMove || writeInFlight || refreshRequired) throw new Error('A schedule change is still saving. Please wait before moving this shift.');
      if (board.period.version !== baseVersion) throw Object.assign(new Error('The schedule changed while confirming. Refresh before moving this shift.'), {status:409,requiresRefresh:true});
      performance.mark('schedule-move-start');
      // Keep the authoritative board unchanged until acknowledgement. Moving
      // the existing node gives immediate feedback and exact rollback.
      const card = document.getElementById(`shift-card-${shift.id}`);
      const destination = targetCell?.querySelector('.schedule-cell__shifts');
      pendingMove = move = {};
      if (card && destination) {
        const parent = card.parentNode, next = card.nextSibling;
        const label = card.querySelector('.shift-card-store-name');
        const oldLabel = label?.textContent;
        const oldTone = card.dataset.storeTone;
        const store = board.stores.find(row => row.id === changes.store_id);
        restore = () => {
          parent.insertBefore(card, next?.parentNode === parent ? next : null);
          card.removeAttribute('aria-busy'); delete card.dataset.pendingMove;
          if (label) label.textContent = oldLabel;
          if (oldTone === undefined) delete card.dataset.storeTone;
          else card.dataset.storeTone = oldTone;
        };
        if (label && store) label.textContent = store.name;
        if (changes.store_id !== shift.store_id) {
          const exemplar = board.shifts.find(row => row.store_id === changes.store_id);
          card.dataset.storeTone = document.getElementById(`shift-card-${exemplar?.id}`)?.dataset.storeTone || 'neutral';
        }
        destination.append(card);
        card.dataset.pendingMove = '';
        card.setAttribute('aria-busy', 'true');
        performance.mark('schedule-move-optimistic');
        requestAnimationFrame(() => requestAnimationFrame(() => {
          performance.clearMeasures('schedule-move-feedback');
          performance.measure('schedule-move-feedback', 'schedule-move-start');
        }));
      }
      moveDialog.close();
      announce('Shift moved. Saving; totals and warnings will update when confirmed.');
      performance.clearMeasures('schedule-move-handler');
      performance.measure('schedule-move-handler', 'schedule-move-start');
      const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}`, 'PATCH', shiftPayload(shift, changes), pendingMove);
      acknowledged = true;
      pendingMove.acknowledged = true;
      const deferred = pendingMove.deferredBoard;
      const merged = data.delta ? mergeMoveDelta(board, data.delta) : {snapshot:data.board,changedIds:null};
      if (deferred && deferred.period?.id !== merged.snapshot.period?.id) throw new Error('Schedule revision changed while saving.');
      if (deferred && deferred.period.version > merged.snapshot.period.version) render(deferred, shift.id);
      else render(merged.snapshot, shift.id, merged.changedIds);
      moveDialog.close();
      announce(data.message);
    } catch (error) {
      if (!acknowledged) restore?.();
      else { refreshRequired = true; error.requiresRefresh = true; error.message = 'The shift was saved, but the display could not refresh. Reload before editing again.'; }
      if (error.requiresRefresh) refreshRequired = true;
      rejected = error;
      showError(error);
    } finally { if (pendingMove === move) pendingMove = null; }
    // A definitive rejection made no change. A new, explicitly confirmed
    // exception is a new user decision; never retry an uncertain transport save.
    const codes = rejected?.data?.operational_exception_codes || [];
    if (rejected?.status === 422 && codes.length && !changes.operational_override && board.actions.override_operational) {
      const exception = confirmOperationalException(codes, rejected.message);
      if (exception && board.period.version === baseVersion) return saveMove(shift, {...changes, ...exception});
    }
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
      operational_override:values.has('operational_override'), override_pto:values.has('override_pto'),
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

  attendanceForm?.elements.commitment_employee_id.addEventListener('change', updateAttendanceFields);
  for (const key of ['coverage_start_time', 'coverage_end_time', 'coverage_break_minutes']) attendanceForm?.elements[key].addEventListener('change', refreshCoverageCandidates);
  attendanceForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!attendanceForm.reportValidity()) return;
    const values = new FormData(attendanceForm);
    const shiftId = Number(values.get('shift_id'));
    const button = attendanceForm.querySelector('[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = await api(`/v2/scheduling/api/shifts/${shiftId}/attendance`, 'POST', attendancePayload(values));
      render(data.board, shiftId);
      attendanceDialog.close();
      announce(data.attendance_warnings?.length ? `${data.message} Warning: ${data.attendance_warnings.join(', ')}.` : data.message);
    } catch (error) { showError(error, $('[data-attendance-errors]')); }
    finally { button.disabled = false; }
  });

  function bindCards() {
    $$('[data-shift-card]').forEach((card) => { card.onpointerdown = startShiftDrag; });
  }

  function beginDrag(event, kind, item, card) {
    if (event.button !== 0 || event.target.closest('button,summary') || matchMedia('(max-width:620px)').matches) return;
    drag = {kind, item, card, x: event.clientX, y: event.clientY, started: false, target: null};
    card.setPointerCapture(event.pointerId);
    card.onpointermove = moveDrag;
    card.onpointerup = endDrag;
    card.onpointercancel = cancelDrag;
  }
  function startShiftDrag(event) { const shift = shiftByCard(event.currentTarget); if (!shift || shift.is_coverage) return; beginDrag(event, 'scheduled', shift, event.currentTarget); }
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
    if (!target) { drag.target = null; return; }
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

    const attendanceVoid = event.target.closest('[data-attendance-void]');
    if (attendanceVoid) {
      const reason = attendanceForm.elements.note.value.trim() || 'Attendance corrected.';
      const shiftId = Number(attendanceForm.elements.shift_id.value);
      try {
        const data = await api(`/v2/scheduling/api/attendance/${Number(attendanceVoid.dataset.attendanceVoid)}/void`, 'POST', {reason});
        render(data.board, shiftId); attendanceDialog.close(); announce(data.message);
      } catch (error) { showError(error, $('[data-attendance-errors]')); }
      return;
    }

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

    const leadControl = event.target.closest('[data-history-lead]');
    if (leadControl) {
      api(`/v2/scheduling/api/history/${board.period.id}/lead/${leadControl.dataset.historyLead}`, 'POST', {
        expected_version: Number(board.period.version), assigned: leadControl.dataset.assigned === 'true',
        reason: 'Explicit Lead responsibility corrected during previous-week editing',
      }).then(async () => render(await api('/v2/scheduling/api/board', 'GET'))).catch(error => showError(error));
      return;
    }
    const card = event.target.closest('[data-shift-card]');
    if (card) {
      const shift = shiftByCard(card);
      if (event.target.closest('[data-lead-duty-open]')) openLeadDuty(shift);
      else if (event.target.closest('[data-callout-open]')) { await openAttendance(shift); }
      else if (event.target.closest('[data-attendance-open]')) openAttendance(shift);
      else if (event.target.closest('[data-shift-unlock]')) {
        try {
          await api(`/v2/scheduling/api/shifts/${shift.id}/lock`, 'POST', {locked: false, reason: 'Explicit management unlock'});
          render(await api('/v2/scheduling/api/board', 'GET'), shift.id);
          announce('Assignment unlocked for regeneration.');
        } catch (error) { showError(error); }
      }
      else if (event.target.closest('[data-shift-remove]')) await saveMove(shift, {employee_id: null, shift_date: shift.shift_date, store_id: shift.store_id});
      else if (event.target.closest('[data-shift-edit]')) openEditor(shift);
      else if (event.target.closest('[data-shift-move]')) openMove(shift);
      else if (event.target.closest('[data-shift-duplicate]')) {
        try {
          const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}/duplicate`, 'POST', {expected_version: Number(board.period.version)});
          render(data.board, data.shift?.id); announce(data.message);
        } catch (error) { showError(error); }
      } else if (event.target.closest('[data-shift-delete]') && (board.historical_edit || confirm('Delete this shift?'))) {
        try {
          const data = await api(`/v2/scheduling/api/periods/${board.period.id}/shifts/${shift.id}`, 'DELETE', {expected_version: Number(board.period.version)});
          render(data.board); announce(data.message);
        } catch (error) { showError(error); }
      }
      return;
    }

    const historyAction = event.target.closest('[data-history-action]')?.dataset.historyAction;
    if (historyAction) { openHistoryAction(historyAction); return; }
    if (event.target.closest('[data-create-draft]')) {
      try { await api('/v2/scheduling/api/periods', 'POST', {week_start_date: board.week.start, notes: ''}); location.reload(); } catch (error) { showError(error); }
    }
    if (event.target.closest('[data-clone-published]')) {
      try { await api(`/v2/scheduling/api/periods/${board.period.id}/clone-published`, 'POST', {expected_version: Number(board.period.version)}); location.reload(); } catch (error) { showError(error); }
    }
    if (event.target.closest('[data-publish-schedule]')) { openPublication(); return; }
    if (event.target.closest('[data-generate-schedule]')) {
      if (!confirm('Regenerate all unlocked assignments? Manual locks will be preserved.')) return;
      try {
        const data = await api(`/v2/scheduling/api/periods/${board.period.id}/generate`, 'POST');
        const incomplete = data.valid_schedule === false;
        announce(`${incomplete ? 'Draft remains incomplete' : 'Draft regenerated'}. ${data.assigned} assigned; ${data.uncovered.length} uncovered.`);
        if (incomplete || data.uncovered.length) {
          const details = (data.allocation?.diagnostics || []).filter(item => item.code === 'UNFILLED_COVERAGE').map(item => item.message);
          const validation = data.final_validation_errors || [];
          alert(`Draft saved for review. ${data.uncovered.length} shift(s) uncovered; ${(data.lead_uncovered || []).length} missing Lead coverage.\n\n${[...details, ...validation].join('\n')}\n\nReview the board coverage warnings for employee constraints before approval.`);
        }
        location.reload();
      } catch (error) { showError(error); }
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
    if (openTool && !event.target.closest('[data-schedule-tools]') && !event.target.closest('[data-tool-toggle]') && !event.target.closest('[data-open-warnings]')) closeTools({restoreFocus: false});
  });

  renderStoreShifts();
  bindCards();
  bindWarnings();

  let swapKind = 'REPLACE';
  const swapDialog = $('[data-swap-dialog]'), swapForm = $('[data-swap-form]');
  function syncSwapKind() {
    swapKind = swapForm.elements.kind.value;
    const replacing = swapKind === 'REPLACE';
    const other = swapForm.elements.other_shift_id;
    const replacement = swapForm.elements.replacement_employee_id;
    $('[data-other-shift-label]').hidden = replacing;
    other.disabled = replacing;
    other.required = !replacing;
    $('[data-replacement-label]').hidden = !replacing;
    replacement.disabled = !replacing;
    replacement.required = replacing;
    if (replacing) other.value = '';
    else replacement.value = '';
    $('[data-other-shift-title]').textContent = swapKind === 'STORE' ? "Other employee's assignment" : 'Other shift';
    other.options[0].textContent = swapKind === 'STORE' ? "Choose another employee's assignment" : 'Choose another shift';
    $('[data-swap-description]').textContent = {
      REPLACE: 'Choose who will work this shift instead.',
      SHIFT: 'Exchange the employees on two shifts.',
      STORE: 'Exchange where two employees will work on the same day.',
    }[swapKind];
  }
  swapForm?.elements.kind.addEventListener('change', syncSwapKind);
  document.querySelectorAll('[data-swap-open]').forEach(button => button.addEventListener('click', () => {
    swapForm.reset();
    swapForm.elements.kind.value = button.dataset.swapOpen;
    $('[data-swap-title]').textContent = 'Change assignment';
    const choices = board.shifts.filter(s => s.employee_id).map(s => `<option value="${s.id}">${escapeHtml(s.employee_name)} · ${escapeHtml(s.shift_date)} · ${escapeHtml(s.store_name)} · ${escapeHtml(s.time_label)}</option>`).join('');
    swapForm.elements.shift_id.innerHTML = choices;
    swapForm.elements.other_shift_id.innerHTML = '<option value="">Choose another shift</option>' + choices;
    syncSwapKind();
    $('[data-swap-error]').textContent = '';
    $('[data-swap-error]').hidden = true;
    swapDialog.showModal();
  }));
  $('[data-swap-close]')?.addEventListener('click', () => swapDialog.close());
  swapForm?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const result = await api('/v2/scheduling/api/assignment-change', 'POST', {
        shift_id: Number(swapForm.elements.shift_id.value), other_shift_id: swapKind !== 'REPLACE' ? Number(swapForm.elements.other_shift_id.value) || null : null,
        replacement_employee_id: swapKind === 'REPLACE' ? Number(swapForm.elements.replacement_employee_id.value) || null : null,
        operational_override:!!swapForm.elements.operational_override?.checked,override_pto:!!swapForm.elements.override_pto?.checked,
        kind: swapKind === 'STORE' ? 'STORE' : 'SHIFT', reason: swapForm.elements.reason.value, expected_versions: {[board.period.id]: board.period.version},
      });
      const target = new URL(location.href);
      target.searchParams.set('period_id', result.period_ids[0]);
      target.searchParams.delete('history');
      location.assign(target);
    } catch (error) { $('[data-swap-error]').textContent = error.message; $('[data-swap-error]').hidden = false; }
  });

})();
