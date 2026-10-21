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
    $('[data-scheduled-shift-count]').textContent = board.shifts.length;
    $('[data-open-shift-count]').textContent = board.summary.open_shift_count ? `${board.summary.open_shift_count} open` : '';
    $('[data-business-hours-summary]').textContent = hoursSummary(board.business_hours || []);
    const warning = $('[data-warning-overview]');
    if (warning) {
      warning.classList.toggle('has-blockers',board.summary.serious_warning_count>0);
      $('[data-warning-blockers]').textContent=board.summary.serious_warning_count ? `${board.summary.serious_warning_count} serious` : '';
    }
    const serious = board.warnings.filter(warning => warning.severity === 'SERIOUS');
    $('[data-serious-warnings]').hidden = !serious.length;
    $('[data-serious-warning-list]').replaceChildren(...serious.slice(0,3).map(warning => {
      const item = document.createElement('li'); item.textContent = warning.message; return item;
    }));
    const publish = $('[data-publish-schedule]');
    if (publish) {
      publish.disabled = !(board.period?.approval || board.actions.approve);
      $('#schedule-approval-permission').hidden = !publish.disabled;
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
