import {drawReceivingDashboard} from './receiving_dashboard.ts';
export {};

type Store = {id: number; name: string};
type Row = {id: number; name: string; sku: string; variant: string; ordered: number; previous: number;
  outstanding: number; closed: number; requested: Record<number, number>; received: Record<number, number>};
type Receipt = {id: string | number; kind: string; at: string; received: number; status: string; retry: boolean;
  note?: string; actor_id?: number; children: {line_id: number; store_id: number; quantity: number; closed: number; posting?: string}[]};
type Order = {id: number; vendor: string; rows: Row[]; stores: Store[]; version: number; history: Receipt[];
  ordered: number; previous: number; outstanding: number; closed: number; status: string; can_receive: boolean};
type Decision = {request_key: string; version: number; cells?: [number, number, number][]};
type Draft = {version: number; cells: Record<string, string>; pending?: Decision};

const root = document.querySelector<HTMLElement>('#receiving-sheet')!;
const apiRoot = '/api/purchasing-v4/receiving/orders';
const $ = <T extends HTMLElement = HTMLElement>(selector: string) => root.querySelector<T>(selector)!;
const escape = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const number = (value: number) => value.toLocaleString('en-US');
const signed = (value: number) => (value > 0 ? '+' : '') + number(value);
const valid = (value: string) => /^\d+$/.test(value) && Number(value) <= 2147483647;
const cell = (line: number, store: number) => `${line}:${store}`;
const previewOrders = new URLSearchParams(location.search);
const orderId = previewOrders.get('po');
const draftKey = `v4-receiving-sheet:${root.dataset.owner}:${orderId}`;
const widthKey = `v4-receiving-widths:${root.dataset.owner}`;
const orderUrl = apiRoot + '/' + encodeURIComponent(orderId ?? '');
let order: Order, draft: Draft, savedDraft: string | null, busy = false, storageError = false;
let sortKey = 'name', direction = 1, visible: Row[] = [];
const expanded = new Set<number>();
let widths: Record<string, number> = {};

async function request(path: string, body?: unknown) {
  const csrf = document.cookie.split('; ').find(c => c.startsWith('csrf_token='))?.slice(11) ?? '';
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: {'Content-Type':'application/json', 'X-CSRF-Token':decodeURIComponent(csrf)}, body: JSON.stringify(body),
    signal: AbortSignal.timeout(120_000)});
  if (response.status === 401 || response.redirected) {
    location.assign('/login');
    throw Error('Sign in to continue receiving.');
  }
  const data = await response.json();
  if (!response.ok) throw Object.assign(Error(typeof data.detail === 'string' ? data.detail : 'Review the shipment quantities.'), {status:response.status});
  return data;
}

function notice(message: string, error = false) {
  const area = $('#receiving-notice');
  area.textContent = message;
  area.classList.toggle('receiving-error', error);
}

function persist() {
  try {
    if (localStorage.getItem(draftKey) !== savedDraft) throw Error('Another tab changed this draft. Reload to see its saved quantities.');
    const next = JSON.stringify(draft);
    localStorage.setItem(draftKey, next);
    savedDraft = next;
    return true;
  } catch (error) {
    storageError = true;
    notice((error as Error).message || 'The browser could not save this draft.', true);
    return false;
  }
}

function quantities(row: Row) {
  const values = order.stores.map(store => draft.cells[cell(row.id,store.id)]).filter(v => v !== undefined);
  const invalid = values.some(v => !valid(v));
  const total = values.reduce((sum,v) => sum + (valid(v) ? Number(v) : 0), 0);
  return {total, entered:values.length > 0, invalid, difference:total - row.outstanding};
}

function valueFor(row: Row, key: string): string | number | undefined {
  const [kind,id] = key.split(':');
  if (kind === 'requested') return row.requested[Number(id)] ?? 0;
  if (kind === 'received') {
    const value = draft.cells[cell(row.id,Number(id))];
    return value !== undefined && valid(value) ? Number(value) : undefined;
  }
  if (key === 'total' || key === 'difference') {
    const q = quantities(row);
    if (q.entered && !q.invalid) return key === 'total' ? q.total : q.difference;
    return undefined;
  }
  if (key === 'ordered') return row.ordered;
  return row.name;
}

function differenceClass(value: number) {
  return value > 0 ? 'receiving-over' : value < 0 ? 'receiving-short' : 'receiving-match';
}

