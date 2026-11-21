import{api,esc,cleanName,when}from'./shared-admin.mjs';
import{safeProductImage}from'./data-source.mjs';
export const MAX_PHOTO_BYTES=2*1024*1024;
export function readRasterFile(file){
 return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('Unable to read this image. Choose the file again.'));reader.readAsDataURL(file)});
}
const modeLabel=mode=>({catalog:'Catalog photo',custom:'Custom photo',hidden:'Photo removed',mixed:'Mixed photo settings'}[mode]||'Photo');
const sourceLabel=kind=>({manufacturer:'Manufacturer photo',own:'Our photo',legacy:'Earlier unclassified photo',catalog:'Catalog photo'}[kind]||'Photo');
const historyLabel=entry=>entry.action==='remove'?'Removed '+sourceLabel(entry.sourceKind).toLowerCase()+' candidate':entry.action==='assign'?sourceLabel(entry.sourceKind):modeLabel(entry.policyMode||entry.mode);
const effectiveLabel=photo=>photo.mode==='custom'&&photo.effectiveSourceKind?sourceLabel(photo.effectiveSourceKind):photo.mode==='catalog'&&photo.effectiveSourceKind==='own'?'Existing original photo':modeLabel(photo.mode);
const emptyPhoto=text=>'<div class="fi-photo-empty">'+esc(text)+'</div>';

function candidateCards(photo,busy){
 const slots=['manufacturer','own','legacy'].map(kind=>{
  const row=photo.candidates?.[kind];if(!row)return '';
  const url=safeProductImage(row.url);
  return '<div><strong>'+esc(sourceLabel(kind))+'</strong>'+(row.mixed?'<p class="fi-hint">Mixed across current variants · '+esc(row.activeVariationCount)+' of '+esc(row.variationCount)+'</p>':(url?'<div class="fi-photo-preview"><img src="'+esc(url)+'" alt="'+esc(sourceLabel(kind))+'" referrerpolicy="no-referrer"></div>':''))+(row.sourceLabel?'<p class="fi-hint">'+esc(row.sourceLabel)+'</p>':'')+(kind!=='legacy'?'<button type="button" class="fi-text-button" data-photo-candidate-remove="'+kind+'" '+(busy?'disabled':'')+'>Remove '+esc(sourceLabel(kind).toLowerCase())+' candidate</button>':'<p class="fi-hint">Retained from an earlier upload; source was not classified.</p>')+'</div>';
 }).filter(Boolean);
 const original=safeProductImage(photo.originalFallback?.url);
 if(original)slots.push('<div><strong>Existing original · fallback</strong><div class="fi-photo-preview"><img src="'+esc(original)+'" alt="Existing original product photo" referrerpolicy="no-referrer"></div><p class="fi-hint">Preserved in the original photo library.</p></div>');
 return slots.length?'<details class="fi-photo-sources"><summary>Retained photos and fallbacks</summary><div class="fi-photo-grid">'+slots.join('')+'</div></details>':'';
}

