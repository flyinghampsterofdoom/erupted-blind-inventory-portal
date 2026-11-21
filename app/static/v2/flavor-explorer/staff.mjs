import{api,esc,cleanName,person,when,statusLabel,classification,changedFields,notice,busy,empty,FieldEditor,profileMarkup,hasProfile}from'./shared-admin.mjs';
const $=selector=>document.querySelector(selector),message=$('#fi-staff-message');
let boot,products=[],selected=null,editor=null,requestNumber=0,detailNumber=0,timer;
const drafts=new Map();
function report(error){notice(message,error.message||String(error),true)}
function title(product){return product.brand+' · '+cleanName(product.name)}
async function search(){
 const request=++requestNumber;
 try{
  const data=await api('/products?'+new URLSearchParams({q:$('#fi-staff-search').value}));
  if(request!==requestNumber)return;
  for(const product of data.items||[]){const index=products.findIndex(row=>row.id===product.id);if(index<0)products.push(product);else products[index]=product}
  const rows=data.items||[];$('#fi-staff-results').hidden=false;$('#fi-staff-result-count').hidden=false;
  $('#fi-staff-results').innerHTML=rows.slice(0,30).map(product=>'<button type="button" class="fi-queue-row" data-staff-product="'+esc(product.id)+'"><strong>'+esc(cleanName(product.name))+'</strong><small>'+esc(product.brand)+' · '+esc(product.type)+'</small></button>').join('')||empty('No products match your search.');
  $('#fi-staff-result-count').textContent=rows.length>30?'Showing the first 30 matches. Refine your search to find a product.':rows.length+' product'+(rows.length===1?'':'s');
 }catch(error){report(error)}
}
function keepDraft(){
 if(!selected||!editor)return;
 const value=editor.read(),comment=$('#fi-staff-comment')?.value||'';
 if(Object.keys(changedFields(selected.classification,value)).length||comment.trim())drafts.set(selected.id,{product:selected,value,comment});
 else drafts.delete(selected.id);
}
function backToProducts(){
 keepDraft();detailNumber++;
 $('#fi-staff-editor').hidden=true;
 $('#fi-staff-search-form').hidden=false;
 $('#fi-staff-results').hidden=false;$('#fi-staff-result-count').hidden=false;
 const target=$('#fi-staff-results').querySelector('[data-staff-product="'+CSS.escape(selected?.id||'')+'"]')||$('#fi-staff-search');
 target.focus({preventScroll:true});target.scrollIntoView({behavior:'smooth',block:'center'});
}
async function choose(id,{keepDraft:reloadDraft=false}={}){
 clearTimeout(timer);requestNumber++;
 keepDraft();
 const request=++detailNumber,saved=drafts.get(id),draft=reloadDraft&&editor?editor.read():saved?.value||null,comment=reloadDraft?$('#fi-staff-comment')?.value||'':saved?.comment||'';
 try{
  const current=await api('/products/'+encodeURIComponent(id));
  if(request!==detailNumber)return;
  const staleDraft=Boolean(!reloadDraft&&saved&&saved.product.classificationFingerprint!==current.classificationFingerprint);
  const product=staleDraft?saved.product:current;
  $('#fi-staff-search-form').hidden=true;
  $('#fi-staff-results').hidden=true;$('#fi-staff-result-count').hidden=true;
  selected=product;const index=products.findIndex(row=>row.id===product.id);if(index<0)products.push(product);else products[index]=product;
  const area=$('#fi-staff-editor');area.hidden=false;
  area.innerHTML='<button type="button" class="v2-button v2-button--secondary fi-back-button" id="fi-staff-back"><svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M11.5 4.5 6 10l5.5 5.5M6 10h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>Back to products</button><div class="fi-editor-heading"><div><p class="v2-eyebrow">Suggest a correction</p><h2>'+esc(cleanName(product.name))+'</h2><p>'+esc(product.brand)+' · '+esc(product.type)+'</p></div><button type="button" class="fi-text-button" id="fi-staff-reload">Reload current guide</button></div><details class="fi-guide" open><summary>'+(staleDraft?'Guide used for this draft':'Current published guide')+'</summary>'+profileMarkup(product.classification,boot.taxonomy,products)+'</details>'+(hasProfile(product.inferredClassification)?'<details class="fi-guide"><summary>Suggested guide from the catalog · unverified</summary>'+profileMarkup(product.inferredClassification,boot.taxonomy,products)+'</details>':'')+'<p class="fi-hint">Change only the fields you want corrected. Your suggestion is reviewed by an administrator before any customer sees a change.</p><div id="fi-staff-fields"></div><label class="fi-field">Anything the reviewer should know? <span class="fi-hint">Optional</span><textarea id="fi-staff-comment" rows="2" maxlength="1000" placeholder="For example, the flavor is much more tart than the current guide suggests."></textarea></label><div class="fi-actions"><button type="button" class="v2-button v2-button--primary" id="fi-staff-submit">Submit for review</button></div>';
  editor=new FieldEditor($('#fi-staff-fields'),boot.taxonomy,products,draft||product.classification,{flavorId:product.flavorId});
  $('#fi-staff-comment').value=comment;
  $('#fi-staff-back').onclick=backToProducts;
  $('#fi-staff-reload').onclick=()=>choose(product.id,{keepDraft:true});
  $('#fi-staff-submit').onclick=event=>busy(event.currentTarget,submit).catch(report);
  if(staleDraft)notice(message,'The published guide changed. Your draft is preserved against its original guide. Reload the current guide and review your changes before submitting.');
  else if(reloadDraft)notice(message,'Current guide reloaded. Your draft is still in the editor; review it before submitting.');
  else area.scrollIntoView({behavior:'smooth',block:'start'});
 }catch(error){report(error)}
}
async function submit(){
 if(!selected||!editor)return;
 const product=selected,value=editor.read(),comment=$('#fi-staff-comment').value.trim();
 const changes=changedFields(product.classification,value);
 if(!Object.keys(changes).length)throw new Error('Choose at least one correction before submitting.');
 const result=await api('/products/'+encodeURIComponent(product.id)+'/suggest',{method:'POST',body:{
  baseVersionId:product.publishedVersionId??null,baseFingerprint:product.classificationFingerprint,changes,comment,
 }});
 notice(message,'Suggestion '+(result.id||result.submissionId||'')+' for '+cleanName(product.name)+' submitted for review. The published guide is unchanged.');
 const matches=(draft,note)=>!Object.keys(changedFields(value,draft)).length&&note.trim()===comment;
 const saved=drafts.get(product.id);
 if(saved&&matches(saved.value,saved.comment))drafts.delete(product.id);
 if(selected?.id===product.id&&editor&&matches(editor.read(),$('#fi-staff-comment').value)){
  editor.set(product.classification);$('#fi-staff-comment').value='';
 }
 await loadMine();
}
async function loadMine(){
 try{
  const data=await api('/my-suggestions'),items=data.items||[];
  $('#fi-my-suggestions').innerHTML=items.length?'<div class="fi-table-wrap"><table class="fi-table"><thead><tr><th>Product</th><th>Submitted</th><th>Status</th><th>Reviewer note</th></tr></thead><tbody>'+items.map(item=>'<tr><td>'+esc(person(item.product))+'</td><td>'+esc(when(item.submittedAt))+'</td><td>'+esc(statusLabel(item.status))+'</td><td>'+esc(item.reviewNote||item.reason||'Awaiting review')+'</td></tr>').join('')+'</tbody></table></div>':empty('Your submitted corrections will appear here.');
 }catch(error){report(error)}
}
async function start(){
 try{
  boot=await api('/bootstrap');
  $('#fi-staff-identity').textContent='Submitting as '+person(boot.principal)+(boot.principal.store?' · '+person(boot.principal.store):'');
  const initial=await api('/products');products=initial.items||[];
  $('#fi-staff-search-form').onsubmit=event=>{event.preventDefault();search()};
  $('#fi-staff-search').oninput=()=>{clearTimeout(timer);timer=setTimeout(search,180)};
  $('#fi-staff-results').onclick=event=>{const button=event.target.closest('[data-staff-product]');if(button)choose(button.dataset.staffProduct)};
  search();loadMine();
 }catch(error){report(error);$('#fi-staff-results').innerHTML=empty('The flavor guide is temporarily unavailable. Your existing suggestions are preserved.')}
}
start();
