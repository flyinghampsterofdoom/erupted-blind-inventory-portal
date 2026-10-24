/* Current-round quantities only. Recovery never grants access to an old store. */
(() => {
  const form = document.getElementById('blind-count-form');
  if (!form) return;
  const status = document.getElementById('count-save-status');
  const login = document.getElementById('count-login'), recover = document.getElementById('count-recover');
  const inputs = [...form.querySelectorAll('[data-field]')];
  const base = `/store/sessions/${form.dataset.sessionId}`;
  const draftBase = `/store/drafts/blind-count/${form.dataset.sessionId}`;
  const identity = {principal_id:form.dataset.principalId,store_id:form.dataset.storeId,kind:'blind-count',record_id:form.dataset.sessionId};
  const legacyKey = `blind-count:${identity.principal_id}:${identity.store_id}:${identity.record_id}`;
  let revision = Number(form.dataset.revision), generation = 0, acknowledged = 0;
  let cache = null, running = null, booting = null, timer, pending = null, pendingSubmission = null;
  let volatileDraft=null,needsReview=false;
  let locked = true, conflict = false, submitting = false, protecting = 0, storageFailed = false;
  const values = () => Object.fromEntries([...form.querySelectorAll('[data-variation-id]')].map(row => [row.dataset.variationId,
    Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input => [input.dataset.field,input.value]))]));
  const csrf = () => decodeURIComponent((document.cookie.split('; ').find(row => row.startsWith('csrf_token=')) || '').slice(11));
  const enable = () => {form.dataset.draftLocked=String(locked);inputs.forEach(input => {input.disabled = locked || conflict || submitting || pendingSubmission !== null || storageFailed;});};
  function fill(saved) {
    for (const row of form.querySelectorAll('[data-variation-id]')) for (const input of row.querySelectorAll('[data-field]')) {
      const raw = saved?.[row.dataset.variationId]?.[input.dataset.field];
      if (raw !== undefined) input.value = raw;
    }
    totals();
  }
  function totals() {
    for (const row of form.querySelectorAll('[data-variation-id]')) {
      const [front,back] = row.querySelectorAll('[data-field]');
      row.querySelector('[data-total]').textContent = front.value !== '' && back.value !== '' && front.validity.valid && back.validity.valid
        ? String(Math.round((Number(front.value)+Number(back.value))*1000)/1000) : '—';
    }
  }
  const snapshot=()=>({revision,generation,acknowledged,pending,pendingSubmission,needsReview,values:values()});
  async function protect() {
    if (!cache || locked || conflict) return;
    protecting++;
    try {await cache.save(snapshot());storageFailed=false;volatileDraft=null;}
    catch (_) {storageFailed=true;status.textContent='Could not protect this draft on your device. Keep this tab open and use Save / Retry before continuing.';throw Error('Device draft storage failed');}
    finally {protecting--;enable();}
  }
  async function pause(message) {
    // Do not snapshot blank server fields over an unrestored draft.
    inputs.forEach(input=>{input.disabled=true;});
    if (!locked && cache && !conflict) {
      volatileDraft={...snapshot(),recordName:cache.recordName,savedAt:Date.now()};
      try {await protect();} catch (_) { /* Keep the last values private in memory when the browser cannot persist. */ }
    }
    locked=true;cache=null;clearTimeout(timer);
    inputs.forEach(input=>{input.value='';});totals();enable();login.hidden=false;
    status.textContent=message+(storageFailed?' Device storage failed: keep this tab open to retain your last changes.':' Your protected draft is retained.')+' Sign in with the same account and restore store access, then use Save / Retry.';
  }
  async function finish(result) {
    pendingSubmission=null;pending=null;acknowledged=generation;conflict=false;
    status.textContent=`Submitted successfully — count #${result.receipt?.id || form.dataset.sessionId}.`;
    locked=true;enable();
    try {await cache?.clear();sessionStorage.removeItem(legacyKey);} catch (_) { /* Receipt is authoritative even if cleanup fails. */ }
    window.location.assign(base);
  }
  async function confirmReceipt(result) {
    if (pendingSubmission && pendingSubmission.revision===result.revision) {await finish(result);return;}
    // A different final revision is not an acknowledgment of this tab's quantities.
    locked=true;cache=null;await boot();
  }
  async function state() {
    const response=await fetch(`${draftBase}/state`,{credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'autosave'}});
    if (!response.ok || response.redirected) return null;
    return response.json();
  }
  async function boot() {
    if (booting) return booting;
    booting=(async()=>{
      const next=await window.eruptedDrafts.open(`${draftBase}/key`,identity);
      cache=next;form.dataset.workingContext=next.context;revision=next.state.revision;
      let records=await cache.records();
      if (volatileDraft) {
        const previous=records.find(record=>record.name===volatileDraft.recordName);
        records=records.filter(record=>record!==previous);
        records.unshift({name:previous?.name,raw:previous?.raw,value:volatileDraft});
      }
      if (next.state.submitted) {
        const covered=record=>(record.value.pendingSubmission?.revision===next.state.revision ||
          (!record.value.pending && record.value.generation<=record.value.acknowledged && record.value.revision===next.state.revision));
        for (const record of records.filter(covered)) cache.remove(record);
        const remaining=records.filter(record=>!covered(record));
        if (!remaining.length) {await finish(next.state);return false;}
        locked=true;enable();recover.hidden=false;
        status.textContent='This count was submitted, but additional protected entries were not confirmed in that submission. They are retained for authorized review.';
        recover.onclick=async()=>{
          if (!await probe()) return;
          fill(remaining[0].value.values);locked=false;conflict=true;enable();
          status.textContent='Protected entries shown for review only. This count is already submitted; these entries remain on this device and cannot overwrite it.';
        };
        return false;
      }
      // Migrate old same-tab buffers only after server authorization, without the old 24-hour cutoff.
      if (!records.length) {
        let old=null;try {old=JSON.parse(sessionStorage.getItem(legacyKey)||'null');} catch (_) {}
        if (old?.values) {await cache.save({...old,generation:1,acknowledged:0});sessionStorage.removeItem(legacyKey);records=await cache.records();}
      }
      if (Number(form.dataset.revision)!==next.state.revision && !(records.length===1 && !records[0].value.needsReview && (records[0].value.revision===next.state.revision || records[0].value.pending || records[0].value.pendingSubmission))) {
        locked=false;conflict=true;recover.hidden=false;enable();
        status.textContent='This count changed while opening. Reopen it to review current values before continuing.';
        recover.onclick=()=>window.location.assign(base);return false;
      }
      const restore=(record,rebase=false)=>{
        cache.adopt(record);fill(record.value.values);
        revision=rebase?next.state.revision:record.value.revision;
        needsReview=false;pending=rebase?null:(record.value.pending||null);
        pendingSubmission=rebase?null:(record.value.pendingSubmission||null);
        generation=record.value.generation||1;acknowledged=record.value.acknowledged||0;
        // A browser snapshot without an in-flight command must be saved at least once.
        if (generation===acknowledged && pendingSubmission===null) generation++;
        locked=false;conflict=false;recover.hidden=true;enable();
        status.textContent='Protected draft restored. Saving to server…';
      };
      if (records.length===1 && !records[0].value.needsReview && (records[0].value.revision===revision || records[0].value.pending || records[0].value.pendingSubmission)) restore(records[0]);
      else if (records.length) {
        // Server fields remain visible for review. No write/overwrite occurs before explicit reconciliation.
        locked=false;conflict=true;recover.hidden=false;enable();
        status.textContent='Protected work and the server count differ, or another tab has pending work. Review the displayed server values before restoring a draft.';
        recover.onclick=()=>{
          if (!window.confirm('Restore the most recent protected draft onto this server revision? Review all values before submitting. Other pending drafts will be retained.')) return;
          restore(records[0],true);void protect().then(()=>save()).catch(()=>{});
        };
        return false;
      } else {locked=false;conflict=false;enable();status.textContent='Saved to server';}
      login.hidden=true;
      return true;
    })().catch(error=>{status.textContent=error.message;login.hidden=false;locked=true;inputs.forEach(input=>{input.value='';});totals();enable();return false;}).finally(()=>{booting=null;});
    return booting;
  }
  async function probe() {
    const response=await (window.eruptedSessionStatus?window.eruptedSessionStatus():fetch('/session-status',{credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'autosave'}}));
    if (!response.ok) {await pause('Authentication expired.');return false;}
    const data=await response.json();
    if (String(data.principal_id)!==form.dataset.principalId || String(data.store_id)!==form.dataset.storeId) {await pause('Account or store access changed.');return false;}
    // Fresh server authorization, same principal/store: refresh presentation version in place.
    if (data.working_context) form.dataset.workingContext=data.working_context;
    return true;
  }
  async function post(path,payload) {
    const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',
      'X-CSRF-Token':csrf(),'X-Working-Context':form.dataset.workingContext||'legacy','X-Requested-With':'autosave'},body:JSON.stringify(payload)});
    if (response.status===401 || response.redirected || response.status===403) {
      await pause('Authentication or store permission needs to be revalidated.');throw Error('Authorization required');
    }
    const data=await response.json();
    if (!response.ok) {
      const message=data.error||data.detail||'Save failed';
      if (response.status===409 && !String(message).includes('Working store') && !String(message).includes('working store')) {
        // Preserve the last encrypted values/command before marking this as unresolved.
        needsReview=true;await protect();conflict=true;recover.hidden=false;
        recover.onclick=()=>window.location.assign(base);
        status.textContent='This count changed elsewhere. Protected entries are retained. Reopen this count to review and reconcile.';
      }
      const error=Error(message);error.status=response.status;throw error;
    }
    return data;
  }
  async function drain() {
    if (locked && !await boot()) return false;
    if (conflict || pendingSubmission!==null) return false;
    while (acknowledged<generation || pending) {
      if (!await probe()) return false;
      if (!pending) pending={generation,payload:{revision,operation_id:crypto.randomUUID(),changes:values()}};
      await protect();
      status.textContent='Saving to server…';
      const result=await post(`${base}/draft`,pending.payload);
      revision=result.revision;const sent=pending.generation;pending=null;
      if (Object.keys(result.errors||{}).length) {await protect();status.textContent='Valid entries saved. Correct invalid quantities before submitting.';return false;}
      acknowledged=sent;
      if (acknowledged<generation) {await protect();continue;}
      await protect();
      if (acknowledged<generation) continue;
      status.textContent='Saved to server';
    }
    return true;
  }
  async function save() {
    clearTimeout(timer);if (running) return running;
    running=drain().catch(async error=>{
      if (!locked && !conflict && !storageFailed) {
        status.textContent='Connection or save failed. Protected entries retained; retrying.';
        timer=setTimeout(()=>void save(),5000);
      }
      if (error.status===400) {
        const current=await state().catch(()=>null);if (current?.submitted) await confirmReceipt(current);
      }
      return false;
    }).finally(()=>{running=null;enable();});return running;
  }
  function dirty() {
    if (locked || conflict || pendingSubmission!==null) return;
    const editGeneration=++generation;totals();status.textContent='Protecting changes on this device…';
    void protect().then(()=>{if (editGeneration===generation && protecting===0 && !running && !locked && !conflict) status.textContent='Saved on this device; waiting for server';}).catch(()=>{});
    clearTimeout(timer);timer=setTimeout(()=>void save(),800);
  }
  inputs.forEach(input=>input.addEventListener('input',dirty));
  document.getElementById('count-save').addEventListener('click',()=>{
    if (pendingSubmission!==null) void submit();else void save();
  });
  async function submit() {
    if (submitting) return;
    submitting=true;enable();
    try {
      if (locked && !await boot()) return;
      if (conflict) return;
      if (pendingSubmission===null) {
        generation++;
        if (!await save() || !await probe()) return;
        pendingSubmission={revision};
      } else {
        if (!await probe()) return;
        const receipt=await state();if (receipt?.submitted) {await confirmReceipt(receipt);return;}
      }
      await protect();
      const result=await post(`${base}/submit`,pendingSubmission);
      if (result.submitted) await confirmReceipt(result);
    } catch (error) {
      if (error.status===400 || error.status===409) {
        const receipt=await state().catch(()=>null);if (receipt?.submitted) {await confirmReceipt(receipt);return;}
        pendingSubmission=null;
      }
      if (!locked && !conflict && !storageFailed) {
        try {await protect();status.textContent=pendingSubmission?'Submission unconfirmed. Your draft is protected. Retry Submit Count to check the receipt.':error.message;} catch (_) { /* Keep the device-storage error visible. */ }
      }
    } finally {submitting=false;enable();}
  }
  form.addEventListener('submit',event=>{event.preventDefault();void submit();});
  document.querySelector('form[action="/logout"]')?.addEventListener('submit',async event=>{
    event.preventDefault();
    if (submitting || pendingSubmission!==null || conflict) {status.textContent='Resolve the pending count or submission before logging out. Your protected draft is retained.';return;}
    if (await save() && await probe()) {await protect();await cache?.flush();inputs.forEach(input=>{input.value='';});locked=true;enable();event.target.submit();}
  });
  window.addEventListener('beforeunload',event=>{
    if (protecting || storageFailed || generation>acknowledged || pending || pendingSubmission!==null) {event.preventDefault();event.returnValue='';}
  });
  document.addEventListener('visibilitychange',()=>{if (document.hidden && !locked && !conflict) void save();else if (!document.hidden) void check();});
  async function check() {
    if (document.hidden || submitting || booting) return;
    try {if (!locked) await probe();} catch (_) { /* No storage mutation on a failed probe. */ }
  }
  window.addEventListener('online',()=>{if (!conflict) {if (pendingSubmission!==null) void submit();else void save();}});
  setInterval(()=>void check(),15000);
  form.inert=false;enable();totals();void boot().then(ok=>{if (ok && pendingSubmission!==null) void submit();else if (ok && generation>acknowledged) void save();});
})();