function updateRow(row: Row) {
    const q = quantities(row);
    const total = $<HTMLElement>(`[data-total="${row.id}"]`);
    const difference = $<HTMLElement>(`[data-difference="${row.id}"]`);
    total.textContent = q.entered && !q.invalid ? number(q.total) : '—';
    difference.textContent = q.entered && !q.invalid ? signed(q.difference) : '—';
    difference.className = q.entered && !q.invalid ? differenceClass(q.difference) : '';
    if (q.entered && !q.invalid) {
      difference.title = `${number(q.total)} this shipment minus ${number(row.outstanding)} outstanding before this shipment`;
    } else difference.removeAttribute('title');
}

function updateTotals() {
  const summary = order.rows.map(quantities);
  const invalid = summary.some(q => q.invalid);
  const entered = summary.some(q => q.entered);
  $('#receiving-draft-total').textContent = invalid ? 'Check quantities' : entered
    ? `${number(summary.reduce((sum,q) => sum + q.total,0))} units this shipment` : 'Enter received units';
  $<HTMLButtonElement>('#send-to-stores').disabled = busy || storageError ||
    (!draft.pending && (!order.can_receive || draft.version !== order.version || invalid || !entered));
  $<HTMLButtonElement>('#close-unfulfilled').disabled = busy || storageError || !!draft.pending ||
    !order.can_receive || draft.version !== order.version;
}

function rowDetails(row: Row) {
  return `<div class="receiving-row-details"><span><b>SKU</b> ${escape(row.sku || '—')}</span>
    <span><b>Variation</b> ${escape(row.variant || '—')}</span>
    <span><b>Previously received</b> ${number(row.previous)}</span>
    <span><b>Outstanding before shipment</b> ${number(row.outstanding)}</span>
    <span><b>Cumulative difference</b> ${signed(row.previous-row.ordered)}</span>
    ${row.closed ? `<span><b>Closed unfulfilled</b> ${number(row.closed)}</span>` : ''}
    <span><b>Previously assigned by store</b> ${order.stores.map(s => `${escape(s.name)} ${number(row.received[s.id] ?? 0)}`).join(' · ')}</span></div>`;
}

function drawRows() {
  const search = $<HTMLInputElement>('#receive-search').value.toLowerCase().trim();
  const filter = $<HTMLSelectElement>('#receive-filter').value;
  visible = order.rows.filter(row => `${row.name} ${row.sku} ${row.variant}`.toLowerCase().includes(search)
    && (filter !== 'outstanding' || row.outstanding > 0) && (filter !== 'entered' || quantities(row).entered))
    .sort((a,b) => {
      const left = valueFor(a,sortKey), right = valueFor(b,sortKey);
      if (left === undefined || right === undefined) return (left === undefined ? 1 : 0) - (right === undefined ? 1 : 0) || a.id-b.id;
      return direction * (typeof left === 'string' ? left.localeCompare(String(right),undefined,{numeric:true})
        : left === right ? 0 : left < Number(right) ? -1 : 1) || a.id-b.id;
    });
  const locked = busy || storageError || !!draft.pending || !order.can_receive || draft.version !== order.version;
  $('#receive-rows').innerHTML = visible.map(row => `<tr>
    <th scope="row"><button type="button" class="receiving-expand" data-expand="${row.id}" aria-expanded="${expanded.has(row.id)}">
      <span aria-hidden="true">${expanded.has(row.id) ? '▾' : '▸'}</span> ${escape(row.name)}</button>
      <small>${escape(row.variant)}${row.sku ? ' · '+escape(row.sku) : ''}</small></th>
    ${order.stores.map(store => {
      const value = draft.cells[cell(row.id,store.id)] ?? '';
      return `<td class="receiving-requested">${number(row.requested[store.id] ?? 0)}</td><td class="receiving-editable">
        <input type="text" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="—"
          data-row="${row.id}" data-store="${store.id}" value="${escape(value)}" ${locked ? 'readonly' : ''}
          aria-label="${escape(store.name)} received · ${escape(row.name)}" aria-invalid="${value!=='' && !valid(value)}"></td>`;
    }).join('')}
    <td>${number(row.ordered)}</td><td data-total="${row.id}"></td><td data-difference="${row.id}"></td>
    </tr>${expanded.has(row.id) ? `<tr class="receiving-detail"><td colspan="12">${rowDetails(row)}</td></tr>` : ''}`).join('')
    || '<tr><td colspan="12" class="receiving-empty">No products match these filters.</td></tr>';
  for (const row of visible) updateRow(row);
  updateTotals();
  $('#visible-count').textContent = `${number(visible.length)} of ${number(order.rows.length)} products`;
}

