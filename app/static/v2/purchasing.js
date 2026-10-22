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
  const saveButton=root.querySelector('[data-save-draft]');
  const editControls=root.querySelector('[data-edit-controls]');
  const inventoryCache=new Map();
  let pendingBatch=null;
  let finalReviewSupplier=null;
  const expandedReviewLines=new Set();
  let version = meta.version;
  let queue = [];
  let busy = false;
  let paused = false;
  let conflict = false;
  let filterTimer;
  let refreshNeeded=false;
  const money = value => value == null ? 'Unknown' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(Number(value));
  const velocity = value => value == null ? 'Unknown' : new Intl.NumberFormat('en-US',{maximumFractionDigits:4}).format(Number(value));
  const timestamp = value => value ? new Date(value).toLocaleString() : 'Unknown';
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
      if (queue.length) localStorage.setItem(storageKey,JSON.stringify({version,queue,pendingBatch}));
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
      for(const op of queue)if(op.preview&&data.lines[String(op.line_id)])op.preview={...op.preview,...data.lines[String(op.line_id)]};
      syncGroups();
    } catch (_) { /* The local draft remains visible while the server is unavailable. */ }
  }
  function draftStatus() {
    const count=new Set(queue.map(op=>op.line_id?`line:${op.line_id}`:`account:${op.supplier_id}`)).size;
    if(!paused)status.textContent=busy?'Saving draft…':count?`${count} unsaved ${count===1?'change':'changes'}`:'Draft saved';
    saveButton.disabled=busy||!queue.length;
    saveButton.textContent=busy?'Saving…':'Save Draft';
    editControls.disabled=busy;
  }
  function enqueue(op) {
    if(busy)return;
    op={...op,staged:false};
    const index=queue.findLastIndex(item=>!item.sent&&item.kind===op.kind&&item.line_id===op.line_id&&item.supplier_id===op.supplier_id);
    if(index>=0&&['line','account'].includes(op.kind))queue[index]={...queue[index],...op,id:crypto.randomUUID(),preview:op.preview?.observations?op.preview:(queue[index].preview||op.preview),changes:{...queue[index].changes,...op.changes}};
    else queue.push({...op,id:crypto.randomUUID()});
    if(!persist()){draftStatus();return;}rowState(op.line_id,op.invalid||'Unsaved',op.invalid?'error':'saving');
    if(op.invalid){paused=true;showError(op.invalid);pendingSummary();}
    else if(!conflict&&!pendingBatch){paused=false;clearError();}
    draftStatus();syncGroups();
    const finalize=root.querySelector('[data-finalize-submit]');if(finalize)finalize.disabled=true;
  }
  async function pump() {
    if(busy||!queue.length)return;
    if(queue.some(op=>op.invalid)){paused=true;showError('Correct the highlighted quantities or costs before saving. All edits remain on this device.');pendingSummary();return;}
    if(conflict){showError('Compare your pending decisions with the latest saved draft before reapplying.');return;}
    busy=true;paused=false;clearError();
    if(!pendingBatch){
      pendingBatch={version,request_key:crypto.randomUUID(),operations:queue.map(op=>({kind:op.kind,...(op.line_id?{line_id:op.line_id}:{}),...(op.supplier_id?{supplier_id:op.supplier_id}:{}),changes:op.changes})),ids:queue.map(op=>op.id)};
      queue.forEach(op=>op.sent=true);
    }
    if(!persist()){busy=false;draftStatus();return;}draftStatus();
    try {
      const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),30000);
      let response;
      try{const {ids,...payload}=pendingBatch;response=await fetch(`${base}/draft/batch`,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf,Accept:'application/json'},body:JSON.stringify(payload),signal:controller.signal});}
      finally{clearTimeout(timeout);}
      if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Save could not be confirmed. Your local draft is retained.');
      const data=await response.json();
      if(!response.ok){
        conflict=response.status===409;
        if(response.status>=400&&response.status<500){pendingBatch=null;queue.forEach(op=>op.sent=false);}
        throw new Error(data.detail||'The draft could not be saved.');
      }
      version=data.version;const savedIds=new Set(pendingBatch.ids);queue=queue.filter(op=>!savedIds.has(op.id));pendingBatch=null;persist();
      root.querySelectorAll('input[name="version"]').forEach(input=>input.value=version);
      // If edits were added after an uncertain save, their baseline is now the confirmed server state.
      if(queue.length){
        const saved=await get(`${base}/draft?line_ids=${queue.map(op=>op.line_id).filter(Boolean).join(',')}`);
        for(const op of queue)if(saved.lines[String(op.line_id)])op.preview={...op.preview,...saved.lines[String(op.line_id)]};
      }
      await refreshMeta();await refreshOpen();
      status.textContent=queue.length?'Saved earlier changes; newer edits are still unsaved.':'Draft saved. No purchase orders created.';
    } catch(error) {
      paused=true;persist();showError(error.message||'Connection lost. Your local draft is retained.');await reviewConflict();
    } finally {busy=false;draftStatus();if(!paused&&finalReviewSupplier)void showFinalReview(finalReviewSupplier);}
  }
  async function flush() {
    // Browsing is read-only. Quantity edits are staged synchronously and never autosaved.
    return !busy;
  }
  function delayed(key,run) {
    const existing=timers.get(key);if (existing) clearTimeout(existing.timer);
    timers.set(key,{run,timer:setTimeout(()=>{timers.delete(key);run();},450)});
  }
  function params(supplier,page=1) {
    const values=new URLSearchParams();for(const field of filters.elements)if(field.name)values.set(field.name,field.value);values.set('supplier',supplier);values.set('page',page);return values;
  }
  function overlayItem(original) {
    const item={...original,allocations:{...original.allocations},decisions:{...original.decisions}};
    for(const op of queue.filter(op=>op.line_id===item.id)){
      if(op.kind==='remove')item.removed=true;
      if(op.kind==='undo')item.removed=false;
      if(op.kind==='line'){
        for(const key of ['quantity','supplier_id','cost'])if(key in op.changes)item[key]=op.changes[key];
        if(op.changes.allocations){item.allocations={...op.changes.allocations};item.decisions.allocation_override=true;}
        if(op.changes.acknowledge===true)item.needs_review=false;
        if(op.changes.acknowledge===false)item.needs_review=true;
      }
    }
    return item;
  }
  function pendingLines(){const values=new Map();for(const op of queue)if(op.line_id&&op.preview)values.set(op.line_id,op.preview);return [...values.values()];}
  function displayGroups(){
    const result=new Map(meta.groups.map(g=>[g.id,{...g}]));
    function group(id){if(!result.has(id))result.set(id,{id,name:meta.vendors.find(v=>v.id===id)?.name||'Needs supplier',status:'OPEN',product_count:0,units:0,cost:0,matched_count:1});return result.get(id);}
    for(const original of pendingLines()){
      const current=overlayItem(original);
      for(const [item,sign]of [[original,-1],[current,1]])if(!item.removed){
        const g=group(item.supplier_id||0);g.product_count+=sign;g.units+=sign*Number(item.quantity||0);
        if(g.cost!=null&&item.cost!=null)g.cost=Number(g.cost)+sign*Number(item.cost)*Number(item.quantity||0);else g.cost=null;
        if(sign===1)g.matched_count=Math.max(1,g.matched_count||0);
      }
    }
    return [...result.values()].sort((a,b)=>a.name.localeCompare(b.name));
  }
  function matchesItem(item){
    const f=filters.elements;const tokens=f.q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if(!tokens.every(token=>`${item.name} ${item.variant} ${item.sku}`.toLowerCase().includes(token)))return false;
    if(f.state.value==='removed')return Boolean(item.removed);
    if(item.removed)return false;
    if(f.state.value==='active'&&(Number(item.quantity)<=0||item.needs_review))return false;
    if(f.state.value==='review'&&!item.needs_review)return false;
    if(f.store.value&&item.forecasts?.[f.store.value]?.raw_need===0)return false;
    return true;
  }
  function workingData(saved,ctrl){
    const data={...saved,categories:(saved.categories||[]).map(c=>({...c}))};
    const cats=new Map(data.categories.map(c=>[c.id,c]));let delta=0;
    const rowMatches=item=>Number(item.supplier_id)===Number(ctrl.group.id)&&matchesItem(item)&&(!ctrl.category||item.item_type?.id===ctrl.category);
    for(const original of pendingLines()){
      const current=overlayItem(original);delta+=Number(rowMatches(current))-Number(rowMatches(original));
      for(const[item,sign]of [[original,-1],[current,1]])if(Number(item.supplier_id)===Number(ctrl.group.id)&&matchesItem(item)&&item.item_type){
        if(!cats.has(item.item_type.id))cats.set(item.item_type.id,{...item.item_type,count:0});cats.get(item.item_type.id).count+=sign;
      }
    }
    const rows=new Map(saved.rows.map(item=>[item.id,item]));
    for(const original of pendingLines())if(Number(overlayItem(original).supplier_id)===Number(ctrl.group.id)&&original.supplier_id!==overlayItem(original).supplier_id)rows.set(original.id,original);
    data.rows=[...rows.values()].map(overlayItem).filter(rowMatches);
    data.total=Math.max(0,saved.total+delta);data.categories=[...cats.values()].filter(c=>c.count>0);data.pages=Math.max(1,Math.ceil(data.total/data.page_size));
    return data;
  }
  function renderWorkingGroups(){syncGroups();for(const ctrl of controllers.values())if(ctrl.details.open&&!ctrl.details.hidden&&ctrl.data)void loadGroup(ctrl,true);}
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
    const reviewButton=root.querySelector('[data-show-review]');reviewButton.textContent=`Review items (${meta.review_count||0})`;reviewButton.hidden=!meta.review_count;
    const shownGroups=displayGroups();
    const totalUnits=shownGroups.reduce((sum,g)=>sum+g.units,0);
    const totalProducts=shownGroups.reduce((sum,g)=>sum+g.product_count,0);
    root.querySelector('[data-draft-summary]').textContent=`${number(totalProducts)} products · ${number(totalUnits)} units · ${meta.status.toLowerCase()}`;
    root.querySelector('[data-evidence-time]').textContent=`Evidence ${meta.observed_at}`;
    for (const group of shownGroups) {
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
  function rowPair(original) {
    const item={review:[],observations:{},forecasts:{},need:original.quantity,editable:meta.status==='ACTIVE',cost_source:'OWNER_CONFIRMED',...overlayItem(original)};
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
      const inventory=el('span',{class:'purchasing-on-hand','data-live-hand':`${item.id}:${store.id}`,'aria-label':`Current Square inventory ${store.name} ${item.sku||item.name}`},'Loading…');
      const pair=el('div',{class:'purchasing-store-pair'});pair.append(inventory,input);td.append(pair);row.append(td);
    }
    const qtyCell=el('td',{class:'purchasing-numeric'});
    const qty=el('output',{'aria-label':`Total quantity ${item.sku||item.name}`},number(item.quantity));qty.value=String(item.quantity);
    const saved=el('span',{class:'purchasing-row-state','data-row-status':item.id,'aria-live':'polite'},'');qtyCell.append(qty,saved);row.append(qtyCell);
    const costCell=el('td');let cost;
    if (item.cost_source==='CONFIGURED'&&item.configured_cost!=null) costCell.append(el('span',{title:'Configured unit cost'},money(item.cost)));
    else {cost=el('input',{type:'number',required:true,min:0,step:'0.0001',value:item.cost??'',placeholder:'Unknown','aria-label':`Unit cost ${item.sku||item.name}`});costCell.append(cost);}
    row.append(costCell);
    const supplierCell=el('td');const supplier=supplierSelect(item.supplier_id);supplier.setAttribute('aria-label',`Supplier ${item.sku||item.name}`);supplierCell.append(supplier);row.append(supplierCell);
    const removeCell=el('td');const remove=button(item.removed?'↶':'×',()=>{enqueue({kind:item.removed?'undo':'remove',line_id:item.id,preview:original,changes:{},label:item.sku||item.name});renderWorkingGroups();}, 'purchasing-remove');
    remove.setAttribute('aria-label',`${item.removed?'Undo removal':'Remove'} ${item.sku||item.name}`);removeCell.append(remove);row.append(removeCell);
    for (const control of [supplier,cost,remove].filter(Boolean)) control.disabled=!item.editable||item.removed&&control!==remove||allocationMismatch&&control!==remove;
    if (item.removed&&meta.status==='ACTIVE'&&item.editable) remove.disabled=false;
    const calculation=el('div',{class:'purchasing-calculations'});const evidence=el('section');evidence.append(el('h3',{},'Saved recommendation evidence'));
    const table=el('table',{class:'v2-table'});const head=el('thead');const headers=el('tr');
    ['Store','Square at calculation','Current Square','Incoming','Position','Weekly (approx.)','Velocity / day','Reorder threshold','Target','Calculated need','Planned allocation'].forEach(label=>headers.append(el('th',{},label)));head.append(headers);table.append(head);
    const body=el('tbody');for(const store of tableStores()){
      const tr=el('tr');const obs=item.observations[store.id];const forecast=item.forecasts[store.id];
      tr.append(el('td',{},store.name));
      const savedHand=el('td',{},obs?.current_inventory==null?'Unknown':number(obs.current_inventory));
      const source=obs?.sources?.find(s=>s.source==='inventory');
      savedHand.title=`Square read for calculation: ${timestamp(source?.observed_at)}. ${source?.detail||''}`;tr.append(savedHand);
      tr.append(el('td',{'data-live-hand':`${item.id}:${store.id}`},'Loading…'));
      [number(obs?.incoming),number(forecast?.inventory_position),forecast?.daily_velocity==null?'Unknown':number(Number(forecast.daily_velocity)*7),velocity(forecast?.daily_velocity),number(forecast?.reorder_level),number(forecast?.target_level),number(forecast?.raw_need)].forEach(value=>tr.append(el('td',{},value)));
      tr.append(el('td',{'data-planned-store':store.id},number(item.allocations[store.id])));body.append(tr);
      if(forecast){const note=el('tr');note.append(el('td',{colspan:11,class:'purchasing-detail-note'},`${store.name}: ${forecast.trend} · ${forecast.evidence_days} completed days · ${forecast.confidence} confidence${obs?.stockout_evidence_reliable?' · qualified stockout history':' · stockout history unavailable'}${forecast.reasons?.length?' · '+forecast.reasons.join(', '):''}`));body.append(note);}
    }
    table.append(body);const calculationScroll=el('div',{class:'purchasing-calculation-scroll'});calculationScroll.append(table);
    evidence.append(calculationScroll,el('p',{class:'purchasing-detail-note'},`Saved calculation: position = Square on-hand + incoming; at or below the reorder threshold, need = ceiling(max(target − position, 0)); otherwise 0. Threshold = ceiling(adjusted daily velocity × max(${meta.settings.reorder_weeks} × 7, lead time)); target uses max(${meta.settings.target_weeks} × 7, reorder days). Supplier lead time: ${item.supplier_lead_time_days==null?'not established':item.supplier_lead_time_days+' days'}. Displayed daily velocity is rounded to four decimals; weekly velocity is approximate. The engine uses its unrounded daily value.`),
      el('p',{class:'purchasing-detail-note'},'Velocity uses up to 56 completed days, spike caps, qualified stockout adjustments and sustained trends; limited history is reduced conservatively. Current Square reads do not recalculate saved recommendations or change your plan. Incoming is a separate outstanding PO commitment. Historical receipts never establish current on-hand.'));

    const allocationNote=el('p',{class:'purchasing-allocation-note'});
    const allocationSum=()=>Object.values(allocationInputs).reduce((sum,input)=>sum+Number(input.value),0);
    function allocationMessage(){
      qty.value=String(allocationSum());qty.textContent=number(allocationSum());
      for(const [sid,input]of Object.entries(allocationInputs)){const cell=detailCell.querySelector(`[data-planned-store="${sid}"]`);if(cell)cell.textContent=input.value===''?'—':number(input.value);}
      allocationNote.textContent=allocationMismatch
        ?`Saved order quantity ${number(item.quantity)} differs from saved allocation total ${number(allocationSum())}. Editing is blocked pending explicit reconciliation; no saved decisions were changed.`
        :`Total ${number(allocationSum())} = intended store allocations. Original calculated need: ${number(item.need)}. Saved engine allocations include applicable pack rounding; editing stores establishes your manual plan.`;
    }
    allocationMessage();calculation.append(evidence);detailCell.append(calculation,allocationNote);
    if(item.configured_cost!=null&&Number(item.configured_cost)!==Number(item.cost)){detailCell.append(el('p',{class:'purchasing-detail-note'},`Configured cost is now ${money(item.configured_cost)}; saved draft cost is ${money(item.cost)}.`),button('Use current configured cost',()=>enqueue({kind:'line',line_id:item.id,preview:original,changes:{},label:item.sku||item.name})));}
    if(item.proposal){const changed=el('p',{class:'purchasing-detail-note'},`Updated recommendation: ${number(item.proposal.quantity)}. Saved need ${number(item.need)} and your order ${number(item.quantity)} stay unchanged until you accept.`);
      detailCell.append(changed,button('Accept Updated Quantity',()=>enqueue({kind:'line',line_id:item.id,preview:original,changes:{accept_proposal:true},label:item.sku||item.name})));}
    if(item.review.length){detailCell.append(el('p',{class:'purchasing-detail-note'},item.review.join('; ')));}
    if(item.needs_review){const label=el('label',{},'I reviewed this uncertainty and choose this quantity and cost.');const check=el('input',{type:'checkbox','aria-label':`Confirm uncertainty ${item.sku||item.name}`});label.prepend(check);detailCell.append(label);check.addEventListener('change',()=>enqueue({kind:'line',line_id:item.id,preview:original,changes:{acknowledge:check.checked,...((cost?cost.value:item.cost)!=null&&(cost?cost.value:item.cost)!==''?{cost:cost?cost.value:item.cost}:{})},label:item.sku||item.name}));saved.textContent='Review evidence';}
    function edit(fields){
      const prior=queue.find(op=>!op.sent&&op.kind==='line'&&op.line_id===item.id);
      const changes={...(prior?.changes||{}),...fields};let invalid;
      if(prior?.changes.acknowledge===true&&Object.keys(fields).some(key=>['quantity','allocations','supplier_id','cost'].includes(key)))changes.acknowledge=false;
      if ('quantity' in changes && (!Number.isInteger(changes.quantity)||changes.quantity<0||changes.quantity>1000000)) invalid='The allocation total must be a whole number from 0 to 1,000,000.';
      if (cost && 'cost' in changes && (!cost.validity.valid||cost.value==='')) invalid='Enter and confirm a valid nonnegative purchase unit cost.';
      if (item.decisions.allocation_override || 'allocations' in changes) {
        changes.allocations=Object.fromEntries(Object.entries(allocationInputs).map(([id,input])=>[id,input.value===''?'':Number(input.value)]));
        if(Object.values(allocationInputs).some(input=>!input.validity.valid||input.value==='')||allocationSum()!==Number(changes.quantity??item.quantity))invalid='Enter nonnegative whole store quantities; their total must be at most 1,000,000. Your edits remain on this device until valid.';
      }
      allocationMessage();enqueue({kind:'line',line_id:item.id,preview:original,changes,label:item.sku||item.name,invalid});
    }
    supplier.addEventListener('change',()=>{const selected=Number(supplier.value);supplier.blur();edit({supplier_id:selected,quantity:allocationSum(),allocations:Object.fromEntries(Object.entries(allocationInputs).map(([id,input])=>[id,input.value===''?'':Number(input.value)]))});renderWorkingGroups();});
    if(cost)cost.addEventListener('input',()=>edit({cost:cost.value}));
    for(const input of Object.values(allocationInputs))input.addEventListener('input',()=>{
      item.decisions.allocation_override=true;allocationMessage();rowState(item.id,'Unsaved','saving');
      const changes={quantity:allocationSum(),allocations:Object.fromEntries(Object.entries(allocationInputs).map(([id,field])=>[id,field.value===''?'':Number(field.value)]))};
      edit(changes);
    });
    const pending=queue.filter(op=>op.line_id===item.id&&op.kind==='line').reduce((all,op)=>({...all,...op.changes}),{});
    if('quantity' in pending&&!pending.allocations)detailCell.append(el('p',{class:'v2-callout'},'An earlier unsaved order-total edit is retained for review. It has not been converted into a new store allocation.'));
    if('supplier_id' in pending)supplier.value=pending.supplier_id;
    if('cost' in pending&&cost)cost.value=pending.cost;
    if(pending.allocations)for(const[id,value]of Object.entries(pending.allocations))if(allocationInputs[id])allocationInputs[id].value=value;
    if(Object.keys(pending).length){rowState(item.id,'Unsaved','error');allocationMessage();}
    return [row,detail];
  }
  async function loadGroup(ctrl,local=false) {
    const requestId=(ctrl.requestId||0)+1;ctrl.requestId=requestId;
    try {
      const selected=params(ctrl.group.id,ctrl.page);if(ctrl.category)selected.set('category',ctrl.category);
      const saved=local&&ctrl.data?ctrl.data:await get(`${base}/workspace?${selected}`);ctrl.data=saved;
      const data=workingData(saved,ctrl);
      if(requestId!==ctrl.requestId)return;
      const expanded=new Set([...ctrl.body.querySelectorAll('[data-detail-id]')].filter(node=>!node.hidden).map(node=>node.dataset.detailId));
      ctrl.page=data.page;ctrl.loaded=true;ctrl.body.replaceChildren();
      if(ctrl.group.status==='CONVERTED'){
        const panel=el('div',{class:'purchasing-group-actions'});appendPdf(panel,ctrl.group);panel.append(el('a',{href:`/management/ordering-tool/orders/${ctrl.group.po_id}`,class:'v2-button v2-button--secondary v2-button--compact'},`Open PO ${ctrl.group.po_id}`));ctrl.body.append(panel);appendAccount(panel,ctrl.group);return;
      }
      appendCategories(ctrl,data);
      const scroll=el('div',{class:'purchasing-table-scroll',tabindex:0,'aria-label':`${ctrl.group.name} recommendations, scroll horizontally for all columns`});
      const table=el('table',{class:'v2-table purchasing-table'});table.append(el('caption',{},`${ctrl.group.name} saved purchasing recommendations`));
      const head=el('thead');const tr=el('tr');tr.append(el('th',{scope:'col'},'Product / SKU'));
      for(const store of tableStores()){const heading=el('th',{scope:'col',class:'purchasing-store'},store.name);heading.append(el('small',{},'Stock / Order'));tr.append(heading);}
      for(const [name,cls]of [['Total','purchasing-qty'],['Unit Cost','purchasing-cost'],['Supplier','purchasing-selected-supplier'],['Remove','']])tr.append(el('th',{scope:'col',class:cls},name));
      head.append(tr);table.append(head);const tbody=el('tbody');
      appendCategoryRows(tbody,data,ctrl,expanded);

      if(!data.rows.length){
        const emptyRow=el('tr');
        emptyRow.append(el('td',{colspan:tableStores().length+5,class:'v2-table__empty'},'No products match these filters.'));
        tbody.append(emptyRow);
      }
      table.append(tbody);scroll.append(table);ctrl.body.append(scroll);
      const pager=el('div',{class:'purchasing-pagination'});pager.append(el('span',{},`${data.total?((data.page-1)*data.page_size+1):0}–${Math.min(data.page*data.page_size,data.total)} of ${number(data.total)} matching products · page ${data.page} / ${data.pages}`));
      const actions=el('span');const previous=button('Previous',async()=>{if(await flush()){ctrl.page--;await loadGroup(ctrl);}});previous.disabled=data.page<=1;
      const next=button('Next',async()=>{if(await flush()){ctrl.page++;await loadGroup(ctrl);}});next.disabled=data.page>=data.pages;actions.append(previous,next);pager.append(actions);ctrl.body.append(pager);
      const footer=el('div',{class:'purchasing-group-actions'});appendAccount(footer,ctrl.group);
      const groupActions=el('div',{class:'purchasing-controls'});
      appendPdf(groupActions,ctrl.group);
      if(meta.status==='ACTIVE'&&ctrl.group.id){
        const create=button('Review / Finalize PO',()=>showFinalReview(ctrl.group.id),'v2-button v2-button--secondary v2-button--compact');
        create.title='Review all saved quantities for this supplier. Saving a draft never creates a purchase order.';
        create.disabled=ctrl.group.status!=='OPEN'||ctrl.group.units<=0;
        groupActions.append(create);
        groupActions.append(actionForm(`${base}/groups/${ctrl.group.id}/discard`,'Discard Group','v2-button v2-button--secondary v2-button--compact'));
      }
      const inventoryStatus=el('span',{class:'purchasing-inventory-status','data-inventory-status':'','aria-live':'polite'},'Reading current Square inventory…');
      const refreshInventory=button('Refresh inventory',()=>loadInventory(ctrl,data.rows,requestId,true));
      groupActions.append(refreshInventory);footer.append(groupActions);ctrl.body.prepend(footer);
      const inventoryBar=el('div',{class:'purchasing-inventory-bar'});inventoryBar.append(inventoryStatus);footer.after(inventoryBar);
      void loadInventory(ctrl,data.rows,requestId);
      ctrl.body.append(el('p',{class:'purchasing-evidence v2-muted',style:'padding:0 12px'},'Intended account and purchasing totals are estimates; no charge, payment or inventory movement is recorded.'));
    } catch(error){ctrl.loaded=false;ctrl.body.replaceChildren(el('p',{class:'v2-callout'},error.message),button('Retry table',()=>loadGroup(ctrl)));}
  }
  async function showFinalReview(supplierId) {
    if(busy)return;
    finalReviewSupplier=supplierId;
    const holder=root.querySelector('[data-finalize-review]');
    holder.className='purchasing-finalize-review';holder.replaceChildren(el('p',{},'Checking saved draft readiness…'));
    try{renderFinalReview(await get(`${base}/groups/${supplierId}/review`));}
    catch(error){holder.replaceChildren(el('p',{role:'alert'},error.message),button('Retry review',()=>showFinalReview(supplierId)));}
    holder.scrollIntoView({block:'start'});
  }
  function renderFinalReview(review,message='') {
    const holder=root.querySelector('[data-finalize-review]');holder.replaceChildren();
    holder.append(el('h3',{},`Review ${meta.vendors.find(v=>v.id===review.supplier_id)?.name||'supplier'} purchase order`),
      el('p',{},`${review.product_count} saved products · ${number(review.units)} units · ${money(review.cost)}`),
      el('p',{class:'v2-muted'},'Save Draft stores unfinished decisions. Finalization includes this supplier’s entire saved draft, including products outside current filters. Create the PO, then review and submit it to generate its existing vendor PDF.'));
    if(review.po_id)holder.append(el('a',{href:`/management/ordering-tool/orders/${review.po_id}`,class:'v2-button v2-button--secondary'},`Open existing PO ${review.po_id}`));
    if(message)holder.append(el('p',{role:'alert'},message));
    if(queue.length)holder.append(el('p',{class:'v2-callout'},'Unsaved changes are retained. Save Draft, then review the updated saved quantities before finalizing.'));
    for(const issue of review.issues)holder.append(el('p',{class:'v2-callout'},issue));
    for(const item of review.items){
      const row=el('div',{class:'purchasing-finalize-item'});
      row.append(el('strong',{},`${item.name} · ${item.sku||'No SKU'} · ${number(item.quantity)} units · ${money(item.cost)}`));
      for(const reason of item.reasons)row.append(el('p',{},reason));
      row.append(el('p',{class:'v2-muted'},Object.entries(item.allocations).map(([id,qty])=>`${review.stores[id]||id}: ${number(qty)}`).join(' · ')));
      const staged=queue.some(op=>op.line_id===item.id&&op.changes.acknowledge===true);
      if(item.can_confirm){
        const confirm=button(staged?'Confirmation staged — Save Draft':'I reviewed this quantity, cost and evidence',()=>{
          enqueue({kind:'line',line_id:item.id,preview:item.preview,changes:{acknowledge:true,cost:item.cost},label:item.sku||item.name});
          confirm.textContent='Confirmation staged — Save Draft';confirm.disabled=true;
        });confirm.disabled=staged;row.append(confirm);
      }
      row.append(button('Review / edit product',async()=>{
        filters.elements.supplier.value=String(review.supplier_id);filters.elements.state.value='all';filters.elements.q.value=item.sku||item.name;
        expandedReviewLines.add(item.id);syncGroups();const ctrl=controllers.get(review.supplier_id);if(ctrl)ctrl.details.open=true;
        await applyFilters();ctrl?.details.scrollIntoView({block:'nearest'});
      }));holder.append(row);
    }
    const actions=el('div',{class:'purchasing-controls'});
    const finalize=button('Finalize / Create PO',async()=>{
      if(queue.length||pendingBatch){renderFinalReview(review,'Save Draft before creating this purchase order.');return;}
      finalize.disabled=true;
      const form=new FormData();form.set('csrf_token',root.dataset.csrf);form.set('version',review.version);
      try{
        const response=await fetch(`${base}/groups/${review.supplier_id}/po`,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json'},body:form});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Creation could not be confirmed. Review existing purchase orders before retrying.');
        const data=await response.json();if(!response.ok){if(data.review)renderFinalReview(data.review,data.detail);else throw new Error(data.detail||'Review the saved draft.');return;}
        location.assign(data.redirect);
      }catch(error){renderFinalReview(review,error.message);}
    },'v2-button v2-button--primary');
    finalize.setAttribute('data-finalize-submit','');finalize.disabled=!review.ready||queue.length>0;
    actions.append(button('Save Draft',()=>void pump()),button('Recheck saved draft',()=>showFinalReview(review.supplier_id)),finalize,button('Close review',()=>{finalReviewSupplier=null;holder.className='';holder.replaceChildren();}));holder.append(actions);
  }
  function appendPdf(holder,group) {
    if(group.pdf_available) {
      holder.append(el('a',{href:`/management/ordering-tool/orders/${group.po_id}/pdf`,target:'_blank',rel:'noopener',class:'v2-button v2-button--primary v2-button--compact'},'Print / Export PDF'));
    } else {
      const disabled=el('button',{type:'button',disabled:true,class:'v2-button v2-button--secondary v2-button--compact'},'Print / Export PDF');
      const reason=!group.po_id?'Create a PO, then review and submit it to generate the vendor PDF.':group.po_status==='DRAFT'?'Review and submit this PO to generate the vendor PDF.':'Original PDF unavailable; restore it from backup.';
      disabled.title=reason;holder.append(disabled,el('span',{class:'purchasing-pdf-note'},reason));
    }
  }
  function appendCategories(ctrl,data) {
    const nav=el('nav',{class:'purchasing-category-nav','aria-label':`Item types for ${ctrl.group.name}`});
    const selectCategory=async id=>{
      if(!await flush())return;
      ctrl.category=id;ctrl.page=1;
      if(id)ctrl.collapsed?.delete(id);
      await loadGroup(ctrl);
    };
    const all=button(`All item types (${(data.categories||[]).reduce((sum,c)=>sum+c.count,0)})`,()=>selectCategory(''));
    all.setAttribute('aria-pressed',String(!ctrl.category));nav.append(all);
    for(const category of data.categories||[]) {
      const choice=button(`${category.name} (${category.count})`,()=>selectCategory(category.id));
      choice.setAttribute('aria-pressed',String(ctrl.category===category.id));
      if(category.issue)choice.title=category.issue;
      nav.append(choice);
    }
    ctrl.body.append(nav);
    if(data.category_issue)ctrl.body.append(el('p',{class:'v2-callout'},data.category_issue));
    if(ctrl.category)ctrl.body.append(el('p',{class:'purchasing-category-context'},'Showing one item type. Search covers all item types; choose All item types to browse every match.'));
  }
  function appendCategoryRows(tbody,data,ctrl,expanded) {
    ctrl.collapsed ||= new Set();
    const savedCollapsed=ctrl.collapsed;
    const collapsedTypes=filters.elements.q.value.trim() ? new Set() : savedCollapsed;
    const grouped=new Map();
    for(const item of data.rows){
      const category=item.item_type||{id:'unavailable',name:'Inventory category unavailable'};
      if(!grouped.has(category.id))grouped.set(category.id,{category,items:[]});
      grouped.get(category.id).items.push(item);
    }
    for(const {category,items} of [...grouped.values()].sort((a,b)=>a.category.name.localeCompare(b.category.name))) {
      const heading=el('tr',{class:'purchasing-category-heading'});
      const cell=el('th',{colspan:tableStores().length+5,scope:'rowgroup'});
      const rows=[];const searching=Boolean(filters.elements.q.value.trim());const collapsed=!searching&&collapsedTypes.has(category.id);

      const count=(data.categories||[]).find(c=>c.id===category.id)?.count??items.length;
      const toggle=button('',()=>{
        const close=!collapsedTypes.has(category.id);
        if(close)collapsedTypes.add(category.id);else collapsedTypes.delete(category.id);
        for(const [row,detail,detailOpen]of rows){row.hidden=close;detail.hidden=close||!detailOpen();}
        updateToggle();
      },'purchasing-category-toggle');
      function updateToggle(){const open=!collapsedTypes.has(category.id);toggle.textContent=`${open?'⌄':'›'} ${category.name} · ${count} matching products${items.length<count?` · ${items.length} on this page`:''}`;toggle.setAttribute('aria-expanded',String(open));}
      updateToggle();cell.append(toggle);
      if(category.issue)cell.append(el('small',{},category.issue));
      heading.append(cell);tbody.append(heading);
      for(const item of items){
        const original=ctrl.data.rows.find(row=>row.id===item.id)||pendingLines().find(row=>row.id===item.id)||item;
        const pair=rowPair(original);
        const expand=pair[0].querySelector('[aria-expanded]');
        if(expanded.has(String(item.id))||expandedReviewLines.has(item.id)){pair[1].hidden=false;expand.setAttribute('aria-expanded','true');expand.textContent='⌄';}
        rows.push([pair[0],pair[1],()=>expand?.getAttribute('aria-expanded')==='true']);
        if(collapsed){pair[0].hidden=true;pair[1].hidden=true;}
        tbody.append(...pair);
      }
    }
  }
  async function loadInventory(ctrl,rows,requestId,force=false) {
    const inventoryRequest=(ctrl.inventoryRequest||0)+1;ctrl.inventoryRequest=inventoryRequest;
    const statusNode=ctrl.body.querySelector('[data-inventory-status]');
    if(!rows.length){if(statusNode)statusNode.textContent='No visible inventory rows.';return;}
    if(statusNode)statusNode.textContent='Reading current Square inventory…';
    try {
      let data;
      if(!force&&rows.every(row=>inventoryCache.has(row.id)&&Date.now()-inventoryCache.get(row.id).at<60000)){
        data={rows:Object.fromEntries(rows.map(row=>[String(row.id),inventoryCache.get(row.id).counts])),read_at:inventoryCache.get(rows[0].id).read_at};
      }else{
        const replies=await Promise.all(Array.from({length:Math.ceil(rows.length/100)},(_,i)=>get(`${base}/inventory?line_ids=${rows.slice(i*100,(i+1)*100).map(row=>row.id).join(',')}`)));
        data={rows:Object.assign({},...replies.map(reply=>reply.rows)),read_at:replies[0].read_at};
        for(const row of rows)inventoryCache.set(row.id,{counts:data.rows[String(row.id)],at:Date.now(),read_at:data.read_at});
      }
      if(requestId!==ctrl.requestId||inventoryRequest!==ctrl.inventoryRequest)return;
      let unknown=0;
      for(const item of rows)for(const store of tableStores()){
        const count=data.rows[String(item.id)]?.[store.id];if(count?.quantity==null)unknown++;
        const title=`Square read: ${timestamp(count?.read_at||data.read_at)}. Count effective: ${timestamp(count?.calculated_at)}.${count?.problem?' '+count.problem:''}`;
        for(const node of ctrl.body.querySelectorAll(`[data-live-hand="${item.id}:${store.id}"]`)){
          node.textContent=count?.quantity==null?'Unknown':number(count.quantity);node.title=title;
          node.classList.toggle('purchasing-inventory-unknown',count?.quantity==null);
        }
      }
      if(statusNode)statusNode.textContent=`Square inventory read ${timestamp(data.read_at)}${unknown?` · ${unknown} unknown counts`:''}. Stock / Order = current on-hand / intended allocation. Saved recommendations unchanged.`;
    } catch(error) {
      if(requestId!==ctrl.requestId||inventoryRequest!==ctrl.inventoryRequest)return;
      for(const node of ctrl.body.querySelectorAll('[data-live-hand]')){node.textContent='Unknown';node.title='Current Square inventory could not be read; no saved values changed.';node.classList.add('purchasing-inventory-unknown');}
      if(statusNode)statusNode.textContent='Current Square inventory unavailable. Retry Refresh inventory; saved recommendations and allocations are unchanged.';
    }
  }
  function appendAccount(holder,group){
    if(!group.id)return;
    const label=el('label',{},'Intended payment account');const select=el('select',{'aria-label':`Intended payment account ${group.name}`});select.append(el('option',{value:''},'Not selected'));
    for(const account of meta.accounts)select.append(el('option',{value:account.id},account.name));const accountPending=queue.filter(op=>op.kind==='account'&&op.supplier_id===group.id).at(-1);select.value=String(accountPending?accountPending.changes.account_id:(group.account_id||''));label.append(select);
    select.addEventListener('change',()=>enqueue({kind:'account',supplier_id:group.id,changes:{account_id:select.value},label:group.name}));holder.append(label);
  }
  function actionForm(action,label,cls){const form=el('form',{method:'post',action,'data-draft-action':''});form.append(el('input',{type:'hidden',name:'csrf_token',value:root.dataset.csrf}),el('input',{type:'hidden',name:'version',value:version}),el('button',{class:cls},label));return form;}
  filters.addEventListener('submit',event=>event.preventDefault());
  async function applyFilters(){if(!await flush())return;for(const ctrl of controllers.values())ctrl.category='';await refreshMeta();for(const ctrl of controllers.values()){ctrl.page=1;ctrl.loaded=false;}await refreshOpen();}
  filters.addEventListener('input',event=>{if(event.target.name==='q'){clearTimeout(filterTimer);filterTimer=setTimeout(applyFilters,300);}});
  filters.addEventListener('change',event=>{if(event.target.name!=='q')void applyFilters();});
  saveButton.addEventListener('click',()=>void pump());
  root.querySelector('[data-show-review]').addEventListener('click',async()=>{filters.elements.state.value='review';filters.elements.supplier.value='';filters.elements.q.value='';await applyFilters();for(const ctrl of controllers.values())if(!ctrl.details.hidden)ctrl.details.open=true;});
  root.addEventListener('submit',async event=>{const form=event.target;if(!form.matches('[data-draft-action]'))return;event.preventDefault();if(queue.length||pendingBatch){showError('Save Draft before this action. Saving does not create purchase orders.');saveButton.focus();return;}if(await flush())HTMLFormElement.prototype.submit.call(form);});
  window.addEventListener('beforeunload',event=>{if(queue.length||busy||timers.size){persist();event.preventDefault();event.returnValue='';}});
  root.addEventListener('focusout',()=>{if(refreshNeeded&&!queue.length&&!busy&&!timers.size)setTimeout(()=>{if(!root.querySelector('.purchasing-table input:focus,.purchasing-table select:focus')){refreshNeeded=false;void refreshOpen();}},50);});
  errorBox.querySelector('[data-retry]').addEventListener('click',async()=>{conflict=false;paused=false;if(!queue.length){try{await refreshMeta();await refreshOpen();clearError();draftStatus();}catch(error){showError(error.message);}return;}void pump();});
  errorBox.querySelector('[data-reapply]').addEventListener('click',async()=>{
    try{const data=await get(`${base}/draft`);version=data.version;pendingBatch=null;conflict=false;paused=false;for(const op of queue){op.inflight=false;op.sent=false;}persist();clearError();void pump();}catch(error){showError(error.message);}
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
      if(!queue.length){version=data.version;pendingBatch=null;conflict=false;paused=false;persist();clearError();draftStatus();await refreshMeta();await refreshOpen();}
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
    if(saved?.queue?.length){queue=saved.queue.map(op=>({...op,inflight:false}));pendingBatch=saved.pendingBatch||null;paused=true;conflict=!pendingBatch;
      showError(pendingBatch?'A draft save was interrupted. Retry to confirm its result without applying it twice.':'Unsaved decisions were recovered from this device. Review them against the saved session before reapplying.');void reviewConflict();}
  } catch(_){showError('Saved local draft could not be read. Keep this page open while checking browser storage.');}
  syncGroups();draftStatus();
})();
