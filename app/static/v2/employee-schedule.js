(() => {
  const root=document.querySelector('[data-employee-schedule]');if(!root)return;
  const status=root.querySelector('[data-request-status]'),error=root.querySelector('[data-request-error]');
  let pending=false;
  const dialog=root.querySelector('[data-employee-action-dialog]');let actionDetails=null;
  const showError=message=>{const target=dialog.open?dialog.querySelector('[data-action-error]'):(root.querySelector('[data-requests-dialog][open] [data-request-dialog-error]')||error);target.textContent=message;target.hidden=false;target.scrollIntoView({block:'nearest'});};
  function closeAction(){
    const form=dialog.querySelector('form');if(form&&actionDetails){actionDetails.append(form);actionDetails.open=false;}
    dialog.close();actionDetails=null;
  }
  root.querySelector('[data-action-close]').addEventListener('click',()=>{if(!pending)closeAction();});
  dialog.addEventListener('cancel',event=>{event.preventDefault();if(!pending)closeAction();});
  function applyResult(result){
      const list=root.querySelector('[data-request-list]');list.querySelector('[data-no-requests]')?.remove();
      const previous=list.querySelector(`[data-request-id="${result.request_id}"]`);
      const previousState=previous?.dataset.requestState?.split(':')[0];
      if(previous && (Date.parse(previous.dataset.updatedAt)>Date.parse(result.updated_at) ||
          ['COMPLETED','CANCELLED','DECLINED','REJECTED'].includes(previousState) && result.status.startsWith('PENDING_')))return;
      if(previous)previous.outerHTML=result.request_html;else list.insertAdjacentHTML('afterbegin',result.request_html);
      for(const shift of result.assignments||[]){
        const card=root.querySelector(`[data-shift-id="${shift.id}"]`);if(!card)continue;
        if(Number(card.dataset.assignmentVersion)>shift.period_version)continue;
        card.dataset.assignmentVersion=String(shift.period_version);
        card.querySelector('[data-shift-employee]').textContent=shift.employee_name;
        card.classList.toggle('company-schedule__shift--own',shift.own);
        card.querySelector('[data-own-label]').hidden=!shift.own;
        const lead=card.querySelector('[data-shift-lead]');if(lead)lead.hidden=!shift.is_shift_lead;
        const actions=card.querySelector('[data-own-actions]');if(actions)actions.hidden=!shift.own;
      }
      if(root.dispatchEvent)root.dispatchEvent(new CustomEvent('employee-schedule:changed',{detail:result}));
  }
  let syncing=false;
  async function syncRequests(){
    if(document.hidden||pending||syncing)return;
    syncing=true;
    try{
      const response=await fetch('/v2/scheduling/api/request-status',{credentials:'same-origin',cache:'no-store'});
      if(!response.ok)return;
      const data=await response.json();
      for(const item of data.requests){
        const shown=root.querySelector(`[data-request-id="${item.id}"]`);
        if(shown?.dataset.requestState===item.state)continue;
        const detail=await fetch(`/v2/scheduling/api/transfers/${item.id}`,{credentials:'same-origin',cache:'no-store'});
        if(detail.ok)applyResult(await detail.json());
      }
    }catch(_){/* A later visible-page check reconciles; never retry a mutation. */}
    finally{syncing=false;}
  }
  const poll=async()=>{await syncRequests();setTimeout(poll,root.querySelector('[data-request-state^="PENDING_"]')?5000:30000);};
  setTimeout(poll,5000);window.addEventListener('focus',syncRequests);
  root.addEventListener('submit',async event=>{
    const form=event.target.closest('[data-shift-request]');if(!form)return;
    event.preventDefault();if(pending)return;pending=true;error.hidden=true;
    const data=new FormData(form);if(event.submitter?.name)data.set(event.submitter.name,event.submitter.value);
    const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);status.textContent='Saving request…';
    try{
      const response=await fetch(form.action,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json'},body:data});
      const result=await response.json();if(!response.ok)throw new Error(result.message||'Request could not be saved.');
      applyResult(result);
      status.textContent=result.invalidated?result.reason:result.status==='COMPLETED'?'Completed. The schedule is updated.':result.status.replaceAll('_',' ').toLowerCase();
      if(dialog.contains(form))closeAction();else form.closest('details')?.removeAttribute('open');
      for(const details of root.querySelectorAll('[data-swap-options]'))delete details.dataset.loaded;
      const key=form.querySelector('[name=operation_key]');if(key)key.value=crypto.randomUUID();
    }catch(e){showError(`${e.message} If the connection failed, refresh before retrying; the request may have been saved.`);status.textContent='';root.dispatchEvent(new CustomEvent('employee-schedule:uncertain'));}
    finally{buttons.forEach(b=>b.disabled=false);pending=false;}
  });
  root.addEventListener('change',event=>{
    if(event.target.matches('[data-swap-choice]'))event.target.form.querySelector('[data-swap-selection]').textContent=
      event.target.value?'Exchange for: '+event.target.selectedOptions[0].textContent:'';
  });
  root.addEventListener('toggle',async event=>{
    const details=event.target.closest('details');if(!details?.open)return;
    const form=details.querySelector('[data-shift-request]');if(!form)return;
    actionDetails=details;dialog.querySelector('[data-action-own-shift]').textContent='Your shift: '+details.closest('[data-shift-label]').dataset.shiftLabel;
    dialog.querySelector('[data-action-content]').replaceChildren(form);
    dialog.querySelector('[data-action-title]').textContent=details.querySelector('summary').textContent;
    dialog.querySelector('[data-action-error]').hidden=true;dialog.showModal();
    if(!details.dataset.swapOptions||details.dataset.loaded)return;
    details.dataset.loaded='pending';const select=form.querySelector('[data-swap-choice]');
    const hint=form.querySelector('[data-swap-hint]');select.disabled=true;hint.textContent='Checking both employees’ resulting schedules…';
    try{
      const csrf=form.querySelector('[name="csrf_token"]').value;
      const response=await fetch('/v2/scheduling/api/swap-options',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({shift_id:Number(details.dataset.swapOptions)})});
      const result=await response.json();if(!response.ok)throw new Error(result.message||'Could not check eligible shifts.');
      select.replaceChildren(new Option('Choose a shift',''));
      for(const option of result.options)select.add(new Option(option.label+(option.requires_approval?' · approval required':''),String(option.shift_id)));
      const options=result.options;
      hint.textContent=options.length?'Eligibility checked. Approval-required choices stay pending until authorized.':'No eligible coworker shifts this week.';
      details.dataset.loaded='yes';
    }catch(e){delete details.dataset.loaded;hint.textContent=e.message;}
    finally{select.disabled=false;}
  },true);
})();
