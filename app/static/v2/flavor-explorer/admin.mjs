import{api,esc,cleanName,person,when,confidenceLabel,statusLabel,classification,changedFields,notice,busy,empty,FieldEditor,profileMarkup,hasProfile,diffMarkup,previewMarkup,normalizedNote}from'./shared-admin.mjs';
const $=selector=>document.querySelector(selector),panel=$('#fi-panel'),message=$('#fi-message'),previewPanel=$('#fi-preview');
const state={tab:'catalog',items:[],all:[],selected:new Set(),product:null,group:null,history:null,groups:[],queueStatus:'PENDING',queueSearch:'',historyId:null,reviewSelected:new Set()};
let boot,editor=null,bulkRelations=null,editorMode='',preview=null,previewRequest=null,requestSequence=0,editorSequence=0,queueRequestSequence=0,searchTimer;
const badge=value=>'<span class="v2-badge '+(value==='verified'?'v2-badge--success':value==='unclassified'?'':'v2-badge--warning')+'">'+esc(confidenceLabel(value))+'</span>';
const title=product=>product.brand+' · '+cleanName(product.name);
const unavailableGroup=group=>Boolean(group.catalogUnavailable||group.product?.catalogUnavailable);
function retainedIdentityMarkup(product={}){
 const identity=(label,values)=>values?.length?'<p><strong>'+label+':</strong> '+values.map(esc).join(' · ')+'</p>':'';
 return '<div class="fi-guide"><strong>Catalog unavailable</strong><p>This retained classification record is not in the current catalog. Publication is unavailable; its history remains accessible.</p>'+identity('Square item IDs',product.squareItemIds)+identity('Square variation IDs',product.squareVariationIds)+'</div>';
}
const productById=id=>state.all.find(product=>String(product.id)===String(id));
function mergeRows(rows){for(const row of rows){const index=state.all.findIndex(product=>product.id===row.id);if(index<0)state.all.push(row);else state.all[index]=row}}
function report(error){notice(message,error.message||String(error),true)}
let discoveryView=null,discoveryOpen=false,discoveryGeneration=0;
function selectAdminTab(name){document.querySelectorAll('[data-fi-tab]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.fiTab===name)))}
async function openDiscoverySetup(){
 if(!boot?.capabilities?.manageExplorerVisibility)return;
 const generation=++discoveryGeneration;discoveryOpen=true;
 $('#fi-classification-workspace').hidden=true;$('#fi-discovery-panel').hidden=false;selectAdminTab('discovery');
 if(!discoveryView){
  const module=await import('./discovery-admin.mjs');
  if(!discoveryOpen||generation!==discoveryGeneration)return;
  discoveryView=module.createDiscoveryAdmin({manageExplorerPhotos:Boolean(boot.capabilities?.manageExplorerPhotos)});
 }
 await discoveryView.mount($('#fi-discovery-panel'));
}
function activateTab(name){
 if(name==='discovery'){openDiscoverySetup().catch(report);return}
 if(discoveryOpen){
  discoveryOpen=false;discoveryGeneration++;discoveryView?.unmount();
  $('#fi-discovery-panel').hidden=true;$('#fi-classification-workspace').hidden=false;selectAdminTab(name);
  if(name===state.tab)return;
 }
 state.tab=name;previewPanel.hidden=true;preview=null;editor=null;editorMode='';
 document.querySelectorAll('[data-fi-tab]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.fiTab===name)));
 if(name==='catalog')catalogView();else if(name==='suggestions')suggestionsView();else if(name==='history')historyView();else activityView();
}
function catalogView(){
 panel.innerHTML='<form class="fi-toolbar" id="fi-catalog-filter"><label class="fi-field fi-search">Search products<input type="search" id="fi-search" placeholder="Product, brand or flavor"></label><label class="fi-field">Classification<select id="fi-confidence"><option value="">All classifications</option><option value="verified">Verified</option><option value="provisional">Pending proposals</option><option value="inferred">Automatically inferred</option><option value="unclassified">Unclassified</option></select></label><label class="fi-field">Product type<select id="fi-type"><option value="">All types</option><option value="freebase">Freebase</option><option value="salt">Nicotine salts</option><option value="disposable">Disposables</option></select></label><button class="v2-button v2-button--secondary">Search</button></form><div class="fi-selection" id="fi-selection" hidden></div><div class="fi-split"><div class="fi-card fi-list"><div class="fi-list-header"><label><input type="checkbox" id="fi-select-visible"> Select matching products</label><span id="fi-product-count"></span></div><div id="fi-products">'+empty('Loading catalog…')+'</div></div><aside class="fi-card fi-editor" id="fi-editor">'+empty('Choose a product to review its classification, or select several for a bulk edit.')+'</aside></div>';
 $('#fi-catalog-filter').addEventListener('submit',event=>{event.preventDefault();loadProducts()});
 $('#fi-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadProducts,180)});
 $('#fi-confidence').addEventListener('change',loadProducts);$('#fi-type').addEventListener('change',loadProducts);
 $('#fi-select-visible').addEventListener('change',event=>{for(const row of state.items)event.target.checked?state.selected.add(row.id):state.selected.delete(row.id);renderCatalogRows();selectionChanged()});
 $('#fi-products').addEventListener('change',event=>{const input=event.target.closest('[data-select-product]');if(input){input.checked?state.selected.add(input.dataset.selectProduct):state.selected.delete(input.dataset.selectProduct);renderCatalogRows();selectionChanged()}});
 $('#fi-products').addEventListener('click',event=>{const button=event.target.closest('[data-edit-product]');if(button){state.selected=new Set([button.dataset.editProduct]);renderCatalogRows();renderSelection();openEditor(button.dataset.editProduct)}});
 loadProducts();
}
async function loadProducts(){
 if(state.tab!=='catalog')return;
 const sequence=++requestSequence;
 const query=new URLSearchParams({q:$('#fi-search').value,confidence:$('#fi-confidence').value,type:$('#fi-type').value});
 try{const data=await api('/products?'+query);if(sequence!==requestSequence||state.tab!=='catalog')return;state.items=data.items||[];mergeRows(state.items);renderCatalogRows();renderSelection()}catch(error){report(error)}
}
function renderCatalogRows(){
 const rows=$('#fi-products');if(!rows)return;
 rows.innerHTML=state.items.map(product=>'<div class="fi-row '+(state.selected.has(product.id)?'is-selected':'')+'"><input type="checkbox" data-select-product="'+esc(product.id)+'" aria-label="Select '+esc(title(product))+'" '+(state.selected.has(product.id)?'checked':'')+'><div><div class="fi-row-title">'+esc(cleanName(product.name))+'</div><div class="fi-row-meta"><span>'+esc(product.brand)+' · '+esc(product.type)+'</span>'+badge(product.confidence)+(product.pendingCount?'<span>'+Number(product.pendingCount)+' pending</span>':'')+'</div></div><button type="button" class="v2-button v2-button--secondary" data-edit-product="'+esc(product.id)+'">Review</button></div>').join('')||empty('No products match these filters.');
 $('#fi-product-count').textContent=state.items.length+' products';
 const chosen=state.items.filter(row=>state.selected.has(row.id)).length;
 $('#fi-select-visible').checked=Boolean(state.items.length)&&chosen===state.items.length;$('#fi-select-visible').indeterminate=chosen>0&&chosen<state.items.length;
}
function renderSelection(){
 const area=$('#fi-selection');if(!area)return;area.hidden=!state.selected.size;
 area.innerHTML='<strong>'+state.selected.size+' selected</strong><div><button type="button" class="v2-button v2-button--secondary" data-edit-selected>Bulk edit</button> <button type="button" class="fi-text-button" data-clear-selection>Clear selection</button></div>';
 area.querySelector('[data-clear-selection]').onclick=()=>{state.selected.clear();renderCatalogRows();renderSelection();$('#fi-editor').innerHTML=empty('Choose a product or select several to classify together.');editor=null};
 area.querySelector('[data-edit-selected]').onclick=()=>state.selected.size===1?openEditor([...state.selected][0]):openBulkEditor();
}
function selectionChanged(){renderSelection();if(state.selected.size===1)openEditor([...state.selected][0]);else if(state.selected.size>1){if(editorMode==='bulk')$('#fi-editor h2').textContent='Classify '+state.selected.size+' products';else openBulkEditor();}else{$('#fi-editor').innerHTML=empty('Choose a product or select several to classify together.');editor=null}}
async function openEditor(id){
 const sequence=++editorSequence;
 try{
  const product=await api('/products/'+encodeURIComponent(id));
  if(state.tab!=='catalog'||sequence!==editorSequence)return;state.product=product;mergeRows([product]);editorMode='single';
  const suggested=hasProfile(product.inferredClassification);
  $('#fi-editor').innerHTML='<div class="fi-editor-heading"><div><h2>'+esc(cleanName(product.name))+'</h2><p>'+esc(product.brand)+' · '+esc(product.type)+' · '+esc((product.strengths||[]).join(' / '))+' mg</p></div>'+badge(product.confidence)+'</div>'+(suggested?'<div class="fi-guide"><strong>Suggested guide from the catalog</strong><p>These suggestions have not been verified. Copy them only when you want to review and publish them.</p><button type="button" class="v2-button v2-button--secondary" id="fi-use-guide">Use suggested guide</button></div>':'')+'<div id="fi-field-editor"></div><label class="fi-field">Change note<input id="fi-publish-reason" maxlength="500" value="Review flavor classification"></label><div class="fi-actions"><button type="button" class="fi-text-button" id="fi-view-history">View history</button><button type="button" class="v2-button v2-button--primary" id="fi-publish">Review & publish</button></div>';
  editor=new FieldEditor($('#fi-field-editor'),boot.taxonomy,state.all,product.classification,{flavorId:product.flavorId});
  if(suggested)$('#fi-use-guide').onclick=()=>{editor.set(product.inferredClassification);notice(message,'Suggested guide copied into your draft. Review each field before publishing.')};
  $('#fi-view-history').onclick=()=>{state.historyId=product.id;activateTab('history')};
  $('#fi-publish').onclick=event=>busy(event.currentTarget,()=>prepare({kind:'publish',productIds:[product.id],classification:editor.read(),reason:$('#fi-publish-reason').value.trim()})).catch(report);
 }catch(error){report(error)}
}
function tokenOptions(items,key){return '<div class="fi-tokens" data-bulk-token-set="'+key+'">'+items.map(row=>'<label class="fi-token"><input type="checkbox" data-bulk="'+key+'" value="'+esc(key==='notes'?row.slug:row.id)+'"><span>'+esc(row.label||row.name||row.slug)+'</span></label>').join('')+'</div>'}
function openBulkEditor(){
 editorSequence++;
 editorMode='bulk';editor=null;
 $('#fi-editor').innerHTML='<h2>Classify '+state.selected.size+' products</h2><p class="fi-hint">Only the changes you choose will be applied. Existing unrelated classifications stay in place.</p><fieldset><legend>Flavor notes</legend><div class="fi-bulk-row"><select id="fi-bulk-notes" aria-label="Bulk flavor note action"><option value="">Leave unchanged</option><option value="add">Add selected notes</option><option value="remove">Remove selected notes</option></select>'+tokenOptions(boot.taxonomy.notes||[],'notes')+'<div class="fi-add-related fi-add-note"><input id="fi-bulk-new-note" maxlength="80" placeholder="Add another flavor note" aria-label="New bulk flavor note"><button type="button" class="v2-button v2-button--secondary" id="fi-bulk-add-note">Add note</button></div></div></fieldset><fieldset><legend>Categories</legend><div class="fi-bulk-row"><select id="fi-bulk-categories" aria-label="Bulk category action"><option value="">Leave unchanged</option><option value="add">Add selected categories</option><option value="remove">Remove selected categories</option></select>'+tokenOptions(boot.taxonomy.categories||[],'categories')+'</div></fieldset><fieldset><legend>Sensory attributes</legend>'+(boot.taxonomy.sensoryAxes||[]).map(axis=>'<div class="fi-bulk-axis"><span>'+esc(axis.label||axis.slug)+'</span><select data-bulk-axis="'+esc(axis.slug)+'" aria-label="'+esc(axis.label||axis.slug)+' bulk action"><option value="">Leave unchanged</option><option value="set">Set intensity</option><option value="clear">Clear classification</option></select><input data-bulk-value="'+esc(axis.slug)+'" type="number" min="0" max="5" step="1" value="3" aria-label="'+esc(axis.label||axis.slug)+' intensity out of five" disabled></div>').join('')+'</fieldset><label class="fi-field"><span><input type="checkbox" id="fi-bulk-description-enabled"> Replace descriptions</span><textarea id="fi-bulk-description" rows="2" maxlength="4000" disabled></textarea></label><fieldset><legend>Similar flavors</legend><select id="fi-bulk-similarity" aria-label="Bulk similarity action"><option value="">Leave unchanged</option><option value="add">Add relationships below</option><option value="remove">Remove relationships below</option></select><div id="fi-bulk-relations"></div></fieldset><label class="fi-field">Change note<input id="fi-bulk-reason" maxlength="500" value="Bulk flavor classification"></label><div class="fi-actions"><button type="button" class="v2-button v2-button--primary" id="fi-preview-bulk">Preview all changes</button></div>';
 const selectedFlavors=new Set([...state.selected].map(id=>Number(productById(id)?.flavorId)));
 bulkRelations=new FieldEditor($('#fi-bulk-relations'),boot.taxonomy,state.all.filter(product=>!selectedFlavors.has(Number(product.flavorId))),{}, {onlySimilarities:true});
 $('#fi-editor').querySelectorAll('[data-bulk-axis]').forEach(select=>select.onchange=()=>{$('#fi-editor').querySelector('[data-bulk-value="'+CSS.escape(select.dataset.bulkAxis)+'"]').disabled=select.value!=='set'});
 $('#fi-bulk-add-note').onclick=()=>{
  const input=$('#fi-bulk-new-note');
  try{const note=normalizedNote(input.value,boot.taxonomy),set=$('#fi-editor [data-bulk-token-set="notes"]');let checkbox=set.querySelector('input[value="'+CSS.escape(note.slug)+'"]');if(!checkbox){const label=document.createElement('label');label.className='fi-token';label.innerHTML='<input type="checkbox" data-bulk="notes" value="'+esc(note.slug)+'"><span>'+esc(note.label)+'</span>';set.append(label);checkbox=label.querySelector('input')}checkbox.checked=true;if(!$('#fi-bulk-notes').value)$('#fi-bulk-notes').value='add';input.value='';input.setCustomValidity('');checkbox.focus()}catch(error){input.setCustomValidity(error.message);input.reportValidity()}
 };
 $('#fi-bulk-description-enabled').onchange=event=>$('#fi-bulk-description').disabled=!event.target.checked;
 $('#fi-preview-bulk').onclick=event=>busy(event.currentTarget,()=>prepare({kind:'bulk',productIds:[...state.selected],operation:bulkOperation(),reason:$('#fi-bulk-reason').value.trim()})).catch(report);
}
function bulkOperation(){
 const operation={};
 for(const key of ['notes','categories']){
  const action=$('#fi-bulk-'+key).value;
  if(action){const values=[...$('#fi-editor').querySelectorAll('[data-bulk="'+key+'"]:checked')].map(input=>key==='notes'?input.value:Number(input.value));if(values.length)operation[key]={[action]:values}}
 }
 const set={},clear=[];
 for(const select of $('#fi-editor').querySelectorAll('[data-bulk-axis]')){
  const axis=select.dataset.bulkAxis;
  if(select.value==='clear')clear.push(axis);
  if(select.value==='set'){const value=Number($('#fi-editor').querySelector('[data-bulk-value="'+CSS.escape(axis)+'"]').value);if(!Number.isFinite(value)||value<0||value>5)throw new Error('Choose sensory intensities from 0 to 5.');set[axis]=value/5}
 }
 if(Object.keys(set).length||clear.length)operation.sensory={set,clear};
 if($('#fi-bulk-description-enabled').checked)operation.description=$('#fi-bulk-description').value;
 const similarityAction=$('#fi-bulk-similarity').value,relations=bulkRelations.read().similarities;
 if(similarityAction&&relations.length)operation.similarities={[similarityAction]:similarityAction==='remove'?relations.map(row=>row.flavor_id):relations};
 if(!Object.keys(operation).length)throw new Error('Choose at least one classification change to preview.');
 return operation;
}
async function prepare(payload){
 const previousFocus=document.activeElement;
 notice(message,'');const result=await api('/preview',{method:'POST',body:payload});
 preview=result;previewRequest=payload;previewPanel.hidden=false;
 previewPanel.innerHTML=previewMarkup(result,boot.taxonomy,state.all);
 const confirm=previewPanel.querySelector('[data-confirm-preview]');
 if(payload.kind==='review'&&payload.decision==='REJECTED'){previewPanel.querySelector('h2').textContent='Reject '+payload.groupIds.length+' suggestion group'+(payload.groupIds.length===1?'':'s');confirm.textContent='Confirm rejection'}
 else confirm.textContent=payload.kind==='restore'||payload.kind==='reverse'?'Confirm restoration':'Publish '+Number(result.changedCount||0)+' changes';
 if(!result.changedCount&&!(payload.kind==='review'&&payload.decision==='REJECTED'))confirm.disabled=true;
 previewPanel.querySelector('[data-cancel-preview]').onclick=()=>{previewPanel.hidden=true;preview=null;if(previousFocus?.isConnected)previousFocus.focus()};
 confirm.onclick=()=>commitPreview(confirm);
 previewPanel.scrollIntoView({behavior:'smooth',block:'start'});
}
async function commitPreview(button){
 if(!preview)return;
 const token=preview.previewId;
 try{
  await busy(button,async()=>{
   const result=await api('/commit',{method:'POST',body:{previewId:token}});
   previewPanel.hidden=true;preview=null;
   notice(message,'Operation '+(result.operationId||'completed')+' saved. Published classifications are available to the Explorer.');
   state.selected.clear();
   const rows=await api('/products');state.all=rows.items||[];
   if(state.tab==='catalog'){await loadProducts();if(state.product)await openEditor(state.product.id)}
   else if(state.tab==='suggestions')await loadGroups();
   else if(state.tab==='history')await loadHistory(state.historyId);
   else await loadActivity();
  });
 }catch(error){report(error)}
}
function suggestionsView(){
 panel.innerHTML='<form class="fi-toolbar" id="fi-queue-filter"><label class="fi-field fi-search">Search suggestions<input type="search" id="fi-queue-search" placeholder="Product, employee or store" value="'+esc(state.queueSearch)+'"></label><label class="fi-field">Review status<select id="fi-queue-status"><option value="PENDING">Pending</option><option value="approved">Approved (including modified)</option><option value="APPROVED">Approved without modification</option><option value="MODIFIED_APPROVED">Modified & approved</option><option value="REJECTED">Rejected</option><option value="">All statuses</option></select></label><button class="v2-button v2-button--secondary">Refresh queue</button></form><div class="fi-selection" id="fi-queue-selection" hidden></div><div class="fi-split"><div class="fi-card fi-list" id="fi-groups">'+empty('Loading suggestions…')+'</div><aside class="fi-card fi-editor" id="fi-group-detail">'+empty('Choose a suggestion group to compare its changes and review every supporting submission.')+'</aside></div>';
 $('#fi-queue-status').value=state.queueStatus;
 $('#fi-queue-filter').onsubmit=event=>{event.preventDefault();loadGroups()};
 $('#fi-queue-status').onchange=()=>{state.queueStatus=$('#fi-queue-status').value;loadGroups()};
 $('#fi-queue-search').oninput=()=>{state.queueSearch=$('#fi-queue-search').value;renderGroups()};
 $('#fi-groups').onchange=event=>{const input=event.target.closest('[data-select-group]');if(input){input.checked?state.reviewSelected.add(input.dataset.selectGroup):state.reviewSelected.delete(input.dataset.selectGroup);renderReviewSelection()}};
 $('#fi-groups').onclick=event=>{const button=event.target.closest('[data-review-group]');if(button)openGroup(button.dataset.reviewGroup)};
 loadGroups();
}
async function loadGroups(){
 const sequence=++queueRequestSequence,status=state.queueStatus;
 try{const data=await api('/suggestions?'+new URLSearchParams({status}));if(sequence!==queueRequestSequence||state.tab!=='suggestions'||state.queueStatus!==status)return;state.groups=data.groups||[];const pendingIds=new Set(state.groups.filter(group=>group.status==='PENDING').map(group=>String(group.id)));state.reviewSelected=new Set([...state.reviewSelected].filter(id=>pendingIds.has(id)));renderGroups();renderReviewSelection();if(state.group){const current=state.groups.find(group=>String(group.id)===String(state.group.id));if(current)openGroup(current.id);else $('#fi-group-detail').innerHTML=empty('This review is complete. Choose another suggestion.')}}catch(error){report(error)}
}
function renderGroups(){
 const query=state.queueSearch.trim().toLowerCase(),groups=state.groups.filter(group=>[person(group.product),group.product?.name,group.product?.brand,group.productId,...(group.product?.squareItemIds||[]),...(group.product?.squareVariationIds||[]),...(group.submissions||[]).flatMap(sub=>[person(sub.employee),person(sub.store),sub.comment])].join(' ').toLowerCase().includes(query));
 $('#fi-groups').innerHTML=groups.map(group=>'<div class="fi-group-select-row">'+(group.status==='PENDING'?'<input type="checkbox" data-select-group="'+esc(group.id)+'" aria-label="Select suggestion group '+esc(group.id)+'" '+(state.reviewSelected.has(String(group.id))?'checked':'')+'>':'<span></span>')+'<button type="button" class="fi-queue-row '+(state.group?.id===group.id?'is-active':'')+'" data-review-group="'+esc(group.id)+'"><strong>'+esc(person(group.product))+'</strong>'+(unavailableGroup(group)?'<small>Catalog unavailable'+(group.product?.squareItemIds?.length?' · Square item '+esc(group.product.squareItemIds[0]):'')+'</small>':'')+'<small>'+statusLabel(group.status)+' · '+(group.submissions||[]).length+' supporting submission'+((group.submissions||[]).length===1?'':'s')+'</small><small>'+esc((group.submissions||[]).map(sub=>person(sub.employee)+' · '+person(sub.store)).slice(0,2).join('; '))+'</small></button></div>').join('')||empty('No suggestions match these filters.');
}
function renderReviewSelection(){
 const area=$('#fi-queue-selection');if(!area)return;area.hidden=!state.reviewSelected.size;
 const unavailable=state.groups.some(group=>state.reviewSelected.has(String(group.id))&&unavailableGroup(group));
 area.innerHTML='<strong>'+state.reviewSelected.size+' groups selected</strong><div><button type="button" class="v2-button v2-button--secondary" data-batch-review="REJECTED">Reject selected</button> <button type="button" class="v2-button v2-button--primary" data-batch-review="APPROVED" '+(unavailable?'disabled':'')+'>Approve selected</button>'+(unavailable?'<p class="fi-hint">Some selected products are unavailable. You can reject them or inspect their retained history.</p>':'')+'</div>';
 area.querySelectorAll('[data-batch-review]').forEach(button=>button.onclick=()=>busy(button,()=>prepare({kind:'review',groupIds:[...state.reviewSelected].map(Number),decision:button.dataset.batchReview,reason:'Review compatible employee classification suggestions'})).catch(report));
}

function openGroup(id){
 const group=state.groups.find(group=>String(group.id)===String(id));if(!group)return;
 const unavailable=unavailableGroup(group);
 state.group=group;editorMode='review';renderGroups();
 const detail=$('#fi-group-detail');
 detail.innerHTML='<h2>'+esc(person(group.product))+'</h2><p class="fi-hint">'+esc(statusLabel(group.status))+' · Baseline '+esc(group.baseVersionId??'unpublished')+'</p>'+(group.status!=='PENDING'?'<p class="fi-hint">This review is complete. The comparison shows the submitted proposal against the current published classification, which may have changed since review. See History for the published result.</p>':'')+(unavailable?retainedIdentityMarkup(group.product):'')+'<button type="button" class="fi-text-button" id="fi-group-history">View history</button>'+diffMarkup(group.current,group.proposed,boot.taxonomy,state.all)+'<details class="fi-supporters"><summary>'+group.submissions.length+' individual submission'+(group.submissions.length===1?'':'s')+'</summary>'+group.submissions.map(sub=>'<div class="fi-supporter"><strong>'+esc(person(sub.employee))+'</strong> · '+esc(person(sub.store))+'<p>'+esc(when(sub.submittedAt))+'</p>'+(sub.comment?'<p>'+esc(sub.comment)+'</p>':'')+'<details><summary>Original proposed changes</summary><pre>'+esc(JSON.stringify(sub.proposedPatch,null,2))+'</pre></details></div>').join('')+'</details>'+(group.status==='PENDING'?'<div id="fi-modified-editor" hidden></div><label class="fi-field">Review note<input id="fi-review-reason" maxlength="500" value="Review employee classification suggestion"></label><div class="fi-actions"><button type="button" class="v2-button v2-button--secondary" data-review-decision="REJECTED">Reject</button>'+(!unavailable?'<button type="button" class="v2-button v2-button--secondary" id="fi-modify-review">Modify & approve</button><button type="button" class="v2-button v2-button--primary" data-review-decision="APPROVED">Approve</button>':'')+'</div>':'');
 $('#fi-group-history').onclick=()=>{state.historyId=group.productId;activateTab('history')};
 if(group.status!=='PENDING')return;
 let modified=null;
 if(!unavailable)$('#fi-modify-review').onclick=event=>{
  const area=$('#fi-modified-editor');
  if(!modified){area.hidden=false;modified=new FieldEditor(area,boot.taxonomy,state.all,group.proposed,{flavorId:productById(group.productId)?.flavorId});event.currentTarget.textContent='Preview modified approval';area.scrollIntoView({behavior:'smooth',block:'start'});return}
  busy(event.currentTarget,()=>prepare({kind:'review',groupIds:[group.id],decision:'MODIFIED_APPROVED',modifiedPatch:changedFields(group.current,modified.read()),reason:$('#fi-review-reason').value.trim()})).catch(report);
 };
 detail.querySelectorAll('[data-review-decision]').forEach(button=>button.onclick=()=>busy(button,()=>prepare({kind:'review',groupIds:[group.id],decision:button.dataset.reviewDecision,reason:$('#fi-review-reason').value.trim()})).catch(report));
}
function historyView(){
 const retained=state.groups.find(group=>group.productId===state.historyId&&unavailableGroup(group))?.product;
 const choices=retained&&!productById(retained.id)?[retained,...state.all]:state.all;
 panel.innerHTML='<form class="fi-toolbar" id="fi-history-search"><label class="fi-field fi-search">Product history<select id="fi-history-product"><option value="">Choose a product</option>'+choices.map(product=>'<option value="'+esc(product.id)+'">'+esc(title(product))+'</option>').join('')+'</select></label><button class="v2-button v2-button--secondary">Load history</button></form><div class="fi-split"><div class="fi-card fi-list" id="fi-history-list">'+empty('Choose a product to inspect its classification history.')+'</div><aside class="fi-card" id="fi-history-detail">'+empty('Restoring an earlier version creates a new auditable version.')+'</aside></div>';
 $('#fi-history-search').onsubmit=event=>{event.preventDefault();state.historyId=$('#fi-history-product').value;loadHistory(state.historyId)};
 if(state.historyId){$('#fi-history-product').value=state.historyId;loadHistory(state.historyId)}
}

async function loadHistory(id){
 if(!id)return;
 try{
  const data=await api('/products/'+encodeURIComponent(id)+'/history');if(state.tab!=='history'||String(state.historyId)!==String(id))return;state.history=data;
  const versions=data.versions||[],operations=data.operations||[],events=data.events||[],readOnly=Boolean(data.product?.catalogUnavailable||data.product?.historyReadOnly);
  $('#fi-history-detail').innerHTML=readOnly?retainedIdentityMarkup(data.product)+empty('Select a version to inspect its retained classification. This history is read-only.'):empty('Select a version to compare it with the current published classification.');
  $('#fi-history-list').innerHTML=(versions.map(version=>'<button type="button" class="fi-history-version" data-history-version="'+version.id+'"><strong>Version '+esc(version.version)+'</strong><small>'+esc(when(version.createdAt))+' · '+esc(person(version.actor))+'</small><small>'+esc(version.reason)+(version.restoresVersionId?' · Restores version '+esc(version.restoresVersionId):'')+'</small></button>').join('')||empty('No published classification versions yet.'))+(operations.length?'<div class="fi-card"><h3>Bulk operations</h3>'+operations.map(operation=>'<p><strong>'+esc(operation.kind||'Bulk edit')+' · '+esc(operation.id)+'</strong><br><small>'+esc(when(operation.createdAt))+' · '+esc(person(operation.actor))+'</small></p>'+(!readOnly?'<button type="button" class="fi-text-button" data-reverse-operation="'+esc(operation.id)+'">Preview safe reversal</button>':'')).join('')+'</div>':'')+(events.length?'<details class="fi-card"><summary>Audit events · '+events.length+'</summary>'+events.map(event=>'<details class="fi-supporter"><summary>'+esc(event.action||event.eventType||event.event_type||'Classification activity')+' · '+esc(person(event.actor))+' · '+esc(when(event.createdAt||event.occurredAt||event.occurred_at))+'</summary><pre>'+esc(JSON.stringify(event,null,2))+'</pre></details>').join('')+'</details>':'');
  $('#fi-history-list').onclick=event=>{const version=event.target.closest('[data-history-version]');if(version)showHistoryVersion(Number(version.dataset.historyVersion));const reverse=event.target.closest('[data-reverse-operation]');if(reverse)busy(reverse,()=>prepare({kind:'reverse',operationId:reverse.dataset.reverseOperation,reason:'Reverse bulk classification operation'})).catch(report)};
 }catch(error){report(error)}
}
function showHistoryVersion(id){
 const version=(state.history?.versions||[]).find(row=>Number(row.id)===id),product=state.history?.product||productById(state.historyId);if(!version||!product)return;
 if(product.catalogUnavailable||product.historyReadOnly){$('#fi-history-detail').innerHTML='<h2>Version '+esc(version.version)+'</h2>'+retainedIdentityMarkup(product)+'<p class="fi-hint">Retained classification · Read-only history</p>'+profileMarkup(version.snapshot,boot.taxonomy,state.all);return}
 $('#fi-history-detail').innerHTML='<h2>Restore version '+esc(version.version)+'</h2><p class="fi-hint">Compare the current published state with this historical version.</p>'+diffMarkup(product.classification,version.snapshot,boot.taxonomy,state.all)+'<label class="fi-field">Restoration note<input id="fi-restore-reason" maxlength="500" value="Restore classification version '+esc(version.version)+'"></label><div class="fi-actions"><button type="button" class="v2-button v2-button--primary" id="fi-preview-restore">Preview restoration</button></div>';
 $('#fi-preview-restore').onclick=event=>busy(event.currentTarget,()=>prepare({kind:'restore',productIds:[product.id],restoreVersionId:version.id,reason:$('#fi-restore-reason').value.trim()})).catch(report);
}
function activityView(){
 panel.innerHTML='<div class="fi-toolbar"><div><h2>Classification activity</h2><p class="fi-hint">Recorded activity and classification coverage. Select a figure to inspect its records.</p></div><button type="button" class="v2-button v2-button--secondary" id="fi-refresh-activity">Refresh</button></div><div id="fi-activity">'+empty('Loading recorded activity…')+'</div>';
 $('#fi-refresh-activity').onclick=()=>loadActivity();loadActivity();
}
async function loadActivity(){
 try{
  const data=await api('/analytics');if(state.tab!=='activity')return;const counts=data.counts||data;
  const value=key=>counts[key]===undefined?'—':Number(counts[key]).toLocaleString();
  const metrics=[['Pending suggestions','pendingSubmissions','suggestions','PENDING','Supporting submissions, grouped for review'],['Approved suggestions','approvedSubmissions','suggestions','approved','Includes approvals with reviewed modifications'],['Rejected suggestions','rejectedSubmissions','suggestions','REJECTED','Individual rejected submissions'],['Verified coverage','verifiedProducts','catalog','verified','of '+value('totalProducts')+' catalog products'],['Inferred guides','inferredProducts','catalog','inferred','Awaiting explicit classification review'],['Unclassified','unclassifiedProducts','catalog','unclassified','No published guide or inferred notes']];
  const table=(head,rows)=>'<div class="fi-table-wrap"><table class="fi-table"><thead><tr>'+head.map(value=>'<th>'+value+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  const employees=data.employees||[],repeated=data.repeatedProducts||[],recent=data.recentPublished||[],operations=data.operations||[],daily=data.daily||[];
  $('#fi-activity').innerHTML='<div class="fi-metrics">'+metrics.map(([label,key,tab,filter,detail])=>'<button type="button" class="fi-metric" data-drill-tab="'+tab+'" data-drill-filter="'+filter+'"><span>'+label+'</span><strong>'+value(key)+'</strong><small>'+esc(detail)+'</small></button>').join('')+'</div><p class="fi-hint">Approval rate: '+(Number(counts.decidedSubmissions)>0&&counts.approvalRate!=null&&Number.isFinite(Number(counts.approvalRate))?Math.round(Number(counts.approvalRate)*100)+'%':'—')+' · '+value('approvedSubmissions')+' approved of '+value('decidedSubmissions')+' decided submissions. Pending suggestions are excluded.</p><div class="fi-activity-grid"><section class="fi-card"><h2>Employee participation</h2>'+table(['Employee','Submitted','Approved'],employees.map(row=>'<tr><td><button class="fi-text-button" data-employee-drill="'+esc(person(row.employee))+'">'+esc(person(row.employee))+'</button><small>'+esc(person(row.store))+'</small></td><td>'+Number(row.submitted||0)+'</td><td>'+Number(row.approved||0)+'</td></tr>'))+(employees.length?'':empty('No submissions recorded yet.'))+'</section><section class="fi-card"><h2>Repeated corrections</h2>'+table(['Product','Submissions'],repeated.map(row=>'<tr><td><button class="fi-text-button" data-history-drill="'+esc(row.productId)+'">'+esc(person(row.product))+'</button></td><td>'+Number(row.corrections||0)+'</td></tr>'))+(repeated.length?'':empty('No repeatedly corrected products.'))+'</section><section class="fi-card"><h2>Recent published changes</h2>'+table(['Product','Who / when'],recent.map(row=>'<tr><td><button class="fi-text-button" data-history-drill="'+esc(row.productId)+'">'+esc(person(row.product))+'</button><small>'+esc(row.action||'Published')+'</small></td><td>'+esc(person(row.actor))+'<small>'+esc(when(row.at||row.createdAt))+'</small></td></tr>'))+(recent.length?'':empty('No publications recorded yet.'))+'</section><section class="fi-card"><h2>Submission frequency</h2>'+table(['Day','Submissions'],daily.map(row=>'<tr><td>'+esc(row.date)+'</td><td>'+Number(row.count||0)+'</td></tr>'))+(daily.length?'':empty('No submission activity yet.'))+'</section></div>'+(operations.length?'<section class="fi-card"><h2>Recent bulk operations</h2>'+table(['Operation','Who / when','Products',''],operations.map(row=>'<tr><td>'+esc(row.kind||'Bulk classification')+'<small>'+esc(row.id)+'</small></td><td>'+esc(person(row.actor))+'<small>'+esc(when(row.createdAt))+'</small></td><td>'+Number(row.productCount||0)+'</td><td><button class="fi-text-button" data-reverse-drill="'+esc(row.id)+'">Preview reversal</button></td></tr>'))+'</section>':'');
  $('#fi-activity').onclick=event=>{
   const drill=event.target.closest('[data-drill-tab]');if(drill){if(drill.dataset.drillTab==='suggestions'){state.queueStatus=drill.dataset.drillFilter;state.queueSearch='';activateTab('suggestions')}else{activateTab('catalog');$('#fi-confidence').value=drill.dataset.drillFilter;loadProducts()}return}
   const employee=event.target.closest('[data-employee-drill]');if(employee){state.queueStatus='';state.queueSearch=employee.dataset.employeeDrill;activateTab('suggestions');return}
   const history=event.target.closest('[data-history-drill]');if(history){state.historyId=history.dataset.historyDrill;activateTab('history');return}
   const reverse=event.target.closest('[data-reverse-drill]');if(reverse)busy(reverse,()=>prepare({kind:'reverse',operationId:reverse.dataset.reverseDrill,reason:'Reverse bulk classification operation'})).catch(report);
  };
 }catch(error){report(error)}
}
async function start(){
 try{
  boot=await api('/bootstrap');
  if(!boot.capabilities?.admin){panel.innerHTML=empty('Administrator permission is required for this workspace.');return}
  const data=await api('/products');state.all=data.items||[];
  $('#fi-principal').textContent=person(boot.principal)+(boot.principal.store?' · '+person(boot.principal.store):'');
  document.querySelectorAll('[data-fi-tab]').forEach(button=>button.onclick=()=>activateTab(button.dataset.fiTab));
  activateTab('catalog');
  $('#fi-discovery-tab').hidden=!boot.capabilities?.manageExplorerVisibility;
 }catch(error){report(error);panel.innerHTML=empty('The classification workspace is unavailable. Your existing data is unchanged.')}
}
start();
