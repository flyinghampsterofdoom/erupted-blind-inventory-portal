/* Durable recovery for stock takes, cash counts, and daily chores. */
(() => {
  for (const form of document.querySelectorAll('form[data-durable-inventory]')) {
    const controls=[...form.querySelectorAll('input,select,textarea')].filter(c=>c.name&&!c.disabled&&!['csrf_token','working_context'].includes(c.name));
    const status=document.getElementById(form.dataset.autosaveStatusId);
    const base=`/store/drafts/${form.dataset.kind}/${form.dataset.recordId}`,resume=`${base}/resume`;
    const identity={principal_id:form.dataset.principalId,store_id:form.dataset.storeId,kind:form.dataset.kind,record_id:form.dataset.recordId,record_uuid:form.dataset.recordUuid};
    const retry=document.createElement('button');retry.type='button';retry.textContent='Save / Retry protected work';form.append(retry);
    const reconcile=document.createElement('button');reconcile.type='button';reconcile.textContent='Restore protected draft after review';reconcile.hidden=true;form.append(reconcile);
    const login=document.createElement('a');login.href='/login';login.target='_blank';login.rel='noopener';login.textContent='Sign in in another tab, then retry here';login.hidden=true;form.append(login);
    let volatileDraft=null,needsReview=false;
    let cache=null,locked=true,conflict=false,busy=false,booting=null,revision=null,pending=null,generation=0,acknowledged=0,protecting=0,storageFailed=false,timer;
    const fields=()=>controls.flatMap(c=>(['checkbox','radio'].includes(c.type)&&!c.checked)?[]:[[c.name,c.value]]);
    function fill(entries) {
      const map=new Map();for(const [name,value]of entries||[]){if(!map.has(name))map.set(name,[]);map.get(name).push(value);}
      for(const c of controls){if(['checkbox','radio'].includes(c.type))c.checked=(map.get(c.name)||[]).includes(c.value);else if(map.has(c.name))c.value=map.get(c.name)[0];}
      controls.forEach(c=>c.dispatchEvent(new Event('input',{bubbles:true})));
    }
    const enable=()=>{form.dataset.draftLocked=String(locked);controls.forEach(c=>{c.disabled=locked||conflict||busy||storageFailed||!!pending?.submit;});};
    const snapshot=()=>({revision,pending,generation,acknowledged,needsReview,fields:fields()});
    async function protect(){
      if(!cache||locked||conflict)return;protecting++;
      try{await cache.save(snapshot());storageFailed=false;volatileDraft=null;}
      catch(_){storageFailed=true;status.textContent='Could not protect this draft on your device. Keep this tab open and use Save / Retry before continuing.';throw Error('Device storage failed');}
      finally{protecting--;enable();}
    }
    async function pause(){
      controls.forEach(c=>{c.disabled=true;});
      if(!locked&&!conflict){volatileDraft={...snapshot(),recordName:cache?.recordName,savedAt:Date.now()};try{await protect();}catch(_){}}locked=true;cache=null;clearTimeout(timer);
      controls.forEach(c=>{if(['checkbox','radio'].includes(c.type))c.checked=false;else c.value='';});controls.forEach(c=>c.dispatchEvent(new Event('input',{bubbles:true})));enable();login.hidden=false;
      status.textContent='Authentication or store access changed. '+(storageFailed?'Device storage failed: keep this tab open to retain your last changes. ':'Your protected draft is retained. ')+' Sign in with the same account and restore store access, then retry.';
    }
    async function finish(){
      locked=true;pending=null;acknowledged=generation;enable();status.textContent='Submitted successfully. Your work is saved.';
      try{await cache?.clear();}catch(_){}window.location.assign(resume);
    }
    async function confirmReceipt(result){
      if(pending?.submit && result.receipt?.operation_id===pending.operation_id && String(result.receipt?.principal_id)===String(identity.principal_id)){await finish();return;}
      locked=true;cache=null;await boot();
    }
    async function state(){const r=await fetch(`${base}/state`,{credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'autosave',...cache.headers()}});return r.ok&&!r.redirected?r.json():null;}
    async function boot(){
      if(booting)return booting;
      booting=(async()=>{
        const next=await window.eruptedDrafts.open(`${base}/key`,identity);cache=next;Object.assign(identity,next.scope);revision=next.state.revision;
        form.querySelector('[name="working_context"]').value=next.context;
        // Refresh must return to this exact record, including after a lost final response.
        window.history.replaceState(null,'',resume);
        let records=await cache.records();
        if(volatileDraft){const previous=records.find(record=>record.name===volatileDraft.recordName);records=records.filter(record=>record!==previous);records.unshift({name:previous?.name,raw:previous?.raw,value:volatileDraft});}
        if(next.state.write_enabled===false&&!next.state.submitted){
          locked=true;controls.forEach(c=>{if(['checkbox','radio'].includes(c.type))c.checked=false;else c.value='';});enable();
          status.textContent='Inventory counting is temporarily paused. Protected work is retained. Use the protected-work review link or retry after counting resumes.';return false;
        }
        if(next.state.submitted){
          const covered=record=>record.value.pending?.submit && record.value.pending.operation_id===next.state.receipt?.operation_id && String(next.state.receipt?.principal_id)===String(identity.principal_id);
          for(const record of records.filter(covered))cache.remove(record);
          const remaining=records.filter(record=>!covered(record));
          if(!remaining.length){await finish();return false;}
          locked=true;enable();reconcile.hidden=false;
          status.textContent='This submission is saved, but additional protected entries were not confirmed in it. They are retained for authorized review.';
          reconcile.textContent='View protected entries for review';
          reconcile.onclick=async()=>{if(!await probe())return;fill(remaining[0].value.fields);locked=false;conflict=true;enable();status.textContent='Protected entries shown for review only. This record is already submitted and cannot be overwritten.';};
          return false;
        }
        if(form.dataset.draftRevision!==String(next.state.revision)&&!(records.length===1&&!records[0].value.needsReview&&(records[0].value.revision===next.state.revision||records[0].value.pending))){
          locked=false;conflict=true;reconcile.hidden=false;enable();
          status.textContent='This draft changed while opening. Reopen it to review current values before continuing.';
          reconcile.textContent='Reopen current draft';reconcile.onclick=()=>window.location.assign(resume);return false;
        }
        const restore=(record,rebase=false)=>{
          cache.adopt(record);fill(record.value.fields);revision=rebase?next.state.revision:record.value.revision;
          needsReview=false;pending=rebase?null:(record.value.pending||null);generation=record.value.generation||1;acknowledged=record.value.acknowledged||0;
          locked=false;conflict=false;reconcile.hidden=true;login.hidden=true;enable();
          status.textContent='Protected draft restored.';
        };
        if(records.length===1&&!records[0].value.needsReview&&(records[0].value.revision===revision||records[0].value.pending))restore(records[0]);
        else if(records.length){
          locked=false;conflict=true;reconcile.hidden=false;enable();
          status.textContent='The server and protected draft differ, or another tab has pending work. Review the displayed server values before restoring.';
          reconcile.onclick=()=>{if(!window.confirm('Restore the most recent protected draft onto this server revision? Review every value before submitting. Other pending drafts are retained.'))return;restore(records[0],true);generation++;void protect().then(()=>save()).catch(()=>{});};
          return false;
        }else{locked=false;conflict=false;enable();status.textContent='Saved to server';}
        login.hidden=true;return true;
      })().catch(error=>{locked=true;controls.forEach(c=>{if(['checkbox','radio'].includes(c.type))c.checked=false;else c.value='';});enable();status.textContent=error.message;login.hidden=false;return false;}).finally(()=>{booting=null;});return booting;
    }
    async function probe(){
      const response=await window.eruptedSessionStatus();
      if(!response.ok){await pause();return false;}
      const data=await response.json();
      if(String(data.principal_id)!==String(identity.principal_id)||String(data.store_id)!==String(identity.store_id)||data.principal_uuid!==identity.principal_uuid||data.store_uuid!==identity.store_uuid){await pause();return false;}
      form.querySelector('[name="working_context"]').value=data.working_context;return true;
    }
    async function save(submit=false,action=form.action){
      if(busy)return;busy=true;enable();clearTimeout(timer);
      try{
        if(locked&&!await boot())return;
        if(conflict||!await probe())return;
        // An outstanding command is retried exactly; later edits wait for its acknowledgment.
        do{
          if(pending?.submit){const receipt=await state();if(receipt?.submitted){await confirmReceipt(receipt);return;}}
          if(!pending)pending={action,submit,revision,operation_id:crypto.randomUUID(),generation,fields:fields()};
          await protect();const data=new FormData();for(const [k,v]of pending.fields)data.append(k,v);
          const csrf=decodeURIComponent((document.cookie.split('; ').find(r=>r.startsWith('csrf_token='))||'').slice(11));data.set('csrf_token',csrf);
          status.textContent=pending.submit?'Submitting…':'Saving to server…';
          const response=await fetch(pending.action,{method:'POST',credentials:'same-origin',redirect:'error',headers:{'X-Requested-With':'autosave','X-Inventory-Draft':'true','X-Working-Context':form.querySelector('[name="working_context"]').value,'X-Draft-Version':String(pending.revision),'X-Draft-Operation':pending.operation_id,...cache.headers()},body:data});
          if([401,403].includes(response.status)){await pause();return;}
          const result=await response.json();
          if(!response.ok){
            const receipt=await state();if(receipt?.submitted){await confirmReceipt(receipt);return;}
            if(response.status===409&&!String(result.detail).includes('Working store')){
              needsReview=true;await protect();conflict=true;reconcile.hidden=false;reconcile.textContent='Reopen this draft to review server changes';reconcile.onclick=()=>window.location.assign(resume);enable();
            }
            // Invalid fields may be corrected; a final-submit intent must not lock them indefinitely.
            if(response.status===400||response.status===422){pending=null;await protect();}
            throw Error(result.detail||'Save failed. Protected values retained.');
          }
          if(form.dataset.kind==='daily-chore-sheet' && result.completed_at){
            for(const cell of form.querySelectorAll('[data-task-time-for]')){
              const timestamp=result.completed_at[cell.dataset.taskTimeFor];
              cell.textContent=timestamp?new Date(timestamp).toLocaleTimeString([], {hour:'numeric',minute:'2-digit',timeZone:'America/Los_Angeles'}):'-';
            }
          }
          revision=result.revision;acknowledged=pending.generation;pending=null;
          if(result.submitted){await confirmReceipt(result);return;}
          await protect();status.textContent=generation===acknowledged?'Saved to server':'Newer changes protected; saving to server…';
          // If this Save completed an old autosave while Submit was clicked, submit after draining it.
          if(submit&&generation===acknowledged){pending={action,submit:true,revision,operation_id:crypto.randomUUID(),generation,fields:fields()};submit=false;continue;}
        }while(pending||generation>acknowledged);
      }catch(error){if(!locked&&!storageFailed)status.textContent=conflict?'This draft changed elsewhere. Protected entries are retained. Reopen to review and reconcile.':(pending?.submit?'Submission unconfirmed. Your protected draft is retained. Retry to check the receipt.':error.message);}
      finally{busy=false;enable();if(!locked&&!conflict&&!storageFailed&&generation>acknowledged&&!pending?.submit)timer=setTimeout(()=>void save(),5000);}
    }
    function dirty(){if(locked||conflict||pending?.submit)return;const editGeneration=++generation;status.textContent='Protecting changes on this device…';void protect().then(()=>{if(editGeneration===generation&&protecting===0&&!busy&&!locked&&!conflict)status.textContent='Saved on this device; waiting for server';}).catch(()=>{});clearTimeout(timer);timer=setTimeout(()=>void save(),800);}
    form.addEventListener('input',dirty);
    form.addEventListener('submit',event=>{event.preventDefault();const action=event.submitter?.getAttribute('formaction')||form.action;void save(action.endsWith('/submit'),action);});
    retry.addEventListener('click',()=>void save());
    window.addEventListener('online',()=>{if(!conflict)void save();});
    window.addEventListener('beforeunload',event=>{if(protecting||storageFailed||generation>acknowledged||pending){event.preventDefault();event.returnValue='';}});
    document.querySelector('form[action="/logout"]')?.addEventListener('submit',async event=>{event.preventDefault();if(busy||conflict||pending?.submit){status.textContent='Resolve pending work before logging out. Your protected draft is retained.';return;}await save();if(!locked&&!storageFailed&&generation===acknowledged&&!pending){await protect();controls.forEach(c=>{if(['checkbox','radio'].includes(c.type))c.checked=false;else c.value='';});locked=true;enable();event.target.submit();}});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!locked&&!busy)void probe().catch(()=>{});});
    setInterval(()=>{if(!document.hidden&&!locked&&!busy&&!booting)void probe().catch(()=>{});},15000);
    form.inert=false;enable();void boot().then(ok=>{if(ok&&(pending||generation>acknowledged))void save();});
  }
})();
