/* The committed receipt never destroys a second tab's unconfirmed quantities. */
(() => {
  const host=document.getElementById('inventory-recovery-review');if(!host)return;
  const identity={principal_id:host.dataset.principalId,store_id:host.dataset.storeId,kind:host.dataset.kind,record_id:host.dataset.recordId};
  const prefix=`inventory-draft:v1:${identity.principal_id}:${identity.store_id}:${identity.kind}:${identity.record_id}:`;
  const status=document.createElement('p');status.setAttribute('role','status');
  try {if(!Object.keys(localStorage).some(key=>key.startsWith(prefix)))return;}
  catch(_){status.textContent='Protected device drafts could not be checked. Keep this browser data and ask for assistance.';host.append(status);return;}
  const button=document.createElement('button');button.type='button';button.textContent='Review protected entries from this device';host.append(button,status);
  let revealed=false;
  async function check(){
    if(!revealed||document.hidden)return;
    try{const response=await window.eruptedSessionStatus();const current=response.ok?await response.json():null;
      if(!current||String(current.principal_id)!==identity.principal_id||String(current.store_id)!==identity.store_id){
        for(const detail of host.querySelectorAll('details'))detail.remove();revealed=false;button.disabled=false;
        status.textContent='Sign in with the same account and restore access to this store before reviewing protected entries.';
      }
    }catch(_){} // Connectivity failure does not destroy the encrypted draft.
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)void check();});
  setInterval(()=>void check(),15000);
  button.addEventListener('click',async()=>{
    button.disabled=true;
    try{
      const cache=await window.eruptedDrafts.open(`/store/drafts/${identity.kind}/${identity.record_id}/key`,identity);
      const records=await cache.records();
      const covered=record=>identity.kind==='blind-count'
        ? (record.value.pendingSubmission?.revision===cache.state.revision || (!record.value.pending&&record.value.generation<=record.value.acknowledged&&record.value.revision===cache.state.revision))
        : (record.value.pending?.submit&&record.value.pending.operation_id===cache.state.receipt?.operation_id&&String(cache.state.receipt?.principal_id)===identity.principal_id);
      const pending=records.filter(record=>!covered(record));
      for(const record of records.filter(covered))cache.remove(record);
      if(!pending.length){status.textContent='All protected entries were confirmed in the saved submission.';return;}
      status.textContent='These entries were not confirmed in this submission. They remain protected on this device for review with a Lead. This submitted record will not be overwritten.';
      revealed=true;
      for(const record of pending){
        const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Protected draft · '+new Date(record.value.savedAt||Date.now()).toLocaleString();details.append(summary);
        const table=document.createElement('table');const add=(label,value)=>{const row=document.createElement('tr'),name=document.createElement('th'),quantity=document.createElement('td');name.textContent=label;quantity.textContent=String(value);row.append(name,quantity);table.append(row);};
        if(identity.kind==='blind-count')for(const [id,parts]of Object.entries(record.value.values||{})){
          if(!cache.labels[id])continue;add(cache.labels[id]+' · Front',parts.front_qty);add(cache.labels[id]+' · Back',parts.back_qty);
        }
        else for(const [field,value]of record.value.fields||[]){
          const label=field==='completed_task_ids'?cache.labels['task:'+value]:cache.labels[field];if(label)add(label,field==='completed_task_ids'?'Complete':value);
        }
        details.append(table);host.append(details);
      }
    }catch(error){status.textContent=error.message;button.disabled=false;}
  });
})();
