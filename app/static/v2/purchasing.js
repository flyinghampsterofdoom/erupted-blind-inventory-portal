/* Recommendations remain server evidence; editable intended allocations sum to the order total. */
(() => {
  'use strict';
  const root = document.querySelector('#purchasing-draft');
  if (!root) return;
  let meta = JSON.parse(document.querySelector('#purchasing-bootstrap').textContent);
  const base = `/management/purchasing/sessions/${meta.id}`;
  const storageKey = `erupted-purchasing:${meta.owner_id}:${meta.id}:${meta.created_at}`;
  const filters = root.querySelector('[data-filters]');
  const filterPanel=root.querySelector('.purchasing-filter-panel');
  const narrow=matchMedia('(max-width:700px)');
  if(filterPanel){filterPanel.open=!narrow.matches;narrow.addEventListener('change',event=>filterPanel.open=!event.matches);}
  const groups = root.querySelector('[data-supplier-groups]');
  const status = root.querySelector('[data-save-status]');
  const errorBox = root.querySelector('[data-save-error]');
  const controllers = new Map();
  const timers = new Map();
  let version = meta.version;
  let queue = [];
  let busy = false;
  let paused = false;
  let conflict = false;
  let filterTimer;
  let refreshNeeded=false;
  const money = value => value == null ? 'Unknown' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(Number(value));
  const number = value => value == null ? '—' : new Intl.NumberFormat('en-US',{maximumFractionDigits:2}).format(Number(value));
  const el = (tag, attrs={}, text='') => {
    const node = document.createElement(tag);
    for (const [key,value] of Object.entries(attrs)) {
      if (key==='class') node.className=value;
      else if (value!==false && value!=null) node.setAttribute(key,value===true?'':String(value));
    }
    if (text!==undefined) node.textContent=text;
    return node;
  };
  const button = (text, action, cls='v2-button v2-button--secondary v2-button--compact') => {
    const node=el('button',{type:'button',class:cls},text);node.addEventListener('click',action);return node;
  };
  const get = async path => {
    const response=await fetch(path,{credentials:'same-origin',headers:{Accept:'application/json'},cache:'no-store'});
    if (!response.ok) throw new Error(`Unable to load the saved draft (${response.status}).`);
    return response.json();
  };
  function persist() {
    try {
      if (queue.length) localStorage.setItem(storageKey,JSON.stringify({version,queue}));
      else localStorage.removeItem(storageKey);
      return true;
    } catch (_) {
      paused=true;showError('This browser cannot retain draft changes. Free browser storage and retry before leaving.');return false;
    }
  }
  function rowState(id,text,state='saved') {
    const node=root.querySelector(`[data-row-status="${id}"]`);
    if (node) {node.textContent=text;node.dataset.state=state;}
  }
  function showError(message) {
    errorBox.hidden=false;errorBox.querySelector('[data-error-message]').textContent=message;
    status.textContent='Save needs attention';status.classList.add('purchasing-unsaved');
    errorBox.querySelector('[data-reapply]').hidden=!conflict;
    errorBox.querySelector('[data-retry]').hidden=conflict;
  }
  function clearError() {
    errorBox.hidden=true;status.classList.remove('purchasing-unsaved');
  }
  function pendingSummary(saved={}) {
    const holder=errorBox.querySelector('[data-pending-list]');holder.replaceChildren();
    const vendor=id=>meta.vendors.find(v=>v.id===Number(id))?.name||'Not selected';
    const account=id=>meta.accounts.find(v=>v.id===Number(id))?.name||'Not selected';
    for (const op of queue) {
      holder.append(el('strong',{},op.label||'Purchasing decision'));
      const wrap=el('div',{class:'purchasing-pending-comparison'});
      const table=el('table',{class:'v2-table'});const header=el('tr');
      ['Decision','Saved draft','Your unsaved decision'].forEach(value=>header.append(el('th',{},value)));table.append(header);
      const current=saved[String(op.line_id)]||{};
      function compare(label,before,after){const row=el('tr');[label,before,after].forEach(value=>row.append(el('td',{},value)));table.append(row);}
      if(op.kind==='remove'||op.kind==='undo')compare('Product',current.removed?'Removed':'Included',op.kind==='remove'?'Remove':'Restore');
      for(const [field,value]of Object.entries(op.changes)){
        if(field==='quantity')compare('Order quantity',number(current.quantity),number(value));
        if(field==='supplier_id')compare('Supplier',vendor(current.supplier_id),vendor(value));
        if(field==='cost')compare('Unit cost',money(current.cost),money(value));
        if(field==='account_id')compare('Intended account','Review saved supplier account',account(value));
        if(field==='allocations')for(const[id,quantity]of Object.entries(value))compare(`${meta.allocation_stores.find(s=>s.id===id)?.name||'Store'} allocation`,number(current.allocations?.[id]),number(quantity));
        if(field==='acknowledge')compare('Evidence review','Saved review state',value?'Confirm reviewed':'Keep unconfirmed');
        if(field==='accept_proposal')compare('Recommendation',number(current.quantity),'Accept updated recommendation');
      }
      wrap.append(table);holder.append(wrap);
    }
  }
  async function reviewConflict() {
    pendingSummary();
    try {
      const data=await get(`${base}/draft?line_ids=${queue.map(op=>op.line_id).filter(Boolean).slice(0,100).join(',')}`);
      pendingSummary(data.lines);
    } catch (_) { /* The local draft remains visible while the server is unavailable. */ }
  }
  function enqueue(op) {
    op={...op,staged:false};
    const index=queue.findLastIndex ? queue.findLastIndex(item=>!item.inflight && item.kind===op.kind && item.line_id===op.line_id && item.supplier_id===op.supplier_id) : -1;
    if (index>=0 && op.kind==='line') queue[index]={...queue[index],...op,changes:{...queue[index].changes,...op.changes}};
    else queue.push({...op,id:crypto.randomUUID()});
    if (!persist()) return;
    status.textContent=op.invalid||'Saving…';
    rowState(op.line_id,op.invalid||'Saving…',op.invalid?'error':'saving');
    if (op.invalid) {paused=true;showError(op.invalid);pendingSummary();return;}
    if (!conflict) {paused=false;clearError();}
    void pump();
  }
  async function pump() {
    if (busy||paused||!queue.length) return;
    busy=true;
    while (queue.length && !paused) {
      const op=queue[0];
      if (op.staged) break;
      if (op.invalid) {paused=true;showError(op.invalid);break;}
      op.inflight=true;persist();status.textContent='Saving…';rowState(op.line_id,'Saving…','saving');
      try {
        const controller=new AbortController();
        const timeout=setTimeout(()=>controller.abort(),15000);
        let response;
        try {
          response=await fetch(`${base}/draft`,{method:'PATCH',credentials:'same-origin',
            headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf,Accept:'application/json'},
            body:JSON.stringify({version,kind:op.kind,line_id:op.line_id,supplier_id:op.supplier_id,changes:op.changes}),signal:controller.signal});
        } finally {clearTimeout(timeout);}
        if(response.redirected&&response.url.includes('/login'))throw new Error('Your session expired. Log in again to save the retained draft.');
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('The save could not be confirmed. Your draft remains on this device.');
        const data=await response.json();
        if (!response.ok) {
          conflict=response.status===409;throw new Error(data.detail||'The draft could not be saved.');
        }
        version=data.version;queue.shift();persist();rowState(op.line_id,'Saved');
        root.querySelectorAll('input[name="version"]').forEach(input=>input.value=version);
      } catch (error) {
        op.inflight=false;paused=true;persist();showError(error.message||'Connection lost. Your draft is retained.');
        rowState(op.line_id,'Unconfirmed','error');await reviewConflict();
      }
    }
    busy=false;
    if (!queue.length) {
      clearError();status.textContent='Saved · updating totals…';
      try {await refreshMeta();if(!queue.length)status.textContent='All changes saved';} catch (_) {showError('Changes saved. Totals could not reload; retry the connection.');}
      // Do not replace a control while the owner is typing in it.
      refreshNeeded=true;
      if (!root.querySelector('.purchasing-table input:focus,.purchasing-table select:focus') && !timers.size) {refreshNeeded=false;await refreshOpen();}
    }
  }
  async function flush() {
    for (const [key,entry] of [...timers]) {clearTimeout(entry.timer);timers.delete(key);entry.run();}
    if (paused) {errorBox.scrollIntoView({block:'nearest'});return false;}
    await pump();
    if (busy) {await new Promise(resolve=>{const timer=setInterval(()=>{if(!busy){clearInterval(timer);resolve();}},30);});}
    return !paused&&!queue.length;
  }
  function delayed(key,run) {
    const existing=timers.get(key);if (existing) clearTimeout(existing.timer);
    timers.set(key,{run,timer:setTimeout(()=>{timers.delete(key);run();},450)});
  }
  function params(supplier,page=1) {
    const values=new URLSearchParams(new FormData(filters));values.set('supplier',supplier);values.set('page',page);return values;
  }
  function summaryNode(group) {
    const summary=el('summary');summary.append(el('span',{class:'purchasing-supplier-name'},group.name));
    const totals=el('span',{class:'purchasing-supplier-metrics'});
    totals.append(el('span',{'data-count':''},`${number(group.product_count)} products`),
      el('span',{'data-units':''},`${number(group.units)} units`),el('strong',{'data-cost':''},money(group.cost)));
    summary.append(totals);return summary;
  }
  function syncGroups() {
    const chosen=filters.elements.supplier.value;
    const filtered=Boolean(filters.elements.q.value.trim()||filters.elements.store.value||filters.elements.state.value!=='active');
    filters.querySelector('option[value="review"]').textContent=`Needs review (${meta.review_count||0})`;
    const totalUnits=meta.groups.reduce((sum,g)=>sum+g.units,0);
    const totalProducts=meta.groups.reduce((sum,g)=>sum+g.product_count,0);
    root.querySelector('[data-draft-summary]').textContent=`${number(totalProducts)} products · ${number(totalUnits)} units · ${meta.status.toLowerCase()}`;
    root.querySelector('[data-evidence-time]').textContent=`Evidence ${meta.observed_at}`;
    for (const group of meta.groups) {
      let ctrl=controllers.get(group.id);
      if (!ctrl) {
        const details=el('details',{class:'v2-card purchasing-supplier vendor','data-supplier':group.id});
        const summary=summaryNode(group);const body=el('div',{'data-group-body':''});
        details.append(summary,body);groups.append(details);
        ctrl={details,body,page:1,loaded:false,group};controllers.set(group.id,ctrl);
        details.addEventListener('toggle',()=>{if(details.open&&!ctrl.loaded)void loadGroup(ctrl);});
      }
      ctrl.group=group;
      const metrics=ctrl.details.querySelector('.purchasing-supplier-metrics');
      metrics.querySelector('[data-count]').textContent=`${number(group.product_count)} products`;
      metrics.querySelector('[data-units]').textContent=`${number(group.units)} units`;
      metrics.querySelector('[data-cost]').textContent=money(group.cost);
      ctrl.details.hidden=Boolean(chosen&&String(group.id)!==chosen)||Boolean(filtered&&!group.matched_count);
    }
  }
  async function refreshMeta() {
    const selected=params('',1);selected.delete('supplier');
    const next=await get(`${base}/workspace?${selected}`);meta=next;
    // Version is changed only by successful saves or explicit owner conflict resolution.
    syncGroups();
  }
  async function refreshOpen() {
    await Promise.all([...controllers.values()].filter(c=>c.details.open&&!c.details.hidden).map(c=>loadGroup(c)));
  }
  const supplierSelect = value => {
    const select=el('select');select.append(el('option',{value:0},'Choose supplier'));
    for (const vendor of meta.vendors) select.append(el('option',{value:vendor.id},vendor.name));
    select.value=String(value||0);return select;
  };
  function tableStores() {
    return [...meta.stores,...meta.allocation_stores.filter(store=>!meta.stores.some(active=>active.id===store.id)).map(store=>({...store,name:`${store.name} (saved)`}))];
  }
  function rowPair(item) {
    const row=el('tr',{'data-line-id':item.id});
    const detail=el('tr',{'data-detail-id':item.id,hidden:true});
    const detailCell=el('td',{colspan:tableStores().length+5,class:'purchasing-detail-cell'});detail.append(detailCell);
    const productCell=el('td');const product=el('div',{class:'purchasing-product-name'});
    const expand=button('›',()=>{detail.hidden=!detail.hidden;expand.setAttribute('aria-expanded',String(!detail.hidden));expand.textContent=detail.hidden?'›':'⌄';},'purchasing-expand');
    expand.setAttribute('aria-label',`Calculations and allocations for ${item.sku||item.name}`);expand.setAttribute('aria-expanded','false');
    const copy=el('span',{class:'purchasing-product-copy'});copy.append(el('strong',{},item.name),el('small',{},`${item.sku||'No SKU'} · ${item.variant}`));
    if(item.needs_review)copy.querySelector('small').append(el('span',{class:'purchasing-review-badge'},' · Review'));
    product.append(expand,copy);productCell.append(product);row.append(productCell);
    const allocationInputs={};
    const allocationMismatch=Object.values(item.allocations).reduce((sum,value)=>sum+Number(value),0)!==item.quantity;
    for (const store of tableStores()) {
      const known=Object.hasOwn(item.allocations,store.id);
      const td=el('td',{class:'purchasing-numeric','data-store-id':store.id});
      const input=el('input',{type:'number',required:true,min:0,max:1000000,step:1,
        value:known?item.allocations[store.id]:'',placeholder:'—',class:'purchasing-store-quantity',
        'aria-label':`Allocation ${store.name} ${item.sku||item.name}`,
        title:known?'Intended allocation; original recommendation in calculation details':'No saved allocation evidence for this store'});
      input.disabled=!known||!item.editable||item.removed||allocationMismatch;
      if(known)allocationInputs[store.id]=input;
      td.append(input);row.append(td);
    }
    const qtyCell=el('td',{class:'purchasing-numeric'});
    const qty=el('output',{'aria-label':`Total quantity ${item.sku||item.name}`},number(item.quantity));qty.value=String(item.quantity);
    const saved=el('span',{class:'purchasing-row-state','data-row-status':item.id,'aria-live':'polite'},'');qtyCell.append(qty,saved);row.append(qtyCell);
    const costCell=el('td');let cost;
    if (item.cost_source==='CONFIGURED'&&item.configured_cost!=null) costCell.append(el('span',{title:'Configured unit cost'},money(item.cost)));
    else {cost=el('input',{type:'number',required:true,min:0,step:'0.0001',value:item.cost??'',placeholder:'Unknown','aria-label':`Unit cost ${item.sku||item.name}`});costCell.append(cost);}
    row.append(costCell);
    const supplierCell=el('td');const supplier=supplierSelect(item.supplier_id);supplier.setAttribute('aria-label',`Supplier ${item.sku||item.name}`);supplierCell.append(supplier);row.append(supplierCell);
    const removeCell=el('td');const remove=button(item.removed?'↶':'×',()=>enqueue({kind:item.removed?'undo':'remove',line_id:item.id,changes:{},label:item.sku||item.name}), 'purchasing-remove');
    remove.setAttribute('aria-label',`${item.removed?'Undo removal':'Remove'} ${item.sku||item.name}`);removeCell.append(remove);row.append(removeCell);
    for (const control of [supplier,cost,remove].filter(Boolean)) control.disabled=!item.editable||item.removed&&control!==remove||allocationMismatch&&control!==remove;
    if (item.removed&&meta.status==='ACTIVE'&&item.editable) remove.disabled=false;
    const calculation=el('div',{class:'purchasing-calculations'});const evidence=el('section');evidence.append(el('h3',{},'Saved recommendation evidence'));
    const table=el('table',{class:'v2-table'});const head=el('thead');const headers=el('tr');
    ['Store','Square at calc.','Latest Square','Incoming at calc.','Forecast / day','Calculated need'].forEach(label=>headers.append(el('th',{},label)));head.append(headers);table.append(head);
    const body=el('tbody');for(const store of meta.stores){const tr=el('tr');const obs=item.observations[store.id];const forecast=item.forecasts[store.id];
      [store.name,number(obs?.current_inventory),number(item.latest_observations?.[store.id]?.current_inventory),number(obs?.incoming),number(forecast?.daily_velocity),number(forecast?.raw_need)].forEach(value=>tr.append(el('td',{},value)));body.append(tr);}
    table.append(body);evidence.append(table,el('p',{class:'purchasing-detail-note'},'Square controls current on-hand. Incoming is a separate outstanding commitment. Historical receipts remain audit evidence.'));
    const allocationNote=el('p',{class:'purchasing-allocation-note'});
    const allocationSum=()=>Object.values(allocationInputs).reduce((sum,input)=>sum+Number(input.value),0);
    function allocationMessage(){
      qty.value=String(allocationSum());qty.textContent=number(allocationSum());
      allocationNote.textContent=allocationMismatch
        ?`Saved order quantity ${number(item.quantity)} differs from saved allocation total ${number(allocationSum())}. Editing is blocked pending explicit reconciliation; no saved decisions were changed.`
        :`Total ${number(allocationSum())} = intended store allocations. Original calculated need: ${number(item.need)}. Saved engine allocations include applicable pack rounding; editing stores establishes your manual plan.`;
    }
    allocationMessage();calculation.append(evidence);detailCell.append(calculation,allocationNote);
    if(item.configured_cost!=null&&Number(item.configured_cost)!==Number(item.cost)){detailCell.append(el('p',{class:'purchasing-detail-note'},`Configured cost is now ${money(item.configured_cost)}; saved draft cost is ${money(item.cost)}.`),button('Use current configured cost',()=>enqueue({kind:'line',line_id:item.id,changes:{},label:item.sku||item.name})));}
    if(item.proposal){const changed=el('p',{class:'purchasing-detail-note'},`Updated recommendation: ${number(item.proposal.quantity)}. Saved need ${number(item.need)} and your order ${number(item.quantity)} stay unchanged until you accept.`);
      detailCell.append(changed,button('Accept Updated Quantity',()=>enqueue({kind:'line',line_id:item.id,changes:{accept_proposal:true},label:item.sku||item.name})));}
    if(item.review.length){detailCell.append(el('p',{class:'purchasing-detail-note'},item.review.join('; ')));}
    if(item.needs_review){const label=el('label',{},'I reviewed this uncertainty and choose this quantity and cost.');const check=el('input',{type:'checkbox','aria-label':`Confirm uncertainty ${item.sku||item.name}`});label.prepend(check);detailCell.append(label);check.addEventListener('change',()=>enqueue({kind:'line',line_id:item.id,changes:{acknowledge:check.checked},label:item.sku||item.name}));saved.textContent='Review evidence';}
    function edit(fields){
      const prior=queue.find(op=>!op.inflight&&op.kind==='line'&&op.line_id===item.id);
      const changes={...(prior?.changes||{}),...fields};let invalid;
      if ('quantity' in changes && (!Number.isInteger(changes.quantity)||changes.quantity<0||changes.quantity>1000000)) invalid='The allocation total must be a whole number from 0 to 1,000,000.';
      if (cost && 'cost' in changes && (!cost.validity.valid||cost.value==='')) invalid='Enter and confirm a valid nonnegative purchase unit cost.';
      if (item.decisions.allocation_override || 'allocations' in changes) {
        changes.allocations=Object.fromEntries(Object.entries(allocationInputs).map(([id,input])=>[id,input.value===''?'':Number(input.value)]));
        if(Object.values(allocationInputs).some(input=>!input.validity.valid||input.value==='')||allocationSum()!==Number(changes.quantity??item.quantity))invalid='Enter nonnegative whole store quantities; their total must be at most 1,000,000. Your edits remain on this device until valid.';
      }
      allocationMessage();enqueue({kind:'line',line_id:item.id,changes,label:item.sku||item.name,invalid});
    }
    supplier.addEventListener('change',()=>{supplier.blur();edit({supplier_id:Number(supplier.value),quantity:allocationSum(),allocations:Object.fromEntries(Object.entries(allocationInputs).map(([id,input])=>[id,input.value===''?'':Number(input.value)]))});});
    if(cost)cost.addEventListener('input',()=>{stageLocal({cost:cost.value});delayed(`cost-${item.id}`,()=>edit({cost:cost.value}));});
    for(const input of Object.values(allocationInputs))input.addEventListener('input',()=>{
      item.decisions.allocation_override=true;allocationMessage();rowState(item.id,'Unsaved','saving');
      const changes={quantity:allocationSum(),allocations:Object.fromEntries(Object.entries(allocationInputs).map(([id,field])=>[id,field.value===''?'':Number(field.value)]))};
      stageLocal(changes);delayed(`alloc-${item.id}`,()=>edit(changes));
    });
    function stageLocal(changes){
      // Persist the keystroke before the debounce or any navigation can occur.
      const prior=queue.find(op=>!op.inflight&&op.kind==='line'&&op.line_id===item.id);
      const invalid=changes.allocations&&(Object.values(allocationInputs).some(input=>!input.validity.valid||input.value==='')||!Number.isInteger(changes.quantity)||changes.quantity>1000000)?'Enter nonnegative whole store quantities with a total at most 1,000,000.':null;
      if(prior){prior.changes={...prior.changes,...changes};prior.invalid=invalid;prior.staged=true;}
      else queue.push({id:crypto.randomUUID(),kind:'line',line_id:item.id,changes,label:item.sku||item.name,invalid,staged:true});
      persist();status.textContent='Unsaved changes';
    }
    const pending=queue.filter(op=>op.line_id===item.id&&op.kind==='line').reduce((all,op)=>({...all,...op.changes}),{});
    if('quantity' in pending&&!pending.allocations)detailCell.append(el('p',{class:'v2-callout'},'An earlier unsaved order-total edit is retained for review. It has not been converted into a new store allocation.'));
    if('supplier_id' in pending)supplier.value=pending.supplier_id;
    if('cost' in pending&&cost)cost.value=pending.cost;
    if(pending.allocations)for(const[id,value]of Object.entries(pending.allocations))if(allocationInputs[id])allocationInputs[id].value=value;
    if(Object.keys(pending).length){rowState(item.id,'Unsaved','error');allocationMessage();}
    return [row,detail];
  }
  async function loadGroup(ctrl) {
    const requestId=(ctrl.requestId||0)+1;ctrl.requestId=requestId;
    try {
      const data=await get(`${base}/workspace?${params(ctrl.group.id,ctrl.page)}`);
      if(requestId!==ctrl.requestId)return;
      const expanded=new Set([...ctrl.body.querySelectorAll('[data-detail-id]')].filter(node=>!node.hidden).map(node=>node.dataset.detailId));
      ctrl.page=data.page;ctrl.loaded=true;ctrl.body.replaceChildren();
      if(ctrl.group.status==='CONVERTED'){
        const panel=el('div',{class:'purchasing-group-actions'});panel.append(el('a',{href:`/management/ordering-tool/orders/${ctrl.group.po_id}`,class:'v2-button v2-button--secondary v2-button--compact'},`Open PO ${ctrl.group.po_id}`));ctrl.body.append(panel);appendAccount(panel,ctrl.group);return;
      }
      const scroll=el('div',{class:'purchasing-table-scroll',tabindex:0,'aria-label':`${ctrl.group.name} recommendations, scroll horizontally for all columns`});
      const table=el('table',{class:'v2-table purchasing-table'});table.append(el('caption',{},`${ctrl.group.name} saved purchasing recommendations`));
      const head=el('thead');const tr=el('tr');tr.append(el('th',{scope:'col'},'Product / SKU'));
      for(const store of tableStores())tr.append(el('th',{scope:'col',class:'purchasing-store'},store.name));
      for(const [name,cls]of [['Total','purchasing-qty'],['Unit Cost','purchasing-cost'],['Supplier','purchasing-selected-supplier'],['Remove','']])tr.append(el('th',{scope:'col',class:cls},name));
      head.append(tr);table.append(head);const tbody=el('tbody');
      for(const item of data.rows){const pair=rowPair(item);if(expanded.has(String(item.id))){pair[1].hidden=false;pair[0].querySelector('[aria-expanded]').setAttribute('aria-expanded','true');pair[0].querySelector('[aria-expanded]').textContent='⌄';}tbody.append(...pair);}
      if(!data.rows.length)tbody.append(el('tr')).append(el('td',{colspan:tableStores().length+5,class:'v2-table__empty'},'No products match these filters.'));
      table.append(tbody);scroll.append(table);ctrl.body.append(scroll);
      const pager=el('div',{class:'purchasing-pagination'});pager.append(el('span',{},`${data.total?((data.page-1)*data.page_size+1):0}–${Math.min(data.page*data.page_size,data.total)} of ${number(data.total)} matching products · page ${data.page} / ${data.pages}`));
      const actions=el('span');const previous=button('Previous',async()=>{if(await flush()){ctrl.page--;await loadGroup(ctrl);}});previous.disabled=data.page<=1;
      const next=button('Next',async()=>{if(await flush()){ctrl.page++;await loadGroup(ctrl);}});next.disabled=data.page>=data.pages;actions.append(previous,next);pager.append(actions);ctrl.body.append(pager);
      const footer=el('div',{class:'purchasing-group-actions'});appendAccount(footer,ctrl.group);
      const groupActions=el('div',{class:'purchasing-controls'});
      if(meta.status==='ACTIVE'&&ctrl.group.id){
        const create=actionForm(`${base}/groups/${ctrl.group.id}/po`,'Create PO','v2-button v2-button--compact');
        create.querySelector('button').title=`Creates this supplier’s full saved draft (${ctrl.group.product_count} products), including rows outside the current filters.`;
        create.querySelector('button').disabled=ctrl.group.status!=='OPEN'||ctrl.group.units<=0;
        groupActions.append(create);
        groupActions.append(actionForm(`${base}/groups/${ctrl.group.id}/discard`,'Discard Group','v2-button v2-button--secondary v2-button--compact'));
      }
      footer.append(groupActions);ctrl.body.prepend(footer);
      ctrl.body.append(el('p',{class:'purchasing-evidence v2-muted',style:'padding:0 12px'},'Intended account and purchasing totals are estimates; no charge, payment or inventory movement is recorded.'));
    } catch(error){ctrl.loaded=false;ctrl.body.replaceChildren(el('p',{class:'v2-callout'},error.message),button('Retry table',()=>loadGroup(ctrl)));}
  }
  function appendAccount(holder,group){
    if(!group.id)return;
    const label=el('label',{},'Intended payment account');const select=el('select',{'aria-label':`Intended payment account ${group.name}`});select.append(el('option',{value:''},'Not selected'));
    for(const account of meta.accounts)select.append(el('option',{value:account.id},account.name));select.value=String(group.account_id||'');label.append(select);
    select.addEventListener('change',()=>enqueue({kind:'account',supplier_id:group.id,changes:{account_id:select.value},label:group.name}));holder.append(label);
  }
  function actionForm(action,label,cls){const form=el('form',{method:'post',action,'data-draft-action':''});form.append(el('input',{type:'hidden',name:'csrf_token',value:root.dataset.csrf}),el('input',{type:'hidden',name:'version',value:version}),el('button',{class:cls},label));return form;}
  filters.addEventListener('submit',event=>event.preventDefault());
  async function applyFilters(){if(!await flush())return;await refreshMeta();for(const ctrl of controllers.values()){ctrl.page=1;ctrl.loaded=false;}await refreshOpen();}
  filters.addEventListener('input',event=>{if(event.target.name==='q'){clearTimeout(filterTimer);filterTimer=setTimeout(applyFilters,300);}});
  filters.addEventListener('change',event=>{if(event.target.name!=='q')void applyFilters();});
  root.addEventListener('submit',async event=>{const form=event.target;if(!form.matches('[data-draft-action]'))return;event.preventDefault();if(await flush())HTMLFormElement.prototype.submit.call(form);});
  document.addEventListener('click',async event=>{const link=event.target.closest('a[href]');if(link&&link.origin===location.origin&&(queue.length||busy||timers.size)){event.preventDefault();if(await flush())location.assign(link.href);}});
  window.addEventListener('beforeunload',event=>{if(queue.length||busy||timers.size){persist();event.preventDefault();event.returnValue='';}});
  root.addEventListener('focusout',()=>{if(refreshNeeded&&!queue.length&&!busy&&!timers.size)setTimeout(()=>{if(!root.querySelector('.purchasing-table input:focus,.purchasing-table select:focus')){refreshNeeded=false;void refreshOpen();}},50);});
  errorBox.querySelector('[data-retry]').addEventListener('click',async()=>{
    try{const data=await get(`${base}/draft`);if(data.version!==version){conflict=true;showError('The saved session changed. Compare your pending decisions before reapplying.');await reviewConflict();return;}
      paused=false;clearError();void pump();}catch(error){showError(error.message);}
  });
  errorBox.querySelector('[data-reapply]').addEventListener('click',async()=>{
    try{const data=await get(`${base}/draft`);version=data.version;conflict=false;paused=false;for(const op of queue)op.inflight=false;persist();clearError();void pump();}catch(error){showError(error.message);}
  });
  window.addEventListener('storage',async event=>{
    if(event.key!==storageKey||event.newValue!==null||!paused||busy||!queue.length)return;
    try{
      const data=await get(`${base}/draft?line_ids=${queue.map(op=>op.line_id).filter(Boolean).join(',')}`);
      queue=queue.filter(op=>{
        const saved=data.lines[String(op.line_id)];if(!saved)return true;
        if(op.kind==='remove')return !saved.removed;
        if(op.kind==='undo')return saved.removed;
        if(op.kind!=='line')return true;
        return !Object.entries(op.changes).every(([key,value])=>{
          if(['quantity','supplier_id','cost'].includes(key))return Number(saved[key])===Number(value);
          if(key==='allocations')return Object.entries(value).every(([id,qty])=>saved.allocations[id]===qty);
          return false;
        });
      });
      if(!queue.length){version=data.version;conflict=false;paused=false;clearError();status.textContent='All changes saved';await refreshMeta();await refreshOpen();}
    }catch(_){/* Preserve the local decisions when confirmation is unavailable. */}
  });
  const catalogSearch=root.querySelector('[data-catalog-search]');
  if(catalogSearch)catalogSearch.addEventListener('submit',async event=>{event.preventDefault();const query=catalogSearch.elements.q.value;
    try{const data=await get(`${base}/catalog?q=${encodeURIComponent(query)}`);const select=root.querySelector('[data-catalog-products]');select.replaceChildren(el('option',{value:''},'Choose a search result'));
      for(const p of data.products)select.append(el('option',{value:p.id},`${p.name} · ${p.variant} · ${p.sku||'No SKU'}`));
      root.querySelector('[data-catalog-message]').textContent=`${data.products.length} active catalog matches (up to 50). Adding affects only this session.`;
    }catch(error){root.querySelector('[data-catalog-message]').textContent=error.message;}
  });
  try {
    const saved=JSON.parse(localStorage.getItem(storageKey)||'null');
    if(saved?.queue?.length){queue=saved.queue.map(op=>({...op,inflight:false}));paused=true;conflict=true;
      showError('Unsaved decisions were recovered from this device. Review them against the saved session before reapplying.');void reviewConflict();}
  } catch(_){showError('Saved local draft could not be read. Keep this page open while checking browser storage.');}
  syncGroups();
})();
