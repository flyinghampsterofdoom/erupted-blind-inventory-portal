(() => {
  'use strict';
  function status(board) {
    const p = board.period;
    if (!p) return {tone:'neutral',label:'Not generated'};
    if (p.is_historical || p.status === 'ARCHIVED') return {tone:'neutral',label:board.mode_label};
    if (p.status === 'PUBLISHED') return {tone:'published',label:'Published'};
    if (board.current_published_period_id) return {tone:'mixed',label:'Published + unpublished revision'};
    if (!p.generated_at && !board.shifts.length) return {tone:'neutral',label:'Not generated · Empty draft'};
    return {tone:'draft',label:'Draft · Unpublished'};
  }
  function labor(labor) {
    if (!labor) return null;
    const missing = labor.missing_rate_shift_count;
    return {value:missing && !labor.costed_paid_hours ? 'Not available' : `$${Number(labor.estimated_cost).toFixed(2)}`,
      note:missing ? `${missing} shifts missing rates${labor.costed_paid_hours ? ' · partial estimate' : ''}` : ''};
  }
  function hoursSummary(items) {
    const closed = items.filter(item => item.closed).length;
    const modified = items.length - closed;
    const events = [...new Set(items.map(item => item.event_name).filter(Boolean))].slice(0,2).join(', ');
    return [events, [closed ? `${closed} closure${closed===1?'':'s'}` : '', modified ? `${modified} modified hours` : ''].filter(Boolean).join(' · ')].filter(Boolean).join(' · ');
  }
  function weekLabel(board) {
    const date = value => new Date(`${value}T12:00:00Z`);
    return new Intl.DateTimeFormat('en-US',{year:'numeric',month:'long',day:'numeric',timeZone:'UTC'}).formatRange(date(board.week.start),date(board.week.end));
  }
  function update(root, board) {
    const $ = selector => root.querySelector(selector);
    const badge = $('[data-board-mode]'), state = status(board);
    badge.className = `schedule-status schedule-status--${state.tone}`; badge.textContent = state.label;
    const cost = labor(board.labor);
    if (cost && $('[data-labor-cost]')) { $('[data-labor-cost]').textContent=cost.value; $('[data-missing-rates]').textContent=cost.note; }
    $('[data-scheduled-shift-count]').textContent = board.summary.effective_shift_count ?? board.shifts.length;
    $('[data-open-shift-count]').textContent = board.summary.open_shift_count ? `${board.summary.open_shift_count} open` : '';
    $('[data-business-hours-summary]').textContent = hoursSummary(board.business_hours || []);
    const warning = $('[data-warning-overview]');
    if (warning) {
      warning.classList.toggle('has-blockers',board.summary.serious_warning_count>0);
      $('[data-warning-blockers]').textContent=board.summary.serious_warning_count ? `${board.summary.serious_warning_count} serious` : '';
    }
    const serious = board.warnings.filter(warning => warning.severity === 'SERIOUS');
    $('[data-serious-warnings]').hidden = !serious.length;
    $('[data-serious-warning-count]').textContent = serious.length;
    $('[data-serious-warning-list]').replaceChildren(...serious.slice(0,3).map(warning => {
      const item = document.createElement('li'); item.textContent = warning.message; return item;
    }));
    const secondary = $('.schedule-secondary-actions');
    if (secondary) {
      for (const [attribute, visible, label] of [
        ['data-add-shift', board.editable, board.actions.direct_edit ? 'Add custom shift' : 'Add shift'],
        ['data-generate-schedule', board.actions.generate, 'Regenerate unlocked shifts'],
      ]) {
        let button = secondary.querySelector(`[${attribute}]`);
        if (!button && visible) {
          secondary.insertAdjacentHTML('beforeend', `<button class="v2-button v2-button--secondary" type="button" ${attribute}></button>`);
          button = secondary.querySelector(`[${attribute}]`);
        }
        if (button) { button.hidden = !visible; button.textContent = label; }
      }
    }
    // A published swap can produce a draft with a new period ID. Rebuild its
    // delegated action controls from canonical state without reloading the page.
    const completion = $('.schedule-completion-actions');
    if (completion) {
      const approvalRequired = !board.actions.approve && !board.period?.approval;
      completion.innerHTML = (board.actions.create_draft ? '<button class="v2-button v2-button--primary" type="button" data-create-draft>Create draft</button>' : '')
        + (board.actions.clone_published ? '<button class="v2-button v2-button--primary" type="button" data-clone-published>Create editable revision</button>' : '')
        + (board.actions.publish ? `<button class="v2-button v2-button--primary schedule-publish" type="button" data-publish-schedule aria-describedby="schedule-approval-permission" ${approvalRequired ? 'disabled' : ''}>Publish Schedule</button><small id="schedule-approval-permission" ${approvalRequired ? '' : 'hidden'}>Approval permission is required for this draft.</small>` : '');
    }
  }
  const helpers = {status,labor,hoursSummary,weekLabel,update};
  if (typeof module !== 'undefined' && module.exports) module.exports=helpers;
  if (typeof document === 'undefined') return;
  window.SchedulePresentation = helpers;
  const root = document.querySelector('[data-schedule-board]');
  if (!root) return;
  update(root,JSON.parse(root.querySelector('[data-board-json]').textContent));
  const actions = root.querySelector('[data-schedule-actions]');
  const narrow = matchMedia('(max-width:620px)');
  const arrange = () => { actions.open = !narrow.matches; root.querySelector('[data-schedule-review]').open = !narrow.matches; };
  arrange(); narrow.addEventListener('change',arrange);
})();