export function createProductPhotosAdmin({request=api,readFile=readRasterFile,onClose=()=>{}}={}){
 let root=null,active=false,generation=0,fileGeneration=0,context=null;
 const find=selector=>root?.querySelector(selector);
 const path=id=>'/products/'+encodeURIComponent(id)+'/photo';
 const current=(ctx,sequence)=>active&&root&&context===ctx&&generation===sequence;
 function fields(){return Object.fromEntries(['sourcePageUrl','sourceImageUrl','sourceLabel','rightsNote'].map(key=>[key,find('[data-photo-field="'+key+'"]')?.value||'']))}
 function notify(text,error=false){if(!active)return;const area=find('[data-photo-message]');area.textContent=text;area.hidden=!text;area.className=error?'fi-notice fi-notice--error':'fi-notice'}
 function capture(ctx){if(ctx&&active&&context===ctx&&find('[data-photo-form]')){ctx.fields=fields();if(ctx.sourceKind)ctx.sourceFields[ctx.sourceKind]={...ctx.fields}}}
 function render(ctx,notice='',error=false){
  if(!active||context!==ctx)return;
  const photo=ctx.displayPhoto||ctx.photo,currentURL=safeProductImage(photo.url),history=photo.history||[],busy=Boolean(ctx.saving);
  root.innerHTML='<button type="button" class="fi-text-button fi-back-button" data-photo-close>← Back to Explorer setup</button><div class="fi-editor-heading"><div><h2>Product photo</h2><p>'+esc([ctx.product.brand,cleanName(ctx.product.name),ctx.product.type,ctx.product.sizeLabel].filter(Boolean).join(' · '))+'</p></div><button type="button" class="fi-text-button" data-photo-reload '+(busy?'disabled':'')+'>Reload current photo</button></div><div class="fi-notice" data-photo-message role="status" tabindex="-1" hidden></div><p class="fi-hint">Used in the staff E-liquid Explorer. Manufacturer photos stay primary; our photos are retained as fallbacks. Removing a candidate keeps other photos. Remove photo hides every source.</p><div class="fi-photo-grid"><div><strong>Current · '+esc(effectiveLabel(photo))+'</strong><div class="fi-photo-preview" data-photo-current>'+(currentURL?'<img src="'+esc(currentURL)+'" alt="'+esc(ctx.product.brand+' '+cleanName(ctx.product.name))+'" referrerpolicy="no-referrer">':emptyPhoto(photo.mode==='hidden'?'Photo removed':'No photo available'))+'</div></div><div><strong>Selected file</strong><div class="fi-photo-preview" data-photo-next>'+(ctx.file?'<img data-photo-selected alt="Selected product photo">':emptyPhoto('Choose a photo to upload or replace the current one'))+'</div><p class="fi-hint">'+esc(ctx.file?.name||'JPEG, PNG or WebP · Up to 2 MiB')+'</p></div></div><form data-photo-form><label class="fi-field">Photo source<select data-photo-source '+(busy?'disabled':'')+'><option value="">Choose source</option><option value="manufacturer" '+(ctx.sourceKind==='manufacturer'?'selected':'')+'>Manufacturer photo</option><option value="own" '+(ctx.sourceKind==='own'?'selected':'')+'>Our photo</option></select></label><label class="fi-field">Choose photo<input type="file" accept="image/jpeg,image/png,image/webp" data-photo-file '+(busy?'disabled':'')+'></label><details class="fi-photo-sources"><summary>Optional source and notes</summary><div class="fi-photo-fields"><label class="fi-field">Source page URL<input type="url" maxlength="2048" data-photo-field="sourcePageUrl" value="'+esc(ctx.fields.sourcePageUrl||'')+'" '+(busy?'disabled':'')+'></label><label class="fi-field">Source image URL<input type="url" maxlength="2048" data-photo-field="sourceImageUrl" value="'+esc(ctx.fields.sourceImageUrl||'')+'" '+(busy?'disabled':'')+'></label><label class="fi-field">Source label<input maxlength="300" data-photo-field="sourceLabel" value="'+esc(ctx.fields.sourceLabel||'')+'" '+(busy?'disabled':'')+'></label><label class="fi-field">Photo notes<textarea rows="2" maxlength="4000" data-photo-field="rightsNote" '+(busy?'disabled':'')+'>'+esc(ctx.fields.rightsNote||'')+'</textarea></label></div></details><div class="fi-actions"><button type="button" class="v2-button v2-button--secondary" data-photo-remove '+(busy||photo.mode==='hidden'?'disabled':'')+'>Remove photo</button><button type="button" class="v2-button v2-button--secondary" data-photo-catalog '+(busy||photo.mode==='catalog'?'disabled':'')+'>Use catalog photo</button><button type="submit" class="v2-button v2-button--primary" data-photo-save '+(busy||!ctx.file||!ctx.sourceKind?'disabled':'')+'>Save photo</button></div></form>'+candidateCards(photo,busy)+(history.length?'<details class="fi-photo-history"><summary>Photo history · '+history.length+'</summary>'+history.map((entry,index)=>'<div class="fi-photo-history-row"><div><strong>'+esc(historyLabel(entry))+'</strong><small>'+esc(when(entry.at))+(entry.sourceLabel?' · '+esc(entry.sourceLabel):'')+'</small></div>'+(entry.revertible?'<button type="button" class="fi-text-button" data-photo-restore="'+index+'" '+(busy?'disabled':'')+'>Restore</button>':'')+'</div>').join('')+'</details>':'');
  if(ctx.file)find('[data-photo-selected]').src=ctx.file.dataURL;
  find('[data-photo-form]').onsubmit=event=>{event.preventDefault();save('custom').catch(error=>notify(error.message,true))};
  find('[data-photo-file]').onchange=event=>selectFile(event.target.files?.[0]);
  find('[data-photo-source]').onchange=event=>{capture(ctx);ctx.sourceKind=['manufacturer','own'].includes(event.target.value)?event.target.value:'';ctx.fields=ctx.sourceFields[ctx.sourceKind]||photoFields(photo.candidates?.[ctx.sourceKind]||{});render(ctx)};
  for(const key of ['sourcePageUrl','sourceImageUrl','sourceLabel','rightsNote'])find('[data-photo-field="'+key+'"]').oninput=()=>capture(ctx);
  root.onclick=event=>{
   if(event.target.closest('[data-photo-close]')){capture(ctx);unmount();onClose()}
   else if(event.target.closest('[data-photo-remove]'))save('hidden').catch(error=>notify(error.message,true));
   else if(event.target.closest('[data-photo-catalog]'))save('catalog').catch(error=>notify(error.message,true));
   else if(event.target.closest('[data-photo-reload]'))reload().catch(error=>notify(error.message,true));
   else if(event.target.closest('[data-photo-candidate-remove]')){const kind=event.target.closest('[data-photo-candidate-remove]').dataset.photoCandidateRemove;if(['manufacturer','own'].includes(kind))save(null,null,kind).catch(error=>notify(error.message,true))}
   else{const restore=event.target.closest('[data-photo-restore]');if(restore){const entry=history[Number(restore.dataset.photoRestore)];if(entry?.revertible)save(entry.mode,entry.eventId).catch(error=>notify(error.message,true))}}
  };
  if(notice)notify(notice,error);
  else if(ctx.expectedToken!==photo.stateToken)notify('The current photo changed. Reload it to review the latest setting; your selected file is preserved.',true);
 }
 async function selectFile(file){
  const ctx=context,sequence=generation,readSequence=++fileGeneration;if(!ctx||!active||ctx.saving||!file)return;
  capture(ctx);
  if(!['image/jpeg','image/png','image/webp'].includes(file.type)||!Number.isFinite(file.size)||file.size<=0||file.size>MAX_PHOTO_BYTES){notify('Choose a JPEG, PNG or WebP image up to 2 MiB. Your previous selection is preserved.',true);return}
  notify('Reading selected photo…');
  try{
   const dataURL=await readFile(file);
   if(!current(ctx,sequence)||readSequence!==fileGeneration)return;
   const match=typeof dataURL==='string'&&dataURL.match(/^data:image\/(?:jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})$/);
   if(!match)throw new Error('This file could not be read as a JPEG, PNG or WebP image.');
   ctx.file={name:file.name||'Selected photo',dataURL,dataBase64:match[1]};render(ctx);
  }catch(error){if(current(ctx,sequence)&&readSequence===fileGeneration)notify(error.message||String(error),true)}
 }
 async function save(mode,restoreEventId=null,removeKind=null){
  const ctx=context,sequence=generation;if(!ctx||!active||ctx.saving)return;
  capture(ctx);
  if(mode==='custom'&&!restoreEventId&&!ctx.file)throw new Error('Choose a photo before saving.');
  if(mode==='custom'&&!restoreEventId&&!['manufacturer','own'].includes(ctx.sourceKind))throw new Error('Choose Manufacturer photo or Our photo before saving.');
  fileGeneration++;
  const selectedFile=ctx.file,savedKind=ctx.sourceKind,body={expectedToken:ctx.expectedToken};
  if(restoreEventId)Object.assign(body,{mode:['custom','catalog','hidden'].includes(mode)?mode:'catalog',restoreEventId});
  else if(removeKind)Object.assign(body,{action:'remove_candidate',sourceKind:removeKind});
  else{body.mode=mode;if(mode==='custom')Object.assign(body,{dataBase64:selectedFile.dataBase64,sourceKind:savedKind,...ctx.fields})}
  ctx.saving=true;render(ctx);
  try{
   const saved=await request(path(ctx.product.id),{method:'POST',body});
   if(!removeKind&&ctx.file===selectedFile)ctx.file=null;
   if(!current(ctx,sequence))return;
   const result=saved?.photo?saved:await request(path(ctx.product.id));
   if(!current(ctx,sequence))return;
   ctx.product=result.product;ctx.photo=result.photo;ctx.displayPhoto=result.photo;ctx.expectedToken=result.photo.stateToken;if(!removeKind){ctx.fields=photoFields(result.photo.candidates?.[ctx.sourceKind]||{});ctx.sourceFields={}};
   render(ctx,restoreEventId?'Earlier photo action restored.':removeKind?'Photo candidate removed; the current display policy is preserved.':savedKind==='own'&&mode==='custom'&&result.photo.effectiveSourceKind==='manufacturer'?'Our photo saved as fallback; the manufacturer photo remains primary.':mode==='hidden'?'Photo removed. The Explorer will show its neutral image frame.':mode==='catalog'?'Catalog photo selected.':'Photo saved. The Explorer refreshes automatically.');
  }catch(error){if(current(ctx,sequence))render(ctx,error.message||String(error),true)}
  finally{ctx.saving=false;if(current(ctx,sequence)){const notice=find('[data-photo-message]')?.textContent||'';const error=find('[data-photo-message]')?.className==='fi-notice fi-notice--error';render(ctx,notice,error)}}
 }
 function photoFields(photo){return Object.fromEntries(['sourcePageUrl','sourceImageUrl','sourceLabel','rightsNote'].map(key=>[key,photo[key]||'']))}
 async function reload(){
  const ctx=context,sequence=++generation;fileGeneration++;if(!ctx||!active||ctx.saving)return;
  capture(ctx);
  try{
   const result=await request(path(ctx.product.id));
   if(!current(ctx,sequence))return;
   ctx.product=result.product;ctx.photo=result.photo;ctx.displayPhoto=result.photo;ctx.expectedToken=result.photo.stateToken;
   if(!ctx.file){ctx.fields=photoFields(result.photo.candidates?.[ctx.sourceKind]||{});ctx.sourceFields={}}
   render(ctx,ctx.file?'Current photo reloaded. Your selected file is preserved; review it before saving.':'Current photo reloaded.');
  }catch(error){if(current(ctx,sequence))notify(error.message||String(error),true)}
 }
 async function mount(container,productId){
  if(context&&active)capture(context);
  const previous=context?.product.id===String(productId)?context:null,sequence=++generation;fileGeneration++;active=true;root=container;
  root.innerHTML='<button type="button" class="fi-text-button fi-back-button" data-photo-close>← Back to Explorer setup</button><p class="fi-empty">Loading product photo…</p>';
  root.onclick=event=>{if(event.target.closest('[data-photo-close]')){unmount();onClose()}};
  try{
   const result=await request(path(productId));
   if(!active||generation!==sequence)return;
   if(previous?.file){context={...previous,product:result.product,displayPhoto:result.photo,fields:{...previous.fields},sourceFields:{...previous.sourceFields},saving:false}}
   else context={product:result.product,photo:result.photo,displayPhoto:result.photo,expectedToken:result.photo.stateToken,fields:photoFields({}),sourceKind:'',sourceFields:{},file:null,saving:false};
   render(context);
  }catch(error){if(active&&generation===sequence)root.innerHTML='<button type="button" class="fi-text-button fi-back-button" data-photo-close>← Back to Explorer setup</button><p class="fi-empty">'+esc(error.message||String(error))+'</p>'}
 }
 function unmount(){if(context&&active)capture(context);active=false;generation++;fileGeneration++;if(root)root.onclick=null;root=null}
 return {mount,unmount};
}
