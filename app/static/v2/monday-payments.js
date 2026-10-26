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
function total(){
 root.querySelector('[data-actual-total]').textContent=roles.every(r=>fields[r].value!==''&&fields[r].validity.valid)?usd(roles.reduce((s,r)=>s+Number(fields[r].value),0)):'Enter all four amounts';
 for(const row of rec.rows){const text=root.querySelector(`[data-role="${row.role}"] [data-payment-difference]`);if(!text)continue;
  const value=fields[row.role].value;text.textContent=value===''?'':row.recommended==null?'Owner-entered actual payment; no allocation recommendation.':`Difference from recommendation: ${usd(Number(value)-Number(row.recommended))}. Your actual amount is allowed.`;
 }
}
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
 rec.rows.forEach(r=>{
  const tr=root.querySelector(`[data-role="${r.role}"]`),consignment=['big','streamline'].includes(r.role),current=data.recommendation.rows.find(c=>c.role===r.role)||r;
  tr.querySelector('[data-account-name]').textContent=r.name;
  tr.querySelector('[data-account-link]').href=r.account_id?`/v2/funding-accounts/${r.account_id}`:'/v2/payment-methods';
  tr.querySelector('[data-account-link]').textContent=r.account_id?'Account activity':'Configure account';
  tr.querySelector('[data-recommended-amount]').textContent=r.recommended===null?(r.status==='advisory_unavailable'?'Optional recommendation unavailable':r.status==='needs_information'?'Needs information':'Calculation unavailable'):usd(r.recommended);
  tr.querySelector('[data-reason]').textContent=r.unavailable||(consignment?'Verified weekly recommendation':'Optional recommendation · choose your actual payment');
  tr.querySelector('[data-verified]').textContent=r.recommended===null&&r.status!=='advisory_unavailable'?`${r.verified_amount==null?'Verified card pool '+usd(rec.gross_card_pool):'Verified portion '+usd(r.verified_amount)} · ${usd(r.unresolved_cogs||'0')} unresolved COGS${r.unpriced_items?' + '+r.unpriced_items+' unpriced sale(s)':''}. Partial, not a recommendation.`:'';
  tr.querySelector('[data-formula]').textContent=r.explanation||'Frozen historical recommendation; inspect the recorded account history.';
  tr.querySelector('[data-calculation]').textContent=consignment?`Verified gross COGS: ${usd(r.verified_amount??r.recommended)}.`:`Verified pool: ${usd(rec.gross_card_pool)}. Balances at calculation: ${usd(rec.card_balances.card1)} / ${usd(rec.card_balances.card2)}.`;
  tr.querySelector('[data-issue-link]').hidden=!r.issue_count;
  if(consignment){
   const position=data.payables?.[r.role],valuation=data.inventory_values?.[r.role];
   tr.querySelector('[data-balance]').textContent=position?.value==null?'Incomplete':usd(position.value);
   tr.querySelector('[data-exposure-note]').textContent=position?`Report credits applied: ${usd(position.credits)}.`:'Review account activity for payable obligations.';
   tr.querySelector('[data-valuation-state]').textContent=valuation?`Stock value (separate): ${valuation.complete?usd(valuation.value):'incomplete; inspect below'}`:'';
  }else{
   tr.querySelector('[data-balance-label]').textContent='Current actual issuer balance';
   const exposure=data.card_exposures?.[r.role]||current;
   tr.querySelector('[data-balance]').textContent=exposure.balance===null?'Unknown (optional)':usd(exposure.balance);
   tr.querySelector('[data-exposure-note]').textContent=!current.account_id?'Choose the owner-confirmed card in Payment Methods.':current.balance===null?'Optional: record a balance for allocation advice. Actual payments can be saved now.':'';
   const changed=current.account_id!==r.account_id;
   if(changed)tr.querySelector('[data-exposure-note]').textContent='Card role changed. This row shows the recorded account; review Payment Methods before entering a new balance.';
   tr.querySelector('[data-save-balance]').disabled=busy||!current.account_id||changed;
  }
 });
 const exposure=roles.map(role=>['big','streamline'].includes(role)?data.payables?.[role]?.value:(data.card_exposures?.[role]||data.recommendation.rows.find(r=>r.role===role))?.balance);
 root.querySelector('[data-exposure-total]').textContent=exposure.some(v=>v==null)?'Incomplete · known '+usd(exposure.reduce((sum,v)=>sum+Number(v||0),0)):usd(exposure.reduce((sum,v)=>sum+Number(v),0));
 renderEvidence();
 root.querySelector('[data-account-review]').hidden=!accountReviewRequired;root.querySelector('[data-account-review-message]').textContent='Payment accounts changed since this draft. Actual amounts are retained; review each before saving. Previous accounts: '+(draftAccounts?.map(r=>r.name+' (#'+r.account_id+')').join(', ')||'older draft has no account snapshot')+'.';
 root.querySelector('[data-recommended-total]').textContent=rec.rows.some(r=>r.recommended===null)?'Available recommendations '+usd(rec.rows.reduce((s,r)=>s+Number(r.recommended||0),0)):usd(rec.rows.reduce((s,r)=>s+Number(r.recommended),0));
 renderValuations();
 const locked=!!data.latest&&!correcting;
 roles.forEach(r=>fields[r].disabled=locked||busy);save.disabled=locked||busy||accountReviewRequired||rec.rows.some(r=>!r.account_id);
 root.querySelector('[data-monday]').disabled=busy;root.querySelector('[data-refresh]').disabled=busy;root.querySelector('[data-note]').disabled=busy;cancel.disabled=busy;root.querySelectorAll('[data-correct]').forEach(b=>b.disabled=busy);
 root.querySelector('[data-correction]').hidden=!correcting;root.querySelector('[data-note-wrap]').hidden=!correcting;cancel.hidden=!correcting;save.textContent=correcting?'Save correction':'Save Payments';total();
}
let exceptionLimit=20;
function renderEvidence(){
 const historical=root.querySelector('[data-historical-disclosures]'),historyItems=root.querySelector('[data-historical-items]');
 if(historical&&historyItems){
  historical.hidden=!(rec.historical_limitations||[]).length;historyItems.replaceChildren();
  for(const item of rec.historical_limitations||[]){const p=document.createElement('p');p.textContent=`${item.product} / ${item.sku||item.variation_id} · sale #${item.fact_id} · ${item.quantity} units. ${item.message}`;historyItems.append(p);}
 }
 const pool=root.querySelector('[data-card-pool]');if(pool)pool.textContent=`Combined credit card COGS: ${usd(rec.gross_card_pool)}${rec.issues.some(i=>i.affected_roles.includes('card1'))?' known portion; review cost exceptions.':'.'} Individual payments are owner decisions; issuer balances are optional.`;
 const source=root.querySelector('[data-source-disclosures]');
 if(source){const rewards=(rec.excluded||[]).filter(x=>x.reason==='LOYALTY_DISCOUNT_MARKER');source.textContent=`Square source units: ${rec.detected_sale_units}. Merchandise units: ${rec.merchandise_sale_units||rec.detected_sale_units}. ${rewards.length} loyalty reward marker(s) treated as discounts, excluded from inventory COGS; underlying merchandise retains its COGS.`;}
 const problems=rec.issues||[],section=root.querySelector('[data-exceptions]');
 root.querySelector('[data-exceptions-summary]').textContent=problems.length?`Financial exceptions · ${problems.length} affected sale(s)`:'Financial exceptions · no unresolved sale items';
 if(section.open){
  const items=root.querySelector('[data-exception-items]');items.replaceChildren();
  const grouped=new Map();
  for(const problem of problems){
   const key=JSON.stringify([problem.variation_id,problem.message,problem.affected_roles]);
   if(!grouped.has(key))grouped.set(key,{...problem,fact_ids:[],known:0,unpriced:0,lots:[]});
   const item=grouped.get(key);item.fact_ids.push(problem.fact_id);item.known+=Number(problem.gross_cogs||0);if(problem.gross_cogs===null)item.unpriced++;
   for(const lot of problem.lots)if(!item.lots.some(l=>l.line_id===lot.line_id))item.lots.push(lot);
  }
  const query=root.querySelector('[data-exception-search]').value.toLowerCase();
  const matches=[...grouped.values()].filter(i=>`${i.product} ${i.sku||''} ${i.variation_id}`.toLowerCase().includes(query));
  root.querySelector('[data-exception-count]').textContent=`Showing ${Math.min(exceptionLimit,matches.length)} of ${matches.length} product exceptions. Amounts are counted once per affected sale.`;
  root.querySelector('[data-more-exceptions]').hidden=matches.length<=exceptionLimit;
  for(const issue of matches.slice(0,exceptionLimit)){
   const article=document.createElement('article');article.className='monday-exception';
   const heading=document.createElement('strong');heading.textContent=`${issue.product} / ${issue.sku||issue.variation_id}`;article.append(heading);
   const info=document.createElement('p');info.textContent=`Sale facts ${issue.fact_ids.map(id=>'#'+id).join(', ')}: ${issue.message}. Gross COGS impact: ${usd(issue.known)}${issue.unpriced?' + '+issue.unpriced+' unpriced sale(s)':''}. Affects ${issue.affected_roles.map(role=>rec.rows.find(r=>r.role===role)?.label||role).join(', ')}.`;article.append(info);
   const action=document.createElement('a');action.href=issue.action_url;action.textContent=issue.action;article.append(action);
   for(const lot of issue.lots){const line=document.createElement('p'),link=document.createElement('a');link.href=`/management/ordering-tool/orders/${lot.po_id}`;link.textContent=`PO #${lot.po_id}, line #${lot.line_id}`;line.append(link,document.createTextNode(` · ${lot.category} · ${lot.reason||'dated receipt verified'}`));article.append(line);}
   items.append(article);
  }
 }
 if(root.querySelector('[data-calculation-disclosure]').open){
  const body=root.querySelector('[data-calculation-items]');body.replaceChildren();
  for(const fact of rec.facts){const tr=document.createElement('tr');for(const value of [`${fact.product||fact.variation_id} / ${fact.sku||''}`,fact.fact_id,fact.category,`${fact.quantity||'?'} × ${fact.unit_cost}`,usd(fact.gross_cogs),`${fact.cost_source}; ${(fact.lots||[]).map(l=>`PO #${l.po_id} / line #${l.line_id}: `+l.receipts.map(r=>r.legacy_event_id?'legacy event #'+r.legacy_event_id:'receipt #'+r.receipt_id).join(', ')).join('; ')}`]){const td=document.createElement('td');td.textContent=value;tr.append(td);}body.append(tr);}
 }
}
root.querySelector('[data-exception-search]').addEventListener('input',()=>{exceptionLimit=20;renderEvidence();});
root.querySelector('[data-more-exceptions]').addEventListener('click',()=>{exceptionLimit+=20;renderEvidence();});
root.querySelector('[data-exceptions]').addEventListener('toggle',renderEvidence);
root.querySelector('[data-calculation-disclosure]').addEventListener('toggle',renderEvidence);
root.querySelectorAll('[data-save-balance]').forEach(button=>{
 let balanceKey=null,pendingValue=null;
 button.addEventListener('click',async()=>{
  const tr=button.closest('[data-role]'),role=tr.dataset.role,input=tr.querySelector('[data-issuer-balance]'),state=tr.querySelector('[data-balance-state]'),account=data.recommendation.rows.find(r=>r.role===role);
  if(input.value===''||!input.validity.valid){state.textContent='Enter an actual issuer balance with at most two decimals.';return;}
  if(pendingValue!==input.value){balanceKey=crypto.randomUUID();pendingValue=input.value;}
  button.disabled=true;
  try{
   const response=await fetch('/management/purchasing/payments/balance',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},body:JSON.stringify({role,account_id:account.account_id,balance:input.value,request_key:balanceKey})});
   const result=await response.json();if(!response.ok||result.saved!==true)throw Error(result.detail||'Balance acknowledgement was not verified; retry safely.');
   state.textContent=`Issuer balance recorded for ${account.name}. Refresh recommendations; actual payment amounts are retained.`;
  }catch(e){state.textContent=e.message;}finally{button.disabled=false;}
 });
});
function renderValuations(){
 if(!root.querySelector('[data-valuation-disclosure]').open)return;
 for(const role of ['big','streamline']){
  const v=data.inventory_values?.[role],detail=root.querySelector(`[data-valuation-detail="${role}"]`);if(!v||!detail)continue;
  detail.querySelector('[data-valuation-summary]').textContent=v.complete?`${usd(v.value)} · observed ${v.observed_at}`:`Incomplete valuation. Known portion ${usd(v.known_value)} is not the account total. Observed ${v.observed_at}`;
  const issues=detail.querySelector('[data-valuation-issues]');issues.replaceChildren();
  for(const problem of v.issues){const li=document.createElement('li');li.textContent=`${problem.sku}${problem.variation_id?' ['+problem.variation_id+']':''}: ${problem.message}`;issues.append(li);}
  for(const observation of v.negative_observations||[]){const li=document.createElement('li');li.textContent=`${observation.sku} · ${observation.store}: ${observation.quantity}; negative inventory contributes zero value.`;issues.append(li);}
  const body=detail.querySelector('[data-valuation-items]');body.replaceChildren();
  for(const item of v.items){const tr=document.createElement('tr');
   for(const value of [`${item.product} / ${item.sku} [${item.variation_id}]`,item.on_hand??'Unknown',item.unit_cost===null?'Missing COGS':item.unit_cost, item.value===null?'Incomplete':usd(item.value),`Current configured vendor assignment; ${item.cost_source?.replace('PurchaseOrderLine:','PO line #').replace('VendorSkuConfig:','SKU mapping #')||'cost unresolved'}; ${Object.entries(item.stores).map(([id,q])=>(v.store_names?.[id]||id)+': '+(q??'Unknown')).join(', ')}`]){const td=document.createElement('td');td.textContent=value;tr.append(td);}body.append(tr);
  }
 }
}
root.querySelector('[data-account-reviewed]').addEventListener('change',event=>{if(event.target.checked){accountReviewRequired=false;draftAccounts=accountIdentities();render();retain();}});
root.querySelector('[data-valuation-disclosure]').addEventListener('toggle',renderValuations);
Object.values(fields).forEach(f=>f.addEventListener('input',()=>{total();retain();}));root.querySelector('[data-note]').addEventListener('input',retain);
root.querySelector('[data-monday]').addEventListener('change',e=>{if(!e.target.value)return;location.href=`/management/purchasing/payments?monday=${encodeURIComponent(e.target.value)}`;});
root.querySelector('[data-refresh]').addEventListener('click',async()=>{
 try{const response=await fetch(`/management/purchasing/payments/workspace?monday=${rec.monday}`);if(!response.ok)throw Error('Could not refresh recommendations. Actual amounts are retained.');const fresh=await response.json();if(fresh.latest&&fresh.latest.id!==data.latest?.id)showError('Another save recorded this week. Your actual values are retained; reload to review history before an explicit correction.');const previousAccounts=accountIdentities();data.inventory_values=fresh.inventory_values;data.payables=fresh.payables;data.card_exposures=fresh.card_exposures;data.recommendation=fresh.recommendation;rec=correcting?data.latest.recommendations:(fresh.latest?.recommendations||fresh.recommendation);data.latest=fresh.latest;if(!correcting&&accountsDiffer(previousAccounts,accountIdentities()))requireAccountReview(previousAccounts);render();if(!data.latest||correcting)retain();status.textContent=data.latest?'Recorded recommendation snapshot retained · actual amounts unchanged':'Recommendations refreshed · actual amounts unchanged';}
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
