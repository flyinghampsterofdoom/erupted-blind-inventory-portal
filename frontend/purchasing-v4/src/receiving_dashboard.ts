type PurchaseOrder = {
  id: number; vendor_id: number; vendor: string; order_date: string | null; last_received_at: string | null;
  receiving_status: string; lifecycle_status: string; partial: boolean | null; ordered_units: number | null;
  received_units: number | null; outstanding_units: number | null; closed_unfulfilled_units: number;
  missing_units?: number; original_order_value: string | null; received_value: string | null; is_closed: boolean;
};
type Summary = {open_orders: number; partial_orders: number; outstanding_units: number;
  outstanding_merchandise_cost: string | null; unknown_cost_orders: number};
type Dashboard = {rows: PurchaseOrder[]; summary: Summary; vendors: {id: number; name: string}[];
  statuses: string[]; total_count: number; next_offset: number | null};
type Sort = 'id' | 'vendor' | 'order_date' | 'last_received_at' | 'receiving_status' | 'partial'
  | 'ordered_units' | 'received_units' | 'outstanding_units' | 'original_order_value' | 'received_value';

const escape = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, char =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]!));
const units = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString('en-US');

// Preserve the precision of saved merchandise costs without floating-point conversion.
function money(value: string | null | undefined) {
  if (value == null) return '—';
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return '—';
  const fraction = (match[3] ?? '').replace(/0+$/, '').padEnd(2, '0');
  return `${match[1]}$${BigInt(match[2]).toLocaleString('en-US')}.${fraction}`;
}

function date(value: string | null) {
  if (!value) return '—';
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const parsed = day ? new Date(Number(day[1]), Number(day[2])-1, Number(day[3])) : new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return `<time datetime="${escape(value)}" title="${escape(day ? parsed.toLocaleDateString() : parsed.toLocaleString())}">${escape(parsed.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}))}</time>`;
}

function badge(status: string) {
  const kind = status === 'Not Received' ? 'waiting' : status === 'Partially Received' ? 'partial'
    : status === 'Fully Received' ? 'complete' : status === 'Closed — Unfulfilled' ? 'unfulfilled'
    : /cancel/i.test(status) ? 'canceled' : 'lifecycle';
  return `<span class="receiving-order-badge receiving-order-${kind}">${escape(status)}</span>`;
}

