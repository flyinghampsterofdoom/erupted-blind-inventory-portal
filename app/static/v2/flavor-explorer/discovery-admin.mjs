import{api,esc,cleanName,empty}from'./shared-admin.mjs';
const visibilityLabel=value=>({included:'Included',excluded:'Excluded',partial:'Partially excluded'}[value]||'Unavailable');
const count=value=>Number.isFinite(Number(value))?Number(value).toLocaleString():'—';
const selectionKey=selected=>[...selected].sort().join('\n');
export function createDiscoveryAdmin({request=api,manageExplorerPhotos=false}={}){
 const state={query:'',type:'',visibility:'all',items:[],known:new Map(),selected:new Set(),reason:'',preview:null,message:'',error:false};
 let root=null,active=false,listSequence=0,previewSequence=0,committing=false,preparing=false,timer,photoManager=null,photoGeneration=0;
 const find=selector=>root?.querySelector(selector);
 function showMessage(text,error=false){state.message=text;state.error=error;if(!active)return;const area=find('[data-discovery-message]');area.textContent=text;area.hidden=!text;area.className=error?'fi-notice fi-notice--error':'fi-notice'}
 function report(error){showMessage(error.message||String(error),true)}
 function invalidatePreview(){previewSequence++;preparing=false;state.preview=null;if(active){find('[data-discovery-preview]').hidden=true;renderSelection()}}
 function renderSelection(){
  if(!active)return;
  const visible=state.items.filter(row=>state.selected.has(String(row.id))).length,outside=state.selected.size-visible;
  find('[data-discovery-selection]').hidden=!state.selected.size;
  find('[data-discovery-selected]').textContent=count(state.selected.size)+' selected';
  find('[data-discovery-outside]').textContent=outside?' · '+count(outside)+' outside current filters':'';
  find('[data-discovery-exclude]').disabled=!state.selected.size||preparing||committing;
  find('[data-discovery-restore]').disabled=!state.selected.size||preparing||committing;
  find('[data-discovery-note-wrap]').hidden=!state.selected.size;
  const checkbox=find('[data-discovery-select-matching]');checkbox.checked=Boolean(state.items.length)&&visible===state.items.length;checkbox.indeterminate=visible>0&&visible<state.items.length;
 }
 function renderRows(){
  if(!active)return;
  find('[data-discovery-products]').innerHTML=state.items.map(row=>'<div class="fi-row '+(state.selected.has(String(row.id))?'is-selected':'')+'"><input type="checkbox" data-discovery-product="'+esc(row.id)+'" aria-label="Select '+esc((row.brand||'')+' '+cleanName(row.name))+'" '+(state.selected.has(String(row.id))?'checked':'')+'><div><div class="fi-row-title">'+esc(cleanName(row.name))+'</div><div class="fi-row-meta"><span>'+esc(row.brand)+' · '+esc(row.type)+(row.sizeLabel?' · '+esc(row.sizeLabel):'')+'</span><span class="v2-badge '+(row.explorerVisibility==='excluded'?'v2-badge--warning':row.explorerVisibility==='included'?'v2-badge--success':'v2-badge--warning')+'">'+esc(visibilityLabel(row.explorerVisibility))+'</span><span>'+count(row.excludedVariationCount)+' of '+count(row.variationCount)+' current variants excluded</span></div></div>'+(manageExplorerPhotos?'<button type="button" class="v2-button v2-button--secondary" data-discovery-photo="'+esc(row.id)+'">Photo</button>':'')+'</div>').join('')||empty('No products match these filters.');
  find('[data-discovery-product-count]').textContent=count(state.items.length)+' matching products';renderSelection();
 }
 async function loadProducts(){
  if(!active)return;
  const sequence=++listSequence,host=root,query=new URLSearchParams({q:state.query,type:state.type,visibility:state.visibility});
  try{
   const result=await request('/discovery/products?'+query);
   if(!active||host!==root||sequence!==listSequence)return;
   state.items=result.items||result.products||[];for(const row of state.items)state.known.set(String(row.id),row);renderRows();
  }catch(error){if(active&&host===root&&sequence===listSequence)report(error)}
 }
 function filterChanged(){
  state.query=find('[data-discovery-search]').value;state.type=find('[data-discovery-type]').value;state.visibility=find('[data-discovery-visibility]').value;
  listSequence++;invalidatePreview();
 }
 function changeSelection(id,checked){
  checked?state.selected.add(String(id)):state.selected.delete(String(id));invalidatePreview();renderRows();
 }
 async function openPhoto(productId){
  if(!active||!manageExplorerPhotos)return;
  const sequence=++photoGeneration,host=root,area=find('[data-discovery-photo-panel]');area.hidden=false;
  if(!photoManager){
   area.innerHTML=empty('Loading photo tools…');
   const module=await import('./product-photos-admin.mjs');
   if(!active||root!==host||sequence!==photoGeneration)return;
   photoManager=module.createProductPhotosAdmin({request,onClose:()=>{if(active){photoGeneration++;find('[data-discovery-photo-panel]').hidden=true}}});
  }
  if(!active||root!==host||sequence!==photoGeneration)return;
  await photoManager.mount(area,productId);
  if(active&&root===host&&sequence===photoGeneration)area.scrollIntoView({behavior:'smooth',block:'start'});
 }
 function renderPreview(){
  if(!active||!state.preview)return;
  const plan=state.preview,area=find('[data-discovery-preview]'),label=plan.excluded?'Exclude from E-liquid Explorer':'Restore to E-liquid Explorer';
  area.hidden=false;
  area.innerHTML='<h3>'+label+'</h3><p><strong>'+count(plan.changedCount)+' products</strong> · '+count(plan.variationChangedCount)+' current variants will change. '+count(plan.unchangedCount)+' products already have this setting.</p>'+(plan.warnings||[]).map(warning=>'<p class="fi-warning">'+esc(warning)+'</p>').join('')+'<details class="fi-preview-products"><summary>Review selected products · '+count(plan.selectedCount??plan.products?.length??0)+'</summary>'+(plan.products||[]).map(row=>'<p><strong>'+esc([row.brand,cleanName(row.name)].filter(Boolean).join(' · '))+'</strong><br>'+esc(visibilityLabel(row.before))+' → '+esc(visibilityLabel(row.after))+' · '+count(row.variationCount??state.known.get(String(row.id))?.variationCount)+' current variants'+(row.excludedVariationCount!=null&&row.afterExcludedVariationCount!=null?' · excluded '+count(row.excludedVariationCount)+' → '+count(row.afterExcludedVariationCount):'')+'</p>').join('')+'</details><p class="fi-hint">Note: '+esc(plan.reason)+'</p><div class="fi-actions"><button type="button" class="v2-button v2-button--secondary" data-discovery-cancel>Cancel</button><button type="button" class="v2-button v2-button--primary" data-discovery-apply '+(!plan.changedCount||committing?'disabled':'')+'>'+(plan.excluded?'Apply exclusions':'Restore products')+'</button></div>';
 }
 async function prepare(excluded){
  if(!active||!state.selected.size||committing)return;
  const ids=[...state.selected].sort(),signature=selectionKey(state.selected),sequence=++previewSequence,host=root;
  state.preview=null;preparing=true;find('[data-discovery-preview]').hidden=true;renderSelection();showMessage('');
  const reason=state.reason.trim()||(excluded?'No longer carried in E-liquid Explorer':'Restore to E-liquid Explorer');
  try{
   const result=await request('/discovery/preview',{method:'POST',body:{productIds:ids,excluded,reason}});
   if(!active||root!==host||sequence!==previewSequence||selectionKey(state.selected)!==signature)return;
   state.preview={...result,excluded,reason,signature,ids};renderPreview();
  }catch(error){if(active&&root===host&&sequence===previewSequence)report(error)}
  finally{if(active&&root===host&&sequence===previewSequence){preparing=false;renderSelection()}}
 }
 async function commit(){
  const plan=state.preview;if(!active||!plan||committing||plan.signature!==selectionKey(state.selected)||!plan.changedCount)return;
  committing=true;const host=root;renderSelection();renderPreview();
  try{
   await request('/discovery/commit',{method:'POST',body:{previewId:plan.previewId}});
   if(state.preview===plan)state.preview=null;
   state.message=(plan.excluded?'Excluded ':'Restored ')+count(plan.changedCount)+' product'+(Number(plan.changedCount)===1?'':'s')+' ('+count(plan.variationChangedCount)+' current variant'+(Number(plan.variationChangedCount)===1?'':'s')+').';state.error=false;
   if(active&&root===host){find('[data-discovery-preview]').hidden=true;showMessage(state.message);await loadProducts()}
  }catch(error){if(active&&root===host)report(error);else{state.message=error.message||String(error);state.error=true}}
  finally{committing=false;if(active&&root===host){renderSelection();if(state.preview)renderPreview()}}
 }
 async function mount(container){
  if(active&&root===container)return;
  root=container;active=true;
  root.innerHTML='<div class="fi-toolbar"><div><h2>Explorer setup</h2><p class="fi-hint">Choose which products appear in the staff E-liquid Explorer. Changes apply at all four stores to current variants; newly added variants start included. Zero stock does not change visibility.</p></div></div><div class="fi-notice" data-discovery-message role="status" tabindex="-1" hidden></div><form class="fi-toolbar" data-discovery-filter><label class="fi-field fi-search">Search products<input type="search" data-discovery-search placeholder="Product, brand or flavor"></label><label class="fi-field">Product type<select data-discovery-type><option value="">All types</option><option value="freebase">Freebase</option><option value="salt">Nicotine salts</option><option value="disposable">Disposables</option></select></label><label class="fi-field">Explorer visibility<select data-discovery-visibility><option value="all">All products</option><option value="included">Included (including partial)</option><option value="excluded">Excluded</option></select></label><button class="v2-button v2-button--secondary">Search</button></form><div class="fi-selection fi-toolbar" data-discovery-selection hidden><div><strong data-discovery-selected></strong><span data-discovery-outside></span></div><button type="button" class="v2-button v2-button--secondary" data-discovery-exclude>Exclude from E-liquid Explorer</button><button type="button" class="v2-button v2-button--secondary" data-discovery-restore>Restore to E-liquid Explorer</button><button type="button" class="fi-text-button" data-discovery-clear>Clear selection</button></div><div class="fi-toolbar" data-discovery-note-wrap hidden><label class="fi-field fi-search">Optional change note<input data-discovery-note maxlength="500" placeholder="Optional; a standard note is recorded if left blank"></label></div><section class="fi-card fi-product-photo-editor" data-discovery-photo-panel aria-label="Product photo editor" hidden></section><section class="fi-card fi-preview" data-discovery-preview aria-label="Review Explorer visibility change" hidden></section><div class="fi-card fi-list"><div class="fi-list-header"><label><input type="checkbox" data-discovery-select-matching> Select matching products</label><span data-discovery-product-count></span></div><div data-discovery-products></div></div>';
  find('[data-discovery-search]').value=state.query;find('[data-discovery-type]').value=state.type;find('[data-discovery-visibility]').value=state.visibility;find('[data-discovery-note]').value=state.reason;
  find('[data-discovery-filter]').onsubmit=event=>{event.preventDefault();clearTimeout(timer);filterChanged();loadProducts()};
  find('[data-discovery-search]').oninput=()=>{clearTimeout(timer);filterChanged();timer=setTimeout(loadProducts,180)};
  for(const selector of ['[data-discovery-type]','[data-discovery-visibility]'])find(selector).onchange=()=>{clearTimeout(timer);filterChanged();loadProducts()};
  find('[data-discovery-note]').oninput=event=>{state.reason=event.target.value;invalidatePreview()};
  root.onchange=event=>{
   const matching=event.target.closest('[data-discovery-select-matching]');if(matching){for(const row of state.items)matching.checked?state.selected.add(String(row.id)):state.selected.delete(String(row.id));invalidatePreview();renderRows();return}
   const product=event.target.closest('[data-discovery-product]');if(product)changeSelection(product.dataset.discoveryProduct,product.checked);
  };
  root.onclick=event=>{
   const photo=event.target.closest('[data-discovery-photo]');if(photo){openPhoto(photo.dataset.discoveryPhoto).catch(report);return}
   if(event.target.closest('[data-discovery-exclude]'))prepare(true);
   else if(event.target.closest('[data-discovery-restore]'))prepare(false);
   else if(event.target.closest('[data-discovery-clear]')){state.selected.clear();invalidatePreview();renderRows()}
   else if(event.target.closest('[data-discovery-cancel]'))invalidatePreview();
   else if(event.target.closest('[data-discovery-apply]'))commit();
  };
  renderRows();showMessage(state.message,state.error);await loadProducts();
 }
 function unmount(){
  photoGeneration++;photoManager?.unmount();active=false;listSequence++;previewSequence++;clearTimeout(timer);preparing=false;state.preview=null;
  if(root){root.onclick=null;root.onchange=null}root=null;
 }
 return {mount,unmount};
}