function headers() {
  const column = (key: string, title: string, rows = 1) => `<th scope="col" rowspan="${rows}" data-column="${key}" aria-sort="${key === sortKey ? (direction === 1 ? 'ascending' : 'descending') : 'none'}">
    <button type="button" data-sort="${key}" aria-label="Sort by ${escape(title)}" title="${escape(title)}">${escape(title)}${key===sortKey ? (direction===1 ? ' ↑' : ' ↓') : ''}</button>
    <span class="receiving-resize" data-resize="${key}" role="separator" aria-label="Resize ${escape(title)}" aria-orientation="vertical" tabindex="0"></span></th>`;
  const keys = ['name', ...order.stores.flatMap(s => [`requested:${s.id}`,`received:${s.id}`]), 'ordered','total','difference'];
  $('#receive-cols').innerHTML = keys.map(key => `<col data-width="${key}" style="width:${widths[key] ?? (key==='name' ? 244 : key.includes(':') ? 80 : 108)}px">`).join('');
  $('#receive-headers').innerHTML = `<tr>${column('name','Product',2)}${order.stores.map(s => `<th colspan="2" scope="colgroup" title="${escape(s.name)}">${escape(s.name)}</th>`).join('')}
    ${column('ordered','Total Ordered',2)}${column('total','Total Received',2)}${column('difference','Difference',2)}</tr>
    <tr>${order.stores.map(s => column('requested:'+s.id,'Requested')+column('received:'+s.id,'Received')).join('')}</tr>`;
  sizeTable();
}

function sizeTable() {
  const columns = root.querySelectorAll<HTMLTableColElement>('#receive-cols col');
  $('.receiving-grid').style.width = Array.from(columns).reduce((total,col) => total + parseFloat(col.style.width),0)+'px';
}

function resize(key: string, width: number) {
  const minimum = key.startsWith('requested:') || key.startsWith('received:') ? 32 : 64;
  widths[key] = Math.max(minimum,Math.round(width));
  $<HTMLTableColElement>(`[data-width="${key}"]`).style.width = widths[key]+'px';
  sizeTable();
}

function retainWidths() {
  try { localStorage.setItem(widthKey,JSON.stringify(widths)); } catch { /* Resizing remains available for this session. */ }
}

function editKey(e: KeyboardEvent) {
  const input = e.target as HTMLInputElement;
  if (!input.dataset.store || !['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter','Tab'].includes(e.key)) return;
  const row = visible.findIndex(r => r.id === Number(input.dataset.row));
  const col = order.stores.findIndex(s => s.id === Number(input.dataset.store));
  const stride = order.stores.length;
  const offset = e.key === 'ArrowUp' ? -stride : e.key === 'ArrowDown' || e.key === 'Enter' ? (e.shiftKey ? -stride : stride)
    : e.key === 'ArrowLeft' || (e.key === 'Tab' && e.shiftKey) ? -1 : 1;
  const next = row*stride+col+offset;
  if (next < 0 || next >= visible.length*stride) return;
  e.preventDefault();
  const field = $<HTMLInputElement>(`[data-row="${visible[Math.floor(next/stride)].id}"][data-store="${order.stores[next%stride].id}"]`);
  field.focus();
  field.select();
}

function receiptHistory() {
  const lineNames = new Map(order.rows.map(row => [row.id,row.name]));
  const storeNames = new Map(order.stores.map(store => [store.id,store.name]));
  $('#receiving-history').innerHTML = order.history.map(receipt => `<details class="receiving-history-item">
    <summary><time>${escape(new Date(receipt.at).toLocaleString())}</time> · ${escape(receipt.kind==='CLOSE' || receipt.kind==='CLOSURE' ? 'Closed unfulfilled' : receipt.kind==='DELIVERY' || receipt.kind==='RECEIPT' ? 'Shipment' : receipt.kind)}
      · ${number(receipt.received)} units · ${escape(receipt.status)}</summary>
    ${receipt.note ? `<p>${escape(receipt.note)}</p>` : ''}
    ${receipt.children.length ? `<ul>${receipt.children.map(entry => `<li>${escape(lineNames.get(entry.line_id) ?? 'PO line '+entry.line_id)}
      · ${escape(storeNames.get(entry.store_id) ?? 'Store '+entry.store_id)}
      · ${entry.closed ? number(entry.closed)+' closed' : number(entry.quantity)+' received'}${entry.posting ? ' · '+escape(entry.posting) : ''}</li>`).join('')}</ul>` : ''}
    ${receipt.retry ? `<button type="button" data-retry="${escape(receipt.id)}">Retry posting</button>` : ''}</details>`).join('')
    || '<p>No shipments recorded.</p>';
}

