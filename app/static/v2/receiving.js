/* A count is a protected browser draft. Only Submit to Stores creates delivery evidence and stock commands. */
(() => {
  'use strict';
  const root=document.querySelector('#purchasing-receiving');if(!root)return;
  const data=JSON.parse(document.querySelector('#receiving-bootstrap').textContent);
  const base=`/management/purchasing/orders/${data.po_id}`;
  const key=`erupted-receiving:${data.owner_id}:${data.po_id}`;
  const state=root.querySelector('[data-count-state]'),result=root.querySelector('[data-result]');
  const panel=root.querySelector('[data-count-panel]'),submit=root.querySelector('[data-submit]'),openCount=root.querySelector('[data-open-count]');
  const storage=()=>JSON.parse(localStorage.getItem(key)||'null');
  let draft={request_key:crypto.randomUUID(),cells:{},stamp:null},blocked=false,busy=false,page=1;
  const show=(message,error=false)=>{result.hidden=false;result.textContent=message;result.classList.toggle('purchasing-unsaved',error);};
  const el=(tag,text='',attrs={})=>{const node=document.createElement(tag);node.textContent=text;for(const[k,v]of Object.entries(attrs))node.setAttribute(k,String(v));return node;};
  try {const saved=storage();if(saved){draft=saved;if(panel)panel.hidden=false;}} catch(_){blocked=true;show('This browser cannot restore the receiving count. Check browser storage before entering quantities.',true);}
  if(openCount&&panel)openCount.hidden=!panel.hidden;
  let confirmation=null;
  const confirmationBox=root.querySelector('[data-confirmation]');
  function dismissConfirmation(){confirmation=null;confirmationBox.hidden=true;}
  function confirmAction(action,message,label){confirmation=action;confirmationBox.hidden=false;confirmationBox.querySelector('[data-confirmation-message]').textContent=message;confirmationBox.querySelector('[data-confirm-action]').textContent=label;}
  root.querySelector('[data-cancel-confirmation]').addEventListener('click',dismissConfirmation);
  document.addEventListener('keydown',event=>{if(event.key==='Escape')dismissConfirmation();});
  root.querySelector('[data-confirm-action]').addEventListener('click',()=>{const action=confirmation;dismissConfirmation();if(action)action();});
  function persist(){
    try {
      if(blocked)return false;
      const saved=storage();
      if((saved&&saved.stamp!==draft.stamp)||(!saved&&draft.stamp)){blocked=true;show('Another tab changed this count. Reload to review the saved quantities before submitting.',true);return false;}
      draft.stamp=crypto.randomUUID();localStorage.setItem(key,JSON.stringify(draft));
      state.textContent='Count saved on this device · not submitted';return true;
    } catch(_){blocked=true;show('Count could not be retained on this device. Keep this page open and restore browser storage before submitting.',true);return false;}
  }
  const number=value=>new Intl.NumberFormat('en-US').format(value);
  function values(row){return Object.values(draft.cells[row.id]||{});}
  function valid(row){return values(row).every(value=>value===''||/^\d+$/.test(value)&&Number.isSafeInteger(Number(value))&&Number(value)<=1000000);}
  function sum(row){return values(row).reduce((total,value)=>total+(value!==''&&/^\d+$/.test(value)?Number(value):0),0);}
  function totals(){
    const counted=data.rows.reduce((total,row)=>total+sum(row),0);
    const invalid=data.rows.some(row=>!valid(row)||sum(row)>row.outstanding);
    if(submit)submit.disabled=busy||blocked||invalid;
    if(root.querySelector('[data-live-totals]'))root.querySelector('[data-live-totals]').textContent=`New received ${number(counted)} · Remaining ${number(data.outstanding-counted)}`;
    return !invalid;
  }
  function rowTotals(row,tr){
    const count=sum(row),invalid=!valid(row)||count>row.outstanding;
    tr.querySelector('[data-new]').textContent=number(count);tr.querySelector('[data-remaining]').textContent=number(row.outstanding-count);
    tr.classList.toggle('receiving-invalid',invalid);
    tr.querySelector('[data-remaining]').title=invalid?'Enter nonnegative whole quantities within the outstanding balance':'';
    totals();
  }
  function render(){
    if(!panel)return;
    const query=root.querySelector('[data-search]').value.toLowerCase().trim();
    const rows=data.rows.filter(row=>`${row.name} ${row.sku||''}`.toLowerCase().includes(query));
    const pages=Math.max(1,Math.ceil(rows.length/50));page=Math.min(page,pages);
    const body=root.querySelector('[data-count-rows]');body.replaceChildren();
    for(const row of rows.slice((page-1)*50,page*50)){
      const tr=el('tr','',{'data-receiving-line':row.id});const name=el('td');name.append(el('strong',row.name),el('small',`${row.sku||'No SKU'} · ${row.variant}`,{class:'receiving-reference'}));tr.append(name);
      for(const field of ['ordered','previous','outstanding'])tr.append(el('td',number(row[field]),{class:'purchasing-numeric'}));
      for(const store of data.stores){
        const cell=el('td','',{class:'purchasing-numeric'}),ref=row.stores[store.id];
        const input=el('input','',{type:'number',min:0,max:row.outstanding,step:1,class:'purchasing-store-quantity','aria-label':`New received ${store.name} ${row.sku||row.name}`,placeholder:'—'});
        input.value=draft.cells[row.id]?.[store.id]??'';input.disabled=busy||blocked||!ref||!store.active||!row.outstanding;
        if(ref)input.title=`Intended ${ref.intended}; previously received ${ref.previous}. Enter only this shipment.`;
        cell.append(input,el('small',ref?`${number(ref.intended)} / ${number(ref.previous)}`:'No allocation',{class:'receiving-reference',title:'Intended / previously received'}));tr.append(cell);
        input.addEventListener('input',()=>{if(!draft.cells[row.id])draft.cells[row.id]={};draft.cells[row.id][store.id]=input.value;persist();rowTotals(row,tr);});
      }
      tr.append(el('td','',{class:'purchasing-numeric','data-new':''}),el('td','',{class:'purchasing-numeric','data-remaining':''}));body.append(tr);rowTotals(row,tr);
    }
    root.querySelector('[data-page]').textContent=`Page ${page} / ${pages} · ${number(rows.length)} products`;
    root.querySelector('[data-previous]').disabled=page<=1;root.querySelector('[data-next]').disabled=page>=pages;totals();
    if(Object.keys(draft.cells).length)state.textContent='Restored count on this device · not submitted';
  }
  async function post(path,body){
    const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':root.dataset.csrf,'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify(body)});
    if(response.redirected||!response.headers.get('content-type')?.includes('application/json'))throw new Error('Session or connection could not be confirmed. Your count is retained; sign in and retry this same shipment.');
    const value=await response.json();if(!response.ok)throw new Error(value.detail||'Submission failed. Retry this same count.');return value;
  }
  if(panel){
    openCount?.addEventListener('click',()=>{panel.hidden=false;openCount.hidden=true;render();});
    root.querySelector('[data-clear-count]').addEventListener('click',()=>{
      if(busy)return;
      confirmAction(()=>{
        try{
          const saved=storage();
          if((saved&&saved.stamp!==draft.stamp)||(!saved&&draft.stamp)){blocked=true;show('This count changed in another tab. Reload before clearing; the newer count was retained.',true);totals();return;}
          localStorage.removeItem(key);location.reload();
        }catch(_){show('Browser storage could not be cleared. Keep this page open and review receiving history.',true);}
      },'Clear only this PO’s unsubmitted count on this device? Saved shipments and other PO drafts remain unchanged.','Confirm clear count');
    });
    root.querySelector('[data-search]').addEventListener('input',()=>{page=1;render();});
    root.querySelector('[data-previous]').addEventListener('click',()=>{page--;render();});root.querySelector('[data-next]').addEventListener('click',()=>{page++;render();});
    submit.addEventListener('click',async()=>{
      if(busy||blocked||!totals()||!persist())return;
      const cells=[];
      for(const row of data.rows)for(const[sid,value]of Object.entries(draft.cells[row.id]||{}))if(value!=='')cells.push([row.id,Number(sid),Number(value)]);
      if(!cells.length){show('Enter actual store quantities. An explicit zero is allowed; ordered allocations are never assumed received.',true);return;}
      busy=true;render();state.textContent='Submitting shipment…';
      try {
        const response=await post(`${base}/shipments`,{request_key:draft.request_key,cells});
        localStorage.removeItem(key);
        sessionStorage.setItem(key+':result',JSON.stringify(response));
        location.assign(`${base}/receiving?shipment=${response.receipt_id}`);
      } catch(error){show(error.message,true);state.textContent='Unconfirmed — count retained; retry the same shipment';busy=false;render();}
    });
    render();
  }
  for(const row of root.querySelectorAll('[data-history-summary]')){
    const button=row.querySelector('button'),detail=root.querySelector(`[data-history-detail="${row.dataset.historySummary}"]`);
    const toggle=()=>{detail.hidden=!detail.hidden;button.setAttribute('aria-expanded',String(!detail.hidden));button.textContent=detail.hidden?'›':'⌄';};
    button.addEventListener('click',event=>{event.stopPropagation();toggle();});
    row.addEventListener('click',event=>{if(!event.target.closest('button,a'))toggle();});
  }
  for(const node of root.querySelectorAll('[data-history-line]')){const row=data.rows.find(row=>row.id===Number(node.dataset.historyLine));if(row)node.textContent=`${row.sku||''} ${row.name}`;}
  for(const node of root.querySelectorAll('[data-history-store]'))node.textContent=data.stores.find(store=>store.id===node.dataset.historyStore)?.name||`Store ${node.dataset.historyStore}`;
  for(const button of root.querySelectorAll('[data-retry-shipment]'))button.addEventListener('click',async()=>{
    button.disabled=true;
    try {const response=await post(`${base}/shipments/${button.dataset.retryShipment}/retry`,{});sessionStorage.setItem(key+':result',JSON.stringify(response));location.reload();}
    catch(error){show(error.message,true);button.disabled=false;}
  });
  const close=root.querySelector('[data-close-form]');if(close)close.addEventListener('submit',event=>{
    if(data.rows.some(row=>values(row).some(value=>value!==''))||busy){event.preventDefault();show('Finish or clear this unsubmitted count before explicitly closing the shortage.',true);return;}
    event.preventDefault();
    confirmAction(()=>{
      if(blocked||busy||data.rows.some(row=>values(row).some(value=>value!==''))){show('The receiving count changed. Review it before closing shortages.',true);return;}
      HTMLFormElement.prototype.submit.call(close);
    },'Close all outstanding quantities with shortage? No stock will be added.','Confirm Close with Shortage');
  });
  window.addEventListener('beforeunload',event=>{if(blocked&&data.rows.some(row=>values(row).some(value=>value!==''))){event.preventDefault();event.returnValue='';}});
  window.addEventListener('storage',event=>{if(event.key===key){blocked=true;show('This count changed in another tab. Reload before continuing; no quantities will be submitted from this stale page.',true);totals();}});
  try {const last=JSON.parse(sessionStorage.getItem(key+':result')||'null');if(last){show(last.failed||last.uncertain?'Shipment counted. Some store updates failed or are uncertain; recover the same shipment from history.':`Shipment recorded. ${last.succeeded||0} new store updates confirmed; prior quantities were not submitted again.`,Boolean(last.failed||last.uncertain));sessionStorage.removeItem(key+':result');}}catch(_){}
})();
