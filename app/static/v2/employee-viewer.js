/* Presentation of the existing employee page; all writes use its original forms. */
(() => {
  'use strict';
  const root = document.querySelector('[data-employee-schedule]');
  if (!root) return;
  const mount = root.querySelector('[data-employee-viewer]');
  const fallback = root.querySelector('[data-employee-fallback]');
  const detail = root.querySelector('[data-shift-detail]');
  const requests = root.querySelector('[data-requests-dialog]');
  const historySection = root.querySelector('#shift-requests');
  const message = root.querySelector('[data-request-status]');
  const error = root.querySelector('[data-request-error]');
  let model;
  try { model = JSON.parse(root.querySelector('[data-employee-week]').textContent); }
  catch (_) { return; } // The original server-rendered page remains usable.
  const identity = model.employeeId, cache = new Map();
  const state = { tab: 'my', day: null, mode: matchMedia('(min-width:1000px)').matches ? 'week' : 'day' };
  let loadedAt = Date.now(), stale = false, loading = false, generation = 0, controller, opener, selected;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = value => new Date(value + 'T12:00:00');
  const format = (value, options) => date(value).toLocaleDateString('en-US', options);
  const shortDay = value => format(value, { weekday:'short' });
  const fullDay = value => format(value, { weekday:'long', month:'long', day:'numeric' });
  const number = value => Number(value).toLocaleString('en-US', { maximumFractionDigits:2 });
  const time = value => { const [h,m] = value.split(':').map(Number); return `${h%12||12}${m?':'+String(m).padStart(2,'0'):''} ${h>=12?'PM':'AM'}`; };
  const store = shift => model.stores.find(s => s.id === shift.storeId);
  const mine = () => model.shifts.filter(s => s.employeeId === identity);
  const own = s => s.employeeId === identity;
  const rows = () => [...root.querySelectorAll('[data-request-list] [data-request-id]')];
  const offers = () => rows().filter(r => r.dataset.incoming === 'true' && model.days.includes(r.dataset.offerDate));
  const pending = s => rows().some(r => r.dataset.outgoing === 'true' && Number(r.dataset.offerShiftId) === s.sourceId && r.dataset.requestState.startsWith('PENDING_'));
  const empty = (title, text) => `<div class="es-empty"><h2>${title}</h2><p>${text}</p></div>`;
  const badge = s => `<span class="es-store" data-store-tone="${escape(store(s)?.tone)}">${escape(store(s)?.name || 'Location unavailable')}</span>`;
  const notices = (id, d) => (model.notices || []).filter(n=>n.storeId===id && n.date===d).map(n=>'<span class="es-sub">'+escape(n.label)+'</span>').join('');
  function labels(s) { return `${s.coverage?'<span class="pending">Coverage</span>':''}${s.exception?'<span class="pending">'+escape(s.statuses.join(', ') || 'Coverage assigned')+'</span>':''}${pending(s)?'<span class="pending">Awaiting response / approval</span>':''}`; }
  function card(s, showName = false) {
    return `<button type="button" class="es-card ${own(s)?'own':''}" data-view-shift="${s.id}" data-store-tone="${escape(store(s)?.tone)}" aria-label="${escape(s.name+', '+store(s)?.name+', '+fullDay(s.date)+', '+time(s.start)+' to '+time(s.end)+(own(s)?', your shift':''))}"><span class="es-card-top">${badge(s)}${own(s)?'<span class="es-you">Your shift</span>':''}</span>${showName?'<span class="es-name">'+escape(s.name)+'</span>':''}<strong class="es-time">${time(s.start)} – ${time(s.end)}</strong><span class="es-sub">${number(s.paidHours)} paid hrs${s.coverage?' · Covering '+escape(s.covering || 'original employee'):''}</span>${labels(s)}${notices(s.storeId,s.date)}</button>`;
  }
  function assignment(s) {
    return `<button type="button" class="es-assignment ${own(s)?'own':''}" data-view-shift="${s.id}" aria-label="${escape(s.name+', '+store(s)?.name+', '+fullDay(s.date)+', '+time(s.start)+' to '+time(s.end)+(own(s)?', your shift':''))}"><strong>${escape(s.name)}</strong><span class="es-sub">${time(s.start)} – ${time(s.end)}</span>${own(s)?'<span class="es-you">Your shift</span>':''}${labels(s)}</button>`;
  }
  function remember() {
    cache.delete(model.weekStart);
    cache.set(model.weekStart, { model, html:fallback.innerHTML, loadedAt });
    while (cache.size > 3) cache.delete(cache.keys().next().value);
  }
  function render() {
    const active=mount.contains(document.activeElement)?document.activeElement:null;
    const focusKey=active && ['data-view','data-day','data-mode','data-week','data-current','data-full-personal'].find(a=>active.hasAttribute(a));
    const focusValue=focusKey?active.getAttribute(focusKey):null;
    const personal = mine(), desktop = matchMedia('(min-width:1000px)').matches;
    const isMatrix = desktop && state.tab === 'full' && state.mode === 'week';
    const next = personal.find(s => s.paidHours > 0 && (s.date > model.today || s.date === model.today && (!model.nowTime || s.end > model.nowTime)));
    let html = `<div class="es-heading"><h1>Schedule</h1><div class="es-shortcuts">${model.timeOff?'<a href="/v2/scheduling/my-time-off">Time off</a>':''}<button type="button" data-show-requests>Shift requests</button></div></div><div class="es-navigation"><nav class="es-tabs" aria-label="Schedule views"><button type="button" data-view="my" aria-pressed="${state.tab==='my'}">My Schedule</button><button type="button" data-view="full" aria-pressed="${state.tab==='full'}">Full Schedule</button><button type="button" data-view="available" aria-pressed="${state.tab==='available'}">Available ${offers().length || ''}</button></nav><div class="es-week"><strong>${format(model.days[0],{month:'short',day:'numeric'})} – ${format(model.days[6],{month:'short',day:'numeric'})}</strong><div><button type="button" data-week="-1" aria-label="Previous week">‹</button><button type="button" data-current>This week</button><button type="button" data-week="1" aria-label="Next week">›</button></div></div></div>`;
    if (stale) html += '<div class="es-notice" role="status">Couldn’t refresh. This saved schedule may have changed. <button type="button" data-retry>Try again</button></div>';
    if (!isMatrix) html += `<nav class="es-days" aria-label="Choose a schedule day">${model.days.map(d=>`<button type="button" data-day="${d}" aria-pressed="${state.day===d || state.tab==='full' && state.day===null && d===model.days[0]}" ${d===model.today?'aria-current="date"':''} class="${personal.some(s=>s.date===d)?'has-shift':''}" aria-label="${escape(fullDay(d))}${personal.some(s=>s.date===d)?', you have a shift':''}"><span>${shortDay(d)}</span><strong>${date(d).getDate()}</strong><i aria-hidden="true"></i></button>`).join('')}</nav>`;
    const total = number(personal.reduce((n,s)=>n+s.paidHours,0));
    if (state.tab === 'my') {
      const nextHtml = next ? `<button type="button" class="es-next" data-view-shift="${next.id}"><div><small>Your next shift${model.days[6]<model.today?' this week':''}</small><strong>${shortDay(next.date)}, ${desktop?format(next.date,{month:'short',day:'numeric'})+'<br>':''}${time(next.start)} – ${time(next.end)}</strong><p>${escape(store(next)?.name)}${desktop?'':' · '+fullDay(next.date)}</p></div><span aria-hidden="true">↗</span></button>` : '';
      if (!desktop && state.day===null) html += nextHtml;
      html += `<div class="es-sectionhead"><h2>${state.day?fullDay(state.day):'Your week'}</h2><span class="es-hours"><strong>${total}</strong> hrs this week</span></div>`;
      if (state.day) html += '<button type="button" class="es-requestlink" data-full-personal>Show full week</button>';
      if (!personal.length) html += empty('No shifts scheduled', 'You have no published shifts this week. View Full Schedule or check incoming offers.');
      else {
        const agenda = model.days.filter(d=>!state.day || state.day===d).map(d=>{
          const shifts = personal.filter(s=>s.date===d);
          if (desktop) return shifts.map(s=>`<button type="button" class="es-personal-row" data-view-shift="${s.id}"><strong>${shortDay(d)} ${date(d).getDate()}</strong><span>${badge(s)}${labels(s)}</span><span class="es-time">${time(s.start)} – ${time(s.end)}<small>${number(s.paidHours)} paid hrs</small></span></button>`).join('') || (state.day?'<div class="es-off">Day off</div>':'');
          return `<div class="es-personal-row ${shifts.length?'':'offday'}"><strong>${shortDay(d)} ${date(d).getDate()}</strong><div>${shifts.map(s=>card(s)).join('') || '<div class="es-off">Day off</div>'}</div></div>`;
        }).join('');
        html += desktop?`<div class="es-personal"><div>${nextHtml}<button type="button" class="es-requestlink" data-show-requests>Shift requests</button></div><div class="es-agenda">${agenda}</div></div>`:`<div class="es-agenda">${agenda}</div>`;
      }
    }
    if (state.tab === 'full') {
      const chosen = state.day || model.days[0];
      html += `<div class="es-sectionhead"><h2>${state.mode==='day'?fullDay(chosen):'All locations'}</h2><div class="es-modes" aria-label="Schedule layout"><button type="button" data-mode="day" aria-pressed="${state.mode==='day'}">Day</button><button type="button" data-mode="week" aria-pressed="${state.mode==='week'}">Week</button></div></div>`;
      if (!model.shifts.length) html += empty('Schedule coming soon', 'No published shifts this week.');
      else if (isMatrix) html += `<div class="es-tablewrap"><table class="es-matrix" aria-label="Published company schedule by location and day"><thead><tr><th scope="col">Location</th>${model.days.map(d=>`<th scope="col"><button type="button" class="es-date" data-day="${d}" ${d===model.today?'aria-current="date"':''} aria-label="View ${escape(fullDay(d))}">${shortDay(d)} <strong>${date(d).getDate()}</strong></button></th>`).join('')}</tr></thead><tbody>${model.stores.map(s=>`<tr><th scope="row" data-store-tone="${escape(s.tone)}"><span class="es-location">${escape(s.name)}</span></th>${model.days.map(d=>`<td>${notices(s.id,d)}${model.shifts.filter(x=>x.storeId===s.id && x.date===d).map(assignment).join('') || '<span class="es-sub">No published shift</span>'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      else if (state.mode==='day') html += `<div class="es-daygrid">${model.shifts.filter(s=>s.date===chosen).map(s=>card(s,true)).join('') || '<p class="es-sub">No published shifts for this day.</p>'}</div>`;
      else html += `<div class="es-week-agenda">${model.days.map(d=>`<section><h3>${fullDay(d)}</h3><div>${model.shifts.filter(s=>s.date===d).map(s=>card(s,true)).join('') || '<p class="es-sub">No published shifts</p>'}</div></section>`).join('')}</div>`;
    }
    if (state.tab === 'available') {
      const incoming = offers().filter(r=>!state.day || r.dataset.offerDate===state.day);
      html += '<div class="es-sectionhead"><h2>Offered to you</h2></div><p class="es-sub">Requests coworkers have sent directly to you.</p>';
      html += incoming.map(r=>`<button type="button" class="es-card" data-view-request="${escape(r.dataset.requestId)}"><strong class="es-name">${escape(r.querySelector('h3')?.textContent)}</strong><span class="es-sub">${escape(r.querySelector('p')?.textContent)}</span><span class="es-you">View request</span></button>`).join('') || empty('You’re all caught up','No incoming shift offers for this selection.');
    }
    html += '<p class="es-fresh">Published schedule · Pacific time</p>';
    html += `<nav class="es-mobile-links" aria-label="Schedule tools">${model.timeOff?'<a href="/v2/scheduling/my-time-off">Time off</a>':''}<button type="button" data-show-requests>Shift requests</button></nav>`;
    mount.innerHTML = html;
    if(focusKey){const target=[...mount.querySelectorAll('button')].find(b=>b.getAttribute(focusKey)===focusValue && b.hasAttribute(focusKey));target?.focus();}
  }
  function openDetails(id, button) {
    const shift = model.shifts.find(s=>s.id===Number(id));
    if (!shift) return;
    selected = shift; opener = button;
    const other = model.shifts.filter(s=>s.id!==shift.id && s.date===shift.date && s.storeId===shift.storeId && s.employeeId!==null).map(s=>s.name);
    const fresh = !stale && Date.now()-loadedAt<30000;
    const action = fresh && shift.canRequest && !pending(shift) ? '<div class="es-actions"><button type="button" data-start-action="offer">Offer shift</button><button type="button" data-start-action="swap">Request swap</button></div>' : '';
    detail.querySelector('[data-detail-content]').innerHTML = `${badge(shift)}<p class="es-detail-time">${time(shift.start)} – ${time(shift.end)}</p><p class="es-detail-date">${fullDay(shift.date)}</p><div class="es-detail-row"><span>Scheduled employee</span><strong>${escape(shift.name)}</strong></div><div class="es-detail-row"><span>Scheduled paid hours</span><strong>${number(shift.paidHours)} hrs</strong></div><div class="es-detail-row"><span>Unpaid break</span><strong>${shift.breakMinutes} min</strong></div>${shift.coverage?'<div class="es-detail-row"><span>Covering</span><strong>'+escape(shift.covering)+'</strong></div><button type="button" class="es-requestlink" data-original="'+shift.sourceId+'">View original shift</button>':''}${shift.exception?'<p class="es-notice">'+escape(shift.statuses.join(', ') || 'Coverage assigned')+' · '+number(shift.paidHours)+' effective paid hrs</p>':''}<div class="es-detail-row"><span>Also working here</span><strong>${escape(other.join(', ') || 'No other assignments')}</strong></div>${shift.lead?'<p class="es-sub">Lead of the Day</p>':''}${shift.opening||shift.closing?'<p class="es-sub">'+(shift.opening?'Opening':'Closing')+'</p>':''}${action}${!fresh?'<p class="es-notice">Refresh this schedule before sending a shift request.</p><button type="button" class="es-requestlink" data-refresh-detail>Refresh shift</button>':''}${pending(shift)?'<p class="es-notice">A request for this shift is pending.</p>':''}`;
    if (!detail.open) detail.showModal();
  }
  function showRequests(button) {
    opener = button || document.querySelector('.v2-nav a[href$="#shift-requests"]');
    if (!requests.open) requests.showModal();
  }
  function trimRequests() {rows().sort((a,b)=>Date.parse(b.dataset.createdAt || b.dataset.updatedAt)-Date.parse(a.dataset.createdAt || a.dataset.updatedAt)).slice(100).forEach(r=>r.remove());}
  function mergeRequests(doc) {
    const list = root.querySelector('[data-request-list]');
    for (const row of doc.querySelectorAll('[data-request-list] [data-request-id]')) {
      const old = list.querySelector(`[data-request-id="${row.dataset.requestId}"]`);
      if (old && Date.parse(old.dataset.updatedAt)>Date.parse(row.dataset.updatedAt)) continue;
      if (old && /^(COMPLETED|CANCELLED|DECLINED|REJECTED):/.test(old.dataset.requestState) && row.dataset.requestState.startsWith('PENDING_')) continue;
      if (old) old.replaceWith(document.importNode(row,true));
      else list.prepend(document.importNode(row,true));
    }
    trimRequests();
    if (list.querySelector('[data-request-id]')) list.querySelector('[data-no-requests]')?.remove();
  }
  async function loadWeek(start, force=false) {
    if (root.querySelector('[data-employee-action-dialog]').open || requests.open) return;
    const token = ++generation;
    controller?.abort();
    const cached = cache.get(start);
    if (cached && !force) {
      model=cached.model; fallback.innerHTML=cached.html; loadedAt=cached.loadedAt;
      render();
      if (Date.now()-loadedAt<30000) { loading=false; remember(); updateUrl(); return; }
    }
    controller = new AbortController(); loading=true;
    const url = new URL(location.href); url.pathname='/v2/scheduling/my-schedule'; url.search='?start='+encodeURIComponent(start); url.hash='';
    try {
      const response = await fetch(url,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (response.status===401 || response.status===403 || response.status===409 || response.redirected) { cache.clear(); location.assign(response.redirected?response.url:'/login'); return; }
      if (!response.ok) throw new Error('Schedule could not be refreshed.');
      const doc = new DOMParser().parseFromString(await response.text(),'text/html');
      const next = JSON.parse(doc.querySelector('[data-employee-week]').textContent);
      if (token!==generation) return;
      if (next.employeeId!==identity) { cache.clear(); location.reload(); return; }
      model=next; fallback.innerHTML=doc.querySelector('[data-employee-fallback]').innerHTML;
      mergeRequests(doc); loadedAt=Date.now(); stale=false; remember(); updateUrl(); render();
      message.textContent='Schedule updated.';
    } catch (e) {
      if (e.name!=='AbortError' && token===generation) { stale=true; render(); message.textContent='Could not load the selected week. Your saved week is still shown.'; }
    } finally { if (token===generation) loading=false; }
  }
  function updateUrl() { const url=new URL(location.href); url.searchParams.set('start',model.weekStart); history.replaceState(null,'',url); }
  root.addEventListener('click', event => {
    const b=event.target.closest('button'); if(!b)return;
    if (b.dataset.view) { state.tab=b.dataset.view; render(); mount.querySelector(`[data-view="${state.tab}"]`)?.focus(); }
    if (b.dataset.day) { state.day=b.dataset.day; if(state.tab==='full')state.mode='day'; render(); }
    if (b.dataset.mode) { state.mode=b.dataset.mode; render(); }
    if (b.hasAttribute('data-full-personal')) { state.day=null; render(); }
    if (b.dataset.viewShift) openDetails(b.dataset.viewShift,b);
    if (b.hasAttribute('data-original')) openDetails(b.dataset.original,opener);
    if (b.dataset.week || b.hasAttribute('data-current')) {
      state.day=null;
      const target=date(b.hasAttribute('data-current')?model.today:model.weekStart);
      if (b.dataset.week) target.setDate(target.getDate()+Number(b.dataset.week)*7);
      else target.setDate(target.getDate()-target.getDay());
      const start=[target.getFullYear(),String(target.getMonth()+1).padStart(2,'0'),String(target.getDate()).padStart(2,'0')].join('-');
      void loadWeek(start);
    }
    if (b.hasAttribute('data-retry')) void loadWeek(model.weekStart,true);
    if (b.hasAttribute('data-refresh-detail')) { const id=selected.id;detail.close();void loadWeek(model.weekStart,true).then(()=>openDetails(id,opener)); }
    if (b.hasAttribute('data-show-requests') || b.dataset.viewRequest) showRequests(b);
    if (b.hasAttribute('data-detail-close')) detail.close();
    if (b.hasAttribute('data-requests-close')) requests.close();
    if (b.dataset.startAction && selected) {
      const article=fallback.querySelector(`[data-shift-id="${selected.id}"]`);
      const action=article?.querySelector(b.dataset.startAction==='swap'?'[data-swap-options]':'[data-own-actions] details');
      if (action) { detail.close(); action.open=true; }
    }
  });
  for (const dialog of [detail,requests]) dialog.addEventListener('close',()=>{ const old=opener;const target=old?.isConnected?old:mount.querySelector('[data-view-shift="'+selected?.id+'"]')||mount.querySelector('[data-view="'+state.tab+'"]');target?.focus(); });
  root.addEventListener('employee-schedule:changed', event=>{
    if(requests.open)requests.close();
    trimRequests();
    cache.clear();
    render();
    if (event.detail?.status==='COMPLETED' || event.detail?.invalidated || stale) setTimeout(()=>void loadWeek(model.weekStart,true),0);
  });
  root.addEventListener('employee-schedule:uncertain',()=>{cache.clear();stale=true;render();});
  const refresh=()=>{if(!document.hidden && !loading && !root.querySelector('dialog[open]') && Date.now()-loadedAt>=30000)void loadWeek(model.weekStart,true);};
  window.addEventListener('pageshow',event=>{if(event.persisted){cache.clear();loadedAt=0;stale=true;render();refresh();}});
  window.addEventListener('focus',refresh);document.addEventListener('visibilitychange',refresh);setInterval(refresh,30000);
  addEventListener('hashchange',()=>{if(location.hash==='#shift-requests')showRequests();});
  root.querySelector('[data-requests-content]').append(historySection);
  historySection.classList.remove('v2-card');
  historySection.querySelector('h2').hidden=true;
  [...historySection.querySelectorAll(':scope > p')].slice(0,2).forEach(p=>p.hidden=true);
  root.classList.add('es-enhanced');
  fallback.hidden=true; mount.hidden=false;
  root.querySelector('[data-legacy-links]')?.setAttribute('hidden','');
  remember(); render();
  if(location.hash==='#shift-requests')showRequests();
})();