export async function drawReceivingDashboard(root: HTMLElement, apiRoot: string) {
  const $ = <T extends HTMLElement = HTMLElement>(selector: string) => root.querySelector<T>(selector)!;
  const content = $('#receiving-content');
  content.classList.add('receiving-dashboard');
  const columns: {key?: Sort; label: string; numeric?: boolean; width: number}[] = [
    {key:'id', label:'PO Number', width:94}, {key:'vendor', label:'Vendor', width:174},
    {key:'order_date', label:'Order Date', width:108}, {key:'last_received_at', label:'Last Received Date', width:112},
    {key:'receiving_status', label:'Receiving Status', width:178}, {key:'partial', label:'Partial', width:65},
    {key:'ordered_units', label:'Total Units Ordered', numeric:true, width:112},
    {key:'received_units', label:'Total Units Received', numeric:true, width:116},
    {key:'outstanding_units', label:'Outstanding Units', numeric:true, width:112},
    {key:'original_order_value', label:'Original Order Value', numeric:true, width:129},
    {key:'received_value', label:'Received Value', numeric:true, width:119}, {label:'Open Order', width:96},
  ];
  const columnNotes: Partial<Record<Sort, string>> = {
    original_order_value:'Saved original order value, including recorded order charges, discounts, and taxes.',
    received_value:'Cumulative received merchandise valued at saved historical unit costs; excludes order charges.',
    outstanding_units:'Remaining open receiving obligations. Closed unfulfilled units are shown in the row tooltip.',
  };
  const state = {q:'', vendor_id:'', status:'', include_closed:false, sort:'order_date' as Sort,
    direction:'desc' as 'asc' | 'desc', offset:0, limit:100};
  let data: Dashboard | undefined, generation = 0, loading = false;
  let abort: AbortController | undefined, searchTimer: number | undefined;
  content.innerHTML = `<div class="receiving-heading"><div><p class="receiving-eyebrow">PURCHASING V4</p>
    <h1>Receiving</h1><p>Track deliveries and open a purchase order to receive a shipment.</p></div></div>
    <section class="receiving-order-summary" aria-label="Receiving summary">
      <p class="receiving-order-summary-scope" title="Summary covers all open purchase orders, independent of table filters. Closed and canceled orders are excluded.">All open orders</p>
      <dl id="receiving-dashboard-summary">
        <div><dt>Open purchase orders</dt><dd data-summary="open_orders">—</dd></div>
        <div><dt>Partially received orders</dt><dd data-summary="partial_orders">—</dd></div>
        <div><dt>Total outstanding units</dt><dd data-summary="outstanding_units">—</dd></div>
        <div><dt>Total outstanding merchandise cost</dt><dd data-summary="outstanding_merchandise_cost">—</dd></div>
      </dl><p id="receiving-summary-cost-note" hidden></p>
    </section>
    <div class="receiving-order-toolbar" role="search" aria-label="Find purchase orders">
      <label class="receiving-order-search">Search<input id="receiving-order-search" type="search" placeholder="PO number or vendor" autocomplete="off"></label>
      <label>Vendor<select id="receiving-vendor-filter"><option value="">All vendors</option></select></label>
      <label>Receiving status<select id="receiving-status-filter"><option value="">All statuses</option></select></label>
      <label class="receiving-order-closed"><input id="receiving-show-closed" type="checkbox">Show closed orders</label>
    </div>
    <div class="receiving-order-feedback"><p id="receiving-order-notice" role="status" aria-live="polite"></p>
      <p id="receiving-order-error" role="alert" hidden></p><button id="receiving-order-retry" type="button" hidden>Retry</button></div>
    <div class="receiving-order-scroll" tabindex="0" role="region" aria-label="Purchase orders; scroll horizontally to view all columns">
      <table id="receiving-order-table" aria-label="Purchase orders for receiving" aria-busy="true">
        <colgroup>${columns.map(column => `<col style="width:${column.width}px">`).join('')}</colgroup>
        <thead id="receiving-order-headers"><tr>${columns.map(column => `<th scope="col"${column.key ? ` aria-sort="none"` : ''}${column.numeric ? ' class="receiving-order-number"' : ''}>
          ${column.key ? `<button type="button" data-order-sort="${column.key}" aria-label="Sort by ${column.label}"${columnNotes[column.key] ? ` title="${escape(columnNotes[column.key])}"` : ''}>${column.label}<span class="receiving-order-sort-arrow" aria-hidden="true">↕</span></button>` : column.label}</th>`).join('')}</tr></thead>
        <tbody id="receiving-order-rows"><tr><td colspan="12" class="receiving-order-empty">Loading purchase orders…</td></tr></tbody>
      </table>
    </div>
    <div class="receiving-order-pagination"><p id="receiving-order-count"></p><div>
      <button id="receiving-order-previous" type="button" disabled>Previous</button>
      <button id="receiving-order-next" type="button" disabled>Next</button></div></div>
    <p class="receiving-order-value-note">Original order value uses saved totals, including recorded charges, taxes, and discounts. Received value and outstanding merchandise cost use saved merchandise unit costs only.</p>`;

  function updateHeaders() {
    for (const button of root.querySelectorAll<HTMLButtonElement>('[data-order-sort]')) {
      const active = button.dataset.orderSort === state.sort;
      button.parentElement!.setAttribute('aria-sort', active ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      button.querySelector<HTMLElement>('.receiving-order-sort-arrow')!.textContent = active ? (state.direction === 'asc' ? '↑' : '↓') : '↕';
    }
  }

  function updatePagination() {
    $<HTMLButtonElement>('#receiving-order-previous').disabled = loading || state.offset === 0;
    $<HTMLButtonElement>('#receiving-order-next').disabled = loading || data?.next_offset == null;
  }

  function startLoading() {
    loading = true;
    $('#receiving-order-table').setAttribute('aria-busy', 'true');
    $('#receiving-order-rows').innerHTML = '<tr><td colspan="12" class="receiving-order-empty">Loading purchase orders…</td></tr>';
    $('#receiving-order-notice').textContent = 'Loading purchase orders…';
    $('#receiving-order-count').textContent = '';
    $('#receiving-order-error').hidden = true;
    $('#receiving-order-retry').hidden = true;
    updatePagination();
  }

  function render(result: Dashboard) {
    const summary = result.summary;
    for (const key of ['open_orders', 'partial_orders', 'outstanding_units'] as const)
      $(`[data-summary="${key}"]`).textContent = units(summary[key]);
    $('[data-summary="outstanding_merchandise_cost"]').textContent = money(summary.outstanding_merchandise_cost);
    const missingCost = $('#receiving-summary-cost-note');
    missingCost.hidden = !summary.unknown_cost_orders;
    missingCost.textContent = summary.unknown_cost_orders
      ? `Merchandise cost unavailable for ${units(summary.unknown_cost_orders)} open ${summary.unknown_cost_orders === 1 ? 'order' : 'orders'}.` : '';
    const vendors = $<HTMLSelectElement>('#receiving-vendor-filter');
    vendors.innerHTML = '<option value="">All vendors</option>' + result.vendors.map(vendor => `<option value="${vendor.id}">${escape(vendor.name)}</option>`).join('');
    vendors.value = state.vendor_id;
    const statuses = $<HTMLSelectElement>('#receiving-status-filter');
    statuses.innerHTML = '<option value="">All statuses</option>' + result.statuses.map(status => `<option value="${escape(status)}">${escape(status)}</option>`).join('');
    statuses.value = state.status;
    $('#receiving-order-rows').innerHTML = result.rows.map(row => {
      const closedBalance = row.closed_unfulfilled_units > 0
        ? `${units(row.closed_unfulfilled_units)} units closed unfulfilled. Outstanding units shows the remaining open receiving obligation.` : '';
      const lifecycle = ({RECEIVED_SPLIT_PENDING:'Store split pending', SENT_TO_STORES:'Sent to stores', COMPLETED:'Completed'} as Record<string, string>)[row.lifecycle_status];
      const lifecycleTitle = row.lifecycle_status ? `Lifecycle: ${row.lifecycle_status.replaceAll('_', ' ').toLowerCase()}.` : '';
      return `<tr data-po="${row.id}"${row.is_closed ? ' class="receiving-order-closed-row"' : ''}>
        <th scope="row"><a href="?po=${row.id}" aria-label="Open PO #${row.id} receiving worksheet">PO #${row.id}</a></th>
        <td class="receiving-order-vendor" title="${escape(row.vendor)}">${escape(row.vendor)}</td>
        <td>${date(row.order_date)}</td><td>${date(row.last_received_at)}</td>
        <td class="receiving-order-status" title="${escape([lifecycleTitle, closedBalance].filter(Boolean).join(' '))}">${badge(row.receiving_status)}${lifecycle ? `<small>${escape(lifecycle)}</small>` : ''}</td>
        <td>${row.partial == null ? '—' : row.partial ? 'Yes' : 'No'}</td>
        <td class="receiving-order-number">${units(row.ordered_units)}</td>
        <td class="receiving-order-number">${units(row.received_units)}</td>
        <td class="receiving-order-number"${closedBalance ? ` title="${escape(closedBalance)}"` : ''}>${units(row.outstanding_units)}</td>
        <td class="receiving-order-number"${row.original_order_value == null ? ' title="Saved original order value unavailable"' : ''}>${money(row.original_order_value)}</td>
        <td class="receiving-order-number"${row.received_value == null ? ' title="Historical cost or separate overreceipt valuation unavailable"' : ''}>${money(row.received_value)}</td>
        <td><a class="receiving-open-order" href="?po=${row.id}" aria-label="Open PO #${row.id} receiving worksheet">Open Order<span aria-hidden="true"> ↗</span></a></td></tr>`;
    }).join('') || '<tr><td colspan="12" class="receiving-order-empty">No purchase orders match these filters.</td></tr>';
    $('#receiving-order-count').textContent = result.total_count
      ? `${units(state.offset+1)}–${units(state.offset+result.rows.length)} of ${units(result.total_count)} purchase orders`
      : '0 purchase orders';
    $('#receiving-order-notice').textContent = result.rows.length ? 'Select a purchase order to open its receiving worksheet.' : '';
  }

  async function load() {
    const current = ++generation;
    abort?.abort();
    const controller = new AbortController();
    abort = controller;
    const timeout = window.setTimeout(() => controller.abort(), 120_000);
    startLoading();
    updateHeaders();
    const params = new URLSearchParams({q:state.q.trim(), vendor_id:state.vendor_id, status:state.status,
      include_closed:String(state.include_closed), sort:state.sort, direction:state.direction,
      offset:String(state.offset), limit:String(state.limit)});
    if (!state.vendor_id) params.delete('vendor_id');
    if (!state.status) params.delete('status');
    try {
      const response = await fetch(apiRoot+'?'+params, {credentials:'same-origin', signal:controller.signal});
      if (current !== generation) return;
      if (response.status === 401 || response.redirected) {
        location.assign('/login');
        throw Error('Sign in to continue receiving.');
      }
      const result = await response.json();
      if (current !== generation) return;
      if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Purchase orders could not be loaded.');
      data = result as Dashboard;
      render(data);
    } catch (error) {
      if (current !== generation) return;
      $('#receiving-order-rows').innerHTML = '<tr><td colspan="12" class="receiving-order-empty">Purchase orders could not be loaded.</td></tr>';
      $('#receiving-order-notice').textContent = '';
      const message = $('#receiving-order-error');
      message.textContent = controller.signal.aborted ? 'Loading took too long. Try again.' : (error as Error).message;
      message.hidden = false;
      $('#receiving-order-retry').hidden = false;
      data = undefined;
    } finally {
      window.clearTimeout(timeout);
      if (current === generation) {
        loading = false;
        $('#receiving-order-table').setAttribute('aria-busy', 'false');
        updatePagination();
      }
    }
  }

  function reload(delay = 0) {
    state.offset = 0;
    ++generation;
    abort?.abort();
    window.clearTimeout(searchTimer);
    startLoading();
    if (delay) searchTimer = window.setTimeout(() => void load(), delay);
    else void load();
  }
  $('#receiving-order-search').addEventListener('input', event => {
    state.q = (event.target as HTMLInputElement).value;
    reload(250);
  });
  $('#receiving-vendor-filter').addEventListener('change', event => {
    state.vendor_id = (event.target as HTMLSelectElement).value;
    reload();
  });
  $('#receiving-status-filter').addEventListener('change', event => {
    state.status = (event.target as HTMLSelectElement).value;
    reload();
  });
  $('#receiving-show-closed').addEventListener('change', event => {
    state.include_closed = (event.target as HTMLInputElement).checked;
    reload();
  });
  $('#receiving-order-headers').addEventListener('click', event => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-order-sort]');
    if (!button) return;
    const nextSort = button.dataset.orderSort as Sort;
    state.direction = nextSort === state.sort ? (state.direction === 'asc' ? 'desc' : 'asc')
      : nextSort === 'order_date' || nextSort === 'last_received_at' ? 'desc' : 'asc';
    state.sort = nextSort;
    updateHeaders();
    reload();
  });
  $('#receiving-order-rows').addEventListener('click', event => {
    const target = event.target as HTMLElement;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || target.closest('a,button,input,select,textarea,label') || window.getSelection()?.toString()) return;
    const row = target.closest<HTMLTableRowElement>('tr[data-po]');
    if (row) location.assign('?po='+encodeURIComponent(row.dataset.po!));
  });
  $('#receiving-order-previous').onclick = () => {
    state.offset = Math.max(0, state.offset-state.limit);
    void load();
  };
  $('#receiving-order-next').onclick = () => {
    if (data?.next_offset == null) return;
    state.offset = data.next_offset;
    void load();
  };
  $('#receiving-order-retry').onclick = () => void load();
  await load();
}
