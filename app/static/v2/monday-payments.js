(()=>{'use strict';
const root=document.querySelector('#monday-payments');if(!root)return;
let data=JSON.parse(document.querySelector('#monday-bootstrap').textContent),rec=data.latest?.recommendations||data.recommendation;
const roles=['big','streamline','card1','card2'],fields=Object.fromEntries(roles.map(r=>[r,root.querySelector(`[name="${r}"]`)]));
const save=root.querySelector('[data-save]'),status=root.querySelector('[data-state]'),error=root.querySelector('[data-error]'),cancel=root.querySelector('[data-cancel]');
let correcting=null,key=crypto.randomUUID(),busy=false,accountReviewRequired=false,draftAccounts=null;
const accountIdentities=()=>rec.rows.map(r=>({role:r.role,account_id:r.account_id,name:r.name}));
const accountsDiffer=(saved,current)=>!saved||saved.some(r=>current.find(c=>c.role===r.role)?.account_id!==r.account_id);
function requireAccountReview(saved){accountReviewRequired=true;draftAccounts=saved;root.querySelector('[data-account-reviewed]').checked=false;}
const usd=v=>v===null?'Unavailable':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(Number(v));
const storage=()=>`erupted-monday:${root.dataset.owner}:${rec.monday}`;
const amounts=()=>Object.fromEntries(roles.map(r=>[r,fields[r].value]));
function showError(message){error.textContent=message;error.hidden=!message;}
function total(){root.querySelector('[data-actual-total]').textContent=roles.every(r=>fields[r].value!==''&&fields[r].validity.valid)?usd(roles.reduce((s,r)=>s+Number(fields[r].value),0)):'Enter all four amounts';}
function retain(){try{localStorage.setItem(storage(),JSON.stringify({amounts:amounts(),key,correcting,note:root.querySelector('[data-note]').value,token:rec.token,accounts:accountReviewRequired?draftAccounts:accountIdentities()}));status.textContent='Draft saved on this device · not recorded';}catch{showError('This browser cannot retain the draft. Keep this page open until saved.');}}
function render(initial=false){
 if(initial){
  rec.rows.forEach(r=>{fields[r.role].value=r.recommended??'';});
  if(data.latest){data.latest.entries.forEach(e=>fields[e.role].value=e.actual);save.disabled=true;status.textContent=`Recorded batch #${data.latest.id}. Recommendations frozen; use explicit correction to change it.`;}
  try{const pending=JSON.parse(localStorage.getItem(storage())||'null');if(pending){
   const same=data.latest?.request_key===pending.key&&data.latest.entries.every(e=>Number(e.actual)===Number(pending.amounts[e.role]));
   if(same)localStorage.removeItem(storage());
   else{roles.forEach(r=>fields[r].value=pending.amounts[r]??'');key=pending.key;correcting=pending.correcting;if(correcting&&data.latest)rec=data.latest.recommendations;root.querySelector('[data-note]').value=pending.note||'';if(!correcting&&((pending.accounts&&accountsDiffer(pending.accounts,accountIdentities()))||(!pending.accounts&&pending.token!==rec.token)))requireAccountReview(pending.accounts||null);status.textContent='Recovered unsent or unacknowledged draft; review before saving.';if(data.latest&&!correcting)showError('This week has a recorded batch. Compare your draft with history before starting an explicit correction.');}
  }}catch{showError('Saved draft could not be read; review all values before recording.');}
 }
 rec.rows.forEach(r=>{const tr=root.querySelector(`[data-role="${r.role}"]`);tr.querySelector('[data-account-name]').textContent=r.name;tr.querySelector('[data-recommended]').firstChild.textContent=r.recommended===null?'Unavailable':usd(r.recommended);tr.querySelector('[data-reason]').textContent=r.unavailable||'';tr.querySelector('[data-balance]').textContent=(()=>{const valuation=data.inventory_values?.[r.role];if(valuation){tr.querySelector('[data-valuation-state]').textContent=valuation.complete?'Current Square inventory':`Incomplete · ${valuation.issues.length} issue(s) · inspect valuation below`;return valuation.complete?usd(valuation.value):'Incomplete';}const frozen=!!data.latest&&rec.token===data.latest.recommendations.token;tr.querySelector('[data-balance-label]').textContent=frozen?'Card Balance at Recommendation':'Actual Card Balance';return r.balance===null?'Unknown':usd(r.balance);})();});
 root.querySelector('[data-account-review]').hidden=!accountReviewRequired;root.querySelector('[data-account-review-message]').textContent='Payment accounts changed since this draft. Actual amounts are retained; review each before saving. Previous accounts: '+(draftAccounts?.map(r=>r.name+' (#'+r.account_id+')').join(', ')||'older draft has no account snapshot')+'.';
 root.querySelector('[data-recommended-total]').textContent=rec.rows.some(r=>r.recommended===null)?'Incomplete':usd(rec.rows.reduce((s,r)=>s+Number(r.recommended),0));
 renderValuations();
 const locked=!!data.latest&&!correcting;
 roles.forEach(r=>fields[r].disabled=locked||busy);save.disabled=locked||busy||accountReviewRequired||rec.rows.some(r=>!r.account_id);
 root.querySelector('[data-monday]').disabled=busy;root.querySelector('[data-refresh]').disabled=busy;root.querySelector('[data-note]').disabled=busy;cancel.disabled=busy;root.querySelectorAll('[data-correct]').forEach(b=>b.disabled=busy);
 root.querySelector('[data-correction]').hidden=!correcting;root.querySelector('[data-note-wrap]').hidden=!correcting;cancel.hidden=!correcting;save.textContent=correcting?'Save correction':'Save Payments';total();
}
function renderValuations(){
 if(!root.querySelector('[data-valuation-disclosure]').open)return;
 for(const role of ['big','streamline']){
  const v=data.inventory_values?.[role],detail=root.querySelector(`[data-valuation-detail="${role}"]`);if(!v||!detail)continue;
  detail.querySelector('[data-valuation-summary]').textContent=v.complete?`${usd(v.value)} · observed ${v.observed_at}`:`Incomplete valuation. Known portion ${usd(v.known_value)} is not the account total. Observed ${v.observed_at}`;
  const issues=detail.querySelector('[data-valuation-issues]');issues.replaceChildren();
  for(const problem of v.issues){const li=document.createElement('li');li.textContent=`${problem.sku}${problem.variation_id?' ['+problem.variation_id+']':''}: ${problem.message}`;issues.append(li);}
  const body=detail.querySelector('[data-valuation-items]');body.replaceChildren();
  for(const item of v.items){const tr=document.createElement('tr');
   for(const value of [`${item.product} / ${item.sku} [${item.variation_id}]`,item.on_hand??'Unknown',item.unit_cost===null?'Missing COGS':item.unit_cost, item.value===null?'Incomplete':usd(item.value),`${item.attribution_source==='SAVED_PO_FINANCING'?'Saved PO financing':'Canonical consignment vendor'}; ${item.cost_source?.replace('PurchaseOrderLine:','PO line #').replace('VendorSkuConfig:','SKU mapping #')||'cost unresolved'}; ${Object.entries(item.stores).map(([id,q])=>(v.store_names?.[id]||id)+': '+(q??'Unknown')).join(', ')}`]){const td=document.createElement('td');td.textContent=value;tr.append(td);}body.append(tr);
  }
 }
}
root.querySelector('[data-account-reviewed]').addEventListener('change',event=>{if(event.target.checked){accountReviewRequired=false;draftAccounts=accountIdentities();render();retain();}});
root.querySelector('[data-valuation-disclosure]').addEventListener('toggle',renderValuations);
Object.values(fields).forEach(f=>f.addEventListener('input',()=>{total();retain();}));root.querySelector('[data-note]').addEventListener('input',retain);
root.querySelector('[data-monday]').addEventListener('change',e=>{if(!e.target.value)return;location.href=`/management/purchasing/payments?monday=${encodeURIComponent(e.target.value)}`;});
root.querySelector('[data-refresh]').addEventListener('click',async()=>{
 try{const response=await fetch(`/management/purchasing/payments/workspace?monday=${rec.monday}`);if(!response.ok)throw Error('Could not refresh recommendations. Actual amounts are retained.');const fresh=await response.json();if(fresh.latest&&fresh.latest.id!==data.latest?.id)showError('Another save recorded this week. Your actual values are retained; reload to review history before an explicit correction.');const previousAccounts=accountIdentities();data.inventory_values=fresh.inventory_values;data.recommendation=fresh.recommendation;rec=correcting?data.latest.recommendations:(fresh.latest?.recommendations||fresh.recommendation);data.latest=fresh.latest;if(!correcting&&accountsDiffer(previousAccounts,accountIdentities()))requireAccountReview(previousAccounts);render();if(!data.latest||correcting)retain();status.textContent=data.latest?'Recorded recommendation snapshot retained · actual amounts unchanged':'Recommendations refreshed · actual amounts unchanged';}
 catch(e){showError(e.message);}
});
root.querySelectorAll('[data-correct]').forEach(button=>button.addEventListener('click',async()=>{
 if(button.dataset.week!==rec.monday){location.href=`/management/purchasing/payments?monday=${button.dataset.week}`;return;}
 if(!data.latest||Number(button.dataset.correct)!==data.latest.id){showError('Choose the most recent revision for this week. Earlier history cannot be overwritten.');return;}
 rec=data.latest.recommendations;correcting=data.latest.id;key=crypto.randomUUID();showError('');render();retain();
}));
cancel.addEventListener('click',()=>{accountReviewRequired=false;draftAccounts=null;correcting=null;rec=data.latest?.recommendations||data.recommendation;localStorage.removeItem(storage());render(true);showError('');});
save.addEventListener('click',async()=>{
 if(busy||accountReviewRequired)return;if(roles.some(r=>fields[r].value===''||!fields[r].validity.valid)){showError('Enter four nonnegative amounts with no more than two decimals. Zero is allowed.');return;}
 busy=true;render();showError('');retain();status.textContent='Saving all four accounts…';
 const body={monday:rec.monday,amounts:amounts(),request_key:key,token:rec.token,corrects:correcting,note:root.querySelector('[data-note]').value};
 try{
  const response=await fetch('/management/purchasing/payments',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},body:JSON.stringify(body)});
  let result;try{result=await response.json();}catch{throw Error('The acknowledgement was lost. Your draft and request identity are retained; retry safely.');}
  if(!response.ok)throw Error(result.detail||'No acknowledgement. Retry the retained request or check history.');
  if(result.saved!==true||result.request_key!==key||result.monday!==rec.monday||!Number.isInteger(result.batch_id))throw Error('The save acknowledgement could not be verified. Draft retained; retry safely.');
  localStorage.removeItem(storage());status.textContent=`Saved batch #${result.batch_id}`;location.href=`/management/purchasing/payments?monday=${rec.monday}`;
 }catch(e){showError(e.message);status.textContent='Not acknowledged · draft retained';}
 finally{busy=false;render();}
});
render(true);
})();