function staleDraft() {
  const stale = !draft.pending && draft.version !== order.version;
  $('#resume-receiving-draft').hidden = !stale;
  if (stale) notice('This order changed since these entries were saved. Review the new balance before resuming the saved count.',true);
}

async function refresh() {
  order = await request(orderUrl);
  $('#receiving-state').textContent = order.status;
  $('#receiving-order-totals').textContent = `${number(order.ordered)} ordered · ${number(order.previous)} previously received · ${number(order.outstanding)} outstanding${order.closed ? ' · '+number(order.closed)+' closed unfulfilled' : ''}`;
  receiptHistory();
}

async function decide(kind: 'shipments' | 'close') {
  if (busy) return;
  const old = draft.pending;
  if (!old) {
    const decision: Decision = {request_key:crypto.randomUUID(),version:draft.version};
    if (kind==='shipments') {
      const entered = Object.values(draft.cells);
      if (!entered.length || entered.some(value => !valid(value))) return;
      decision.cells = order.rows.flatMap(row => order.stores.map((store): [number,number,number] =>
        [row.id,store.id,Number(draft.cells[cell(row.id,store.id)] ?? 0)]));
    }
    draft.pending = decision;
    if (!persist()) { delete draft.pending; updateTotals(); return; }
  }
  busy = true;
  drawRows();
  notice(kind==='shipments' ? 'Sending shipment to stores…' : 'Closing outstanding quantities…');
  let acknowledged = false;
  try {
    const pending = draft.pending!;
    await request(orderUrl+(pending.cells ? '/shipments' : '/close'),pending);
    acknowledged = true;
    await refresh();
    draft = {version:order.version,cells:{}};
    persist();
    notice(pending.cells ? 'Shipment recorded. Store posting status is in receiving history.' : 'Order closed with unfulfilled quantities.');
  } catch (error) {
    const status = (error as Error & {status?: number}).status;
    if (!acknowledged && (status === 409 || status === 422)) {
      delete draft.pending;
      persist();
      try { await refresh(); } catch { /* Keep the saved quantities available. */ }
      if (!Object.keys(draft.cells).length) { draft.version=order.version; persist(); }
      notice((error as Error).message,true);
    } else {
      notice('Submission outcome is unconfirmed. Retry the saved action to recover its original shipment.',true);
    }
  } finally {
    busy = false;
    updateAction();
    drawRows();
    staleDraft();
  }
}

function updateAction() {
  $('#send-to-stores').textContent = draft.pending ? (draft.pending.cells ? 'Retry Send to Stores' : 'Retry closure') : 'Send to Stores';
}

