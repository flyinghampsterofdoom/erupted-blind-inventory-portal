/* Calendar navigation only: selecting a week performs the existing board GET. */
(() => {
  'use strict';
  const iso = date => date.toISOString().slice(0, 10);
  const parse = value => new Date(`${value}T12:00:00Z`);
  const addDays = (date, count) => { const next = new Date(date); next.setUTCDate(next.getUTCDate() + count); return next; };
  const sunday = date => addDays(date, -date.getUTCDay());
  const monthDate = (year, month, day = 1) => {
    const date = parse('2000-01-01'); date.setUTCFullYear(year, month, day); return date;
  };
  const monthGrid = month => {
    const first = sunday(monthDate(month.getUTCFullYear(), month.getUTCMonth()));
    const last = addDays(sunday(monthDate(month.getUTCFullYear(), month.getUTCMonth() + 1, 0)), 6);
    const dates = [];
    for (let day = first; day <= last; day = addDays(day, 1)) dates.push(day);
    return dates;
  };
  const weekURL = (current, start) => {
    const target = new URL(current);
    target.searchParams.set('start', start);
    ['period_id', 'history', 'message', 'error'].forEach(key => target.searchParams.delete(key));
    return target.href;
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = {iso, parse, addDays, sunday, monthDate, monthGrid, weekURL};
  if (typeof document === 'undefined') return;
  const root = document.querySelector('[data-schedule-board]');
  const dialog = root?.querySelector('[data-week-picker]');
  if (!dialog) return;
  const $ = selector => dialog.querySelector(selector);
  const trigger = root.querySelector('[data-week-picker-open]');
  const viewed = parse(root.dataset.weekStart);
  const days = $('[data-calendar-days]');
  const monthInput = $('[data-calendar-month]');
  const yearInput = $('[data-calendar-year]');
  const message = $('[data-calendar-message]');
  let month, focused, requestId = 0;
  const cache = new Map();
  const formatter = new Intl.DateTimeFormat(undefined, {dateStyle: 'long', timeZone: 'UTC'});
  for (let index = 0; index < 12; index++) {
    const option = document.createElement('option');
    option.value = index;
    option.textContent = monthDate(2026, index).toLocaleString(undefined, {month: 'long', timeZone: 'UTC'});
    monthInput.append(option);
  }
  function focusDate() { days.querySelector(`[data-date="${iso(focused)}"]`)?.focus(); }
  function markStatus(weeks) {
    for (const week of weeks) {
      const row = days.querySelector(`[data-week="${week.start}"]`);
      if (!row) continue;
      row.className = `calendar-${week.status}`;
      row.querySelectorAll('button').forEach(button => {
        button.setAttribute('aria-label', `${formatter.format(parse(button.dataset.date))}; week of ${formatter.format(parse(week.start))}: ${week.label}${week.start === iso(viewed) ? '; currently viewed week' : ''}`);
        button.title = week.label;
      });
    }
  }
  async function loadStatus(dates) {
    const id = ++requestId;
    const key = `${iso(dates[0])}/${iso(dates.at(-1))}`;
    const entry = cache.get(key);
    if (entry && Date.now() - entry.at < 30000) {
      markStatus(entry.weeks); message.textContent = ''; return;
    }
    message.textContent = 'Loading scheduling status…';
    const url = new URL('/v2/scheduling/api/calendar-weeks', location.origin);
    new URL(location.href).searchParams.getAll('store_id').forEach(value => url.searchParams.append('store_id', value));
    url.searchParams.set('start', iso(dates[0])); url.searchParams.set('end', iso(dates.at(-1)));
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('status unavailable');
      const data = await response.json();
      if (id !== requestId || !dialog.open) return;
      cache.set(key, {at: Date.now(), weeks: data.weeks});
      markStatus(data.weeks); message.textContent = '';
    } catch (_) {
      if (id === requestId && dialog.open) message.textContent = 'Scheduling status unavailable. You can still choose a week. Reopen the calendar to retry.';
    }
  }
  function render(moveFocus = false) {
    monthInput.value = month.getUTCMonth(); yearInput.value = month.getUTCFullYear();
    const dates = monthGrid(month);
    days.replaceChildren();
    for (let index = 0; index < dates.length; index += 7) {
      const row = document.createElement('tr');
      const week = iso(dates[index]); row.dataset.week = week;
      row.setAttribute('aria-selected', String(week === iso(viewed)));
      if (week === iso(viewed)) row.dataset.viewed = 'true';
      for (const date of dates.slice(index, index + 7)) {
        const cell = document.createElement('td');
        const button = document.createElement('button');
        button.type = 'button'; button.dataset.date = iso(date);
        button.tabIndex = iso(date) === iso(focused) ? 0 : -1;
        button.textContent = date.getUTCDate();
        button.setAttribute('aria-label', `${formatter.format(date)}; week of ${formatter.format(dates[index])}; status unknown${week === iso(viewed) ? '; currently viewed week' : ''}`);
        if (date.getUTCMonth() !== month.getUTCMonth()) button.className = 'calendar-outside';
        cell.append(button); row.append(cell);
      }
      days.append(row);
    }
    if (moveFocus) focusDate();
    loadStatus(dates);
  }
  function changeMonth(delta, moveFocus = false) {
    const next = monthDate(month.getUTCFullYear(), month.getUTCMonth() + delta);
    if (next.getUTCFullYear() < 2 || next.getUTCFullYear() > 9998) return;
    month = next; focused = next; render(moveFocus);
  }
  trigger.addEventListener('click', () => {
    cache.clear(); month = monthDate(viewed.getUTCFullYear(), viewed.getUTCMonth()); focused = viewed;
    dialog.showModal(); trigger.setAttribute('aria-expanded', 'true'); render(true);
  });
  dialog.addEventListener('close', () => { ++requestId; trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  $('[data-calendar-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  $('[data-calendar-previous]').addEventListener('click', () => changeMonth(-1));
  $('[data-calendar-next]').addEventListener('click', () => changeMonth(1));
  for (const input of [monthInput, yearInput]) input.addEventListener('change', () => {
    if (!yearInput.checkValidity() || !yearInput.value) { yearInput.reportValidity(); return; }
    month = monthDate(Number(yearInput.value), Number(monthInput.value)); focused = month; render();
  });
  days.addEventListener('click', event => {
    const button = event.target.closest('[data-date]'); if (!button) return;
    const target = weekURL(location.href, iso(sunday(parse(button.dataset.date))));
    dialog.close(); location.assign(target);
  });
  days.addEventListener('focusin', event => {
    const button = event.target.closest('[data-date]'); if (!button) return;
    focused = parse(button.dataset.date);
    days.querySelectorAll('button').forEach(item => { item.tabIndex = item === button ? 0 : -1; });
  });
  days.addEventListener('keydown', event => {
    if (!event.target.matches('[data-date]')) return;
    const offsets = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -focused.getUTCDay(), End: 6 - focused.getUTCDay()};
    if (event.key === 'PageUp' || event.key === 'PageDown') { event.preventDefault(); changeMonth(event.key === 'PageUp' ? -1 : 1, true); return; }
    if (!(event.key in offsets)) return;
    event.preventDefault();
    const next = addDays(focused, offsets[event.key]);
    if (next.getUTCFullYear() < 2 || next.getUTCFullYear() > 9998) return;
    focused = next;
    if (focused.getUTCMonth() !== month.getUTCMonth() || focused.getUTCFullYear() !== month.getUTCFullYear()) {
      month = monthDate(focused.getUTCFullYear(), focused.getUTCMonth()); render(true);
    } else focusDate();
  });
  root.addEventListener('schedule:updated', () => { cache.clear(); if (dialog.open) render(); });
  window.addEventListener('pageshow', () => cache.clear());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { cache.clear(); if (dialog.open) render(); } });
})();