async function drawOrder() {
  order = await request(orderUrl);
  try {
    savedDraft = localStorage.getItem(draftKey);
    draft = savedDraft ? JSON.parse(savedDraft) : {version:order.version,cells:{}};
    if (!Object.keys(draft.cells).length && !draft.pending) draft.version = order.version;
    widths = JSON.parse(localStorage.getItem(widthKey) || '{}');
  } catch {
    draft = {version:order.version,cells:{}};
    storageError = true;
  }
  $('#receiving-content').innerHTML = `<div class="receiving-heading"><div><p class="receiving-eyebrow">PURCHASE ORDER #${order.id}</p>
    <h1>${escape(order.vendor)}</h1><p id="receiving-order-totals"></p></div><strong id="receiving-state"></strong></div>
    <div class="receiving-toolbar"><label>Find product or SKU <input id="receive-search" type="search" placeholder="Search worksheet"></label>
    <label>Show <select id="receive-filter"><option value="all">All products</option><option value="outstanding">Outstanding products</option><option value="entered">Entered this shipment</option></select></label>
    <span id="visible-count"></span></div>
    <p class="receiving-instructions">Enter individual units. Blank cells count as zero when you send. Difference compares this shipment with the outstanding quantity before it.</p>
    <div class="receiving-scroll"><table class="receiving-grid"><colgroup id="receive-cols"></colgroup>
      <thead id="receive-headers"></thead><tbody id="receive-rows"></tbody></table></div>
    <div class="receiving-footer"><span id="receiving-draft-total"></span>
      <button type="button" id="resume-receiving-draft" hidden>Resume saved count</button>
      <button type="button" id="close-unfulfilled">Close as Unfulfilled</button>
      <button type="button" class="primary" id="send-to-stores">Send to Stores</button></div>
    <p id="receiving-notice" role="status" aria-live="polite"></p>
    <section class="receiving-history"><h2>Receiving history</h2><div id="receiving-history"></div></section>`;
  $('#receiving-state').textContent = order.status;
  $('#receiving-order-totals').textContent = `${number(order.ordered)} ordered · ${number(order.previous)} previously received · ${number(order.outstanding)} outstanding${order.closed ? ' · '+number(order.closed)+' closed unfulfilled' : ''}`;
  headers();
  drawRows();
  receiptHistory();
  staleDraft();
  updateAction();
  if (draft.pending) notice('A saved submission is ready to recover using its original shipment identity.');
  if (storageError) notice('Browser storage is unavailable. Restore access before editing or sending.',true);
  $('#receive-search').addEventListener('input',drawRows);
  $('#receive-filter').addEventListener('change',drawRows);
  $('#receive-rows').addEventListener('input',e => {
    const input = e.target as HTMLInputElement;
    if (!input.dataset.store || input.readOnly) return;
    const key = cell(Number(input.dataset.row),Number(input.dataset.store));
    if (input.value === '') delete draft.cells[key];
    else draft.cells[key] = input.value;
    input.setAttribute('aria-invalid',String(input.value!=='' && !valid(input.value)));
    if (persist()) notice('Draft saved in this browser.');
    const row = order.rows.find(row => row.id===Number(input.dataset.row));
    if (row) updateRow(row);
    updateTotals();
  });
  $('#receive-rows').addEventListener('keydown',editKey);
  $('#receive-rows').addEventListener('click',e => {
    const control = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-expand]');
    if (!control) return;
    const id = Number(control.dataset.expand);
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    drawRows();
    $<HTMLButtonElement>(`[data-expand="${id}"]`).focus();
  });
  $('#receive-headers').addEventListener('click',e => {
    const control = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-sort]');
    if (!control) return;
    const key = control.dataset.sort!;
    direction = key===sortKey ? -direction : 1;
    sortKey = key;
    headers();
    drawRows();
    $<HTMLButtonElement>(`[data-sort="${key}"]`).focus();
  });
  $('#receive-headers').addEventListener('pointerdown',e => {
    const handle = (e.target as HTMLElement).closest<HTMLElement>('[data-resize]');
    if (!handle) return;
    e.preventDefault();
    const key = handle.dataset.resize!;
    const start = e.clientX, width = handle.parentElement!.getBoundingClientRect().width;
    handle.setPointerCapture(e.pointerId);
    handle.onpointermove = ev => resize(key,width+ev.clientX-start);
    handle.onlostpointercapture = () => {handle.onpointermove=null;retainWidths();};
  });
  $('#receive-headers').addEventListener('keydown',e => {
    const handle = e.target as HTMLElement;
    if (!handle.dataset.resize || !['ArrowLeft','ArrowRight'].includes(e.key)) return;
    e.preventDefault();
    resize(handle.dataset.resize,handle.parentElement!.getBoundingClientRect().width+(e.key==='ArrowLeft' ? -8 : 8));
    retainWidths();
  });
  $('#send-to-stores').onclick = () => void decide('shipments');
  $('#close-unfulfilled').onclick = () => void decide('close');
  $('#resume-receiving-draft').onclick = () => {
    draft.version = order.version;
    if (persist()) notice('Saved count resumed against the current balance.');
    staleDraft();
    drawRows();
  };
  $('#receiving-history').addEventListener('click',async e => {
    const control = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-retry]');
    if (!control || busy) return;
    busy = true;
    control.disabled = true;
    drawRows();
    try {
      await request(orderUrl+'/shipments/'+encodeURIComponent(control.dataset.retry!)+'/retry',{});
      await refresh();
      notice('Posting status refreshed.');
    } catch (error) { notice((error as Error).message,true); }
    finally { busy=false;control.disabled=false;drawRows(); }
  });
}

async function drawOrders() {
  await drawReceivingDashboard(root, apiRoot);
}

root.innerHTML = `<header class="top"><a class="brand" href="/purchasing-v4">ERUPTED<span>Purchasing V4</span></a>
  <div class="top-actions"><a href="/purchasing-v4">Ordering worksheet</a><a href="/purchasing-v4/receiving">Receiving orders</a></div>
  ${root.dataset.mode === 'preview' ? '<span class="environment">ISOLATED PREVIEW · SIMULATED INVENTORY</span>' : ''}</header>
  <div id="receiving-content" class="receiving-content"><p>Loading receiving…</p></div><p id="receiving-page-error" role="alert"></p>`;
void (orderId ? drawOrder() : drawOrders()).catch(error => { $('#receiving-page-error').textContent=error.message; });
