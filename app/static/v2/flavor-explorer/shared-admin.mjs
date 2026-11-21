export const API='/v2/flavor-intelligence/api';
export const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export const cleanName=value=>String(value||'').replace(/^\*+\s*/,'').replace(/ · \*+/g,' · ');
export const person=value=>typeof value==='object'&&value?(value.username||value.name||value.label||'—'):String(value??'—');
export function when(value){if(!value)return '—';const date=new Date(value);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en-US',{dateStyle:'medium',timeStyle:'short',timeZone:'America/Los_Angeles'}).format(date):'—'}
export const confidenceLabel=value=>({verified:'Verified',provisional:'Pending review',inferred:'Inferred guide',unclassified:'Unclassified',existing_verified:'Verified',established:'Verified'}[value]||value||'Unclassified');
export const statusLabel=value=>({PENDING:'Pending',APPROVED:'Approved',MODIFIED_APPROVED:'Modified & approved',REJECTED:'Rejected'}[value]||value||'—');
export const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
export function classification(value={}){
 const sensory={};
 for(const [key,number] of Object.entries(value.sensory||{}).sort(([a],[b])=>a.localeCompare(b)))if(typeof number==='number'&&Number.isFinite(number)&&number>=0&&number<=1)sensory[key]=number;
 return {description:String(value.description||''),notes:[...new Set(value.notes||[])].sort(),categories:[...new Set((value.categories||[]).map(Number).filter(value=>Number.isInteger(value)&&value>0))].sort((a,b)=>a-b),sensory,
  similarities:(value.similarities||[]).map(row=>({flavor_id:Number(row.flavor_id),weight:Number(row.weight),reason:String(row.reason||'')})).sort((a,b)=>a.flavor_id-b.flavor_id)};
}
export function changedFields(before,after){
 const a=classification(before),b=classification(after),changes={};
 for(const key of ['description','notes','categories','similarities'])if(!same(a[key],b[key]))changes[key]=b[key];
 const sensory={};
 for(const key of new Set([...Object.keys(a.sensory),...Object.keys(b.sensory)]))
  if(a.sensory[key]!==b.sensory[key])sensory[key]=b.sensory[key]??null;
 if(Object.keys(sensory).length)changes.sensory=sensory;
 return changes;
}
export async function api(path,{method='GET',body}={}){
 const headers={Accept:'application/json'};
 if(method!=='GET'){
  headers['Content-Type']='application/json';
  const cookie=document.cookie.split(';').map(part=>part.trim()).find(part=>part.startsWith('csrf_token='));
  if(cookie)headers['X-CSRF-Token']=decodeURIComponent(cookie.slice('csrf_token='.length));
 }
 let response;
 try{response=await fetch(API+path,{method,headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)})}
 catch{throw new Error('Unable to connect. Your edits are still here; try again.')}
 const data=await response.json().catch(()=>({}));
 if(!response.ok){
  let detail=typeof data.detail==='string'?data.detail:Array.isArray(data.detail)?data.detail.map(row=>row.msg||String(row)).join('; '):data.message;
  if(response.status===401)detail='Your session ended. Sign in again in another tab, then retry. Your edits are still here.';
  if(response.status===409)detail=(detail||'This record changed while you were working.')+' Your draft is preserved; reload the current record before trying again.';
  const error=new Error(detail||'The action could not be completed. Your edits are still here.');error.status=response.status;throw error;
 }
 return data;
}
export function notice(element,message,error=false){
 element.hidden=!message;element.className=error?'fi-notice fi-notice--error':'fi-notice';
 element.textContent=message||'';if(error)element.focus({preventScroll:true});
}
export async function busy(button,action){
 const old=button.textContent;button.disabled=true;
 try{return await action()}finally{if(button.isConnected){button.disabled=false;button.textContent=old}}
}
export function empty(message){return '<p class="fi-empty">'+esc(message)+'</p>'}
const level=value=>['None','Light','Light','Moderate','Intense','Intense'][Math.round(value*5)]||'Unclassified';
const sensoryPercent=new Intl.NumberFormat('en-US',{style:'percent',maximumFractionDigits:15});
const sensoryValue=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=1?level(value)+' · '+sensoryPercent.format(value):'Unclassified';
export function labelMaps(taxonomy){
 return {notes:new Map((taxonomy.notes||[]).map(row=>[row.slug,row.label||row.slug])),
  categories:new Map((taxonomy.categories||[]).map(row=>[Number(row.id),row.name||row.label||row.slug])),
  axes:new Map((taxonomy.sensoryAxes||[]).map(row=>[row.slug,row.label||row.slug]))};
}
export function fieldValue(key,value,taxonomy,products=[]){
 const maps=labelMaps(taxonomy);
 if(key==='notes')return (value||[]).map(id=>maps.notes.get(id)||id).join(', ')||'—';
 if(key==='categories')return (value||[]).map(id=>maps.categories.get(Number(id))||'Category '+id).join(', ')||'—';
 if(key==='sensory')return Object.entries(value||{}).map(([axis,intensity])=>(maps.axes.get(axis)||axis)+': '+sensoryValue(intensity)).join('; ')||'Unclassified';
 if(key==='similarities')return (value||[]).map(row=>{const product=products.find(product=>Number(product.flavorId)===Number(row.flavor_id));return (product?cleanName(product.name):'Flavor '+row.flavor_id)+' ('+Math.round(row.weight*100)+'%)'+(row.reason?' · '+row.reason:'')}).join('; ')||'—';
 return String(value||'')||'—';
}
export function diffMarkup(before,after,taxonomy,products=[]){
 const a=classification(before),b=classification(after);
 const names={description:'Description',notes:'Flavor notes',categories:'Categories',sensory:'Sensory profile',similarities:'Similar flavors'};
 const fields=Object.keys(names).filter(key=>!same(a[key],b[key]));
 return fields.length?'<div class="fi-diff">'+fields.map(key=>'<div class="fi-diff-row"><strong>'+names[key]+'</strong><div><span class="fi-diff-label">Before</span><p>'+esc(fieldValue(key,a[key],taxonomy,products))+'</p></div><div class="fi-diff-after"><span class="fi-diff-label">After</span><p>'+esc(fieldValue(key,b[key],taxonomy,products))+'</p></div></div>').join('')+'</div>':empty('No classification changes.');
}
export function profileMarkup(profile,taxonomy,products=[]){
 const value=classification(profile);
 const lines=['notes','categories','sensory','similarities'].filter(key=>key==='sensory'?Object.keys(value[key]).length:value[key].length)
  .map(key=>'<p><strong>'+({notes:'Notes',categories:'Categories',sensory:'Sensory',similarities:'Similar flavors'}[key])+':</strong> '+esc(fieldValue(key,value[key],taxonomy,products))+'</p>').join('');
 return (value.description?'<p>'+esc(value.description)+'</p>':'')+lines||empty('No published classification yet.');
}
export function normalizedNote(text,taxonomy){
 const label=String(text||'').trim();
 if(!label||label.length>80)throw new Error('Enter a flavor note from 1 to 80 characters.');
 const existing=(taxonomy.notes||[]).find(row=>row.slug===label.toLowerCase()||String(row.label||row.slug).toLowerCase()===label.toLowerCase());
 if(existing)return {slug:existing.slug,label:existing.label||existing.slug};
 const slug=label.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
 if(!slug||slug.length>80)throw new Error('Use a short flavor name containing letters or numbers.');
 const recovered=(taxonomy.notes||[]).find(row=>row.slug===slug);
 return recovered?{slug:recovered.slug,label:recovered.label||recovered.slug}:{slug,label};
}
export function hasProfile(profile){
 const value=classification(profile);
 return Boolean(value.description||value.notes.length||value.categories.length||Object.keys(value.sensory).length||value.similarities.length);
}
let editorNumber=0;
export class FieldEditor{
 constructor(element,taxonomy,products,profile={},options={}){
  this.element=element;this.taxonomy=taxonomy;this.products=products;this.value=classification(profile);this.options=options;this.uid='fi-editor-'+(++editorNumber);this.render();
 }
 render(){
  const value=this.value,notes=[...(this.taxonomy.notes||[])],categories=[...(this.taxonomy.categories||[])];
  for(const slug of value.notes)if(!notes.some(row=>row.slug===slug))notes.push({slug,label:slug});
  for(const id of value.categories)if(!categories.some(row=>Number(row.id)===id))categories.push({id,name:'Category '+id});
  const tokenSet=(items,key)=>'<div class="fi-tokens" data-token-set="'+key+'">'+items.map(row=>{const id=key==='notes'?row.slug:Number(row.id),label=row.label||row.name||row.slug;return '<label class="fi-token"><input type="checkbox" data-field="'+key+'" value="'+esc(id)+'" '+(value[key].includes(id)?'checked':'')+'><span>'+esc(label)+'</span></label>'}).join('')+'</div>';
  const full=this.options.onlySimilarities?'':'<fieldset><legend>Flavor notes</legend>'+tokenSet(notes,'notes')+'<div class="fi-add-related fi-add-note"><input type="text" data-new-note placeholder="Add another flavor note" maxlength="80" aria-label="New flavor note"><button type="button" class="v2-button v2-button--secondary" data-add-note>Add note</button></div></fieldset><fieldset><legend>Categories</legend>'+tokenSet(categories,'categories')+'</fieldset><fieldset><legend>Sensory profile</legend><p class="fi-hint">Check an attribute to classify it. An unchecked attribute stays unclassified.</p><div class="fi-axes">'+(this.taxonomy.sensoryAxes||[]).map(row=>{
   const known=value.sensory[row.slug]!==undefined,n=known?Math.round(value.sensory[row.slug]*5):0;
   return '<div class="fi-axis"><label><input type="checkbox" data-axis-known="'+esc(row.slug)+'" '+(known?'checked':'')+'> '+esc(row.label||row.slug)+'</label><input type="range" min="0" max="5" step="1" value="'+n+'" data-axis="'+esc(row.slug)+'" data-original-value="'+(known?value.sensory[row.slug]:'')+'" aria-label="'+esc(row.label||row.slug)+' intensity" '+(known?'':'disabled')+'><output data-axis-output="'+esc(row.slug)+'">'+(known?level(n/5):'Unclassified')+'</output></div>';
  }).join('')+'</div></fieldset><label class="fi-field">Flavor description<textarea data-description maxlength="4000" rows="3">'+esc(value.description)+'</textarea></label>';
  this.element.innerHTML=full+'<fieldset><legend>Similar flavors</legend><div class="fi-relations" data-relations></div><div class="fi-add-related"><input type="search" list="'+this.uid+'-options" data-related-search placeholder="Find a related product" aria-label="Find a related product"><datalist id="'+this.uid+'-options">'+this.products.filter(row=>row.flavorId&&Number(row.flavorId)!==Number(this.options.flavorId)).map(row=>'<option value="'+esc(this.relatedLabel(row))+'"></option>').join('')+'</datalist><button type="button" class="v2-button v2-button--secondary" data-add-related>Add</button></div><p class="fi-hint">Relationships guide flavor recommendations. They never bypass product type or strength filters.</p></fieldset>';
  this.renderRelations();
  const addNote=this.element.querySelector('[data-add-note]');
  if(addNote)addNote.onclick=()=>{
   const input=this.element.querySelector('[data-new-note]');
   try{
    const note=normalizedNote(input.value,this.taxonomy),set=this.element.querySelector('[data-token-set="notes"]');
    let checkbox=set.querySelector('input[value="'+CSS.escape(note.slug)+'"]');
    if(!checkbox){const label=document.createElement('label');label.className='fi-token';label.innerHTML='<input type="checkbox" data-field="notes" value="'+esc(note.slug)+'"><span>'+esc(note.label)+'</span>';set.append(label);checkbox=label.querySelector('input')}
    checkbox.checked=true;input.value='';input.setCustomValidity('');checkbox.focus();
   }catch(error){input.setCustomValidity(error.message);input.reportValidity()}
  };
  this.element.onchange=event=>{const axis=event.target.dataset.axisKnown;if(axis!==undefined){const slider=this.element.querySelector('[data-axis="'+CSS.escape(axis)+'"]');slider.disabled=!event.target.checked;slider.dataset.changed='true';this.updateAxis(axis)}};
  this.element.oninput=event=>{if(event.target.dataset.axis){event.target.dataset.changed='true';this.updateAxis(event.target.dataset.axis)}};
  this.element.querySelector('[data-add-related]').addEventListener('click',()=>{
   const input=this.element.querySelector('[data-related-search]'),product=this.products.find(row=>this.relatedLabel(row)===input.value);
   if(!product||!product.flavorId||Number(product.flavorId)===Number(this.options.flavorId)){input.setCustomValidity('Choose a related product from the list.');input.reportValidity();return}
   input.setCustomValidity('');this.captureRelations();
   if(!this.value.similarities.some(row=>row.flavor_id===Number(product.flavorId)))this.value.similarities.push({flavor_id:Number(product.flavorId),weight:.75,reason:'Related flavor profile'});
   input.value='';this.renderRelations();
  });
  this.element.querySelector('[data-relations]').addEventListener('click',event=>{const button=event.target.closest('[data-remove-related]');if(button){this.captureRelations();this.value.similarities=this.value.similarities.filter(row=>row.flavor_id!==Number(button.dataset.removeRelated));this.renderRelations()}});
 }
 relatedLabel(product){return product.brand+' · '+cleanName(product.name)+' · #'+product.flavorId}
 renderRelations(){
  this.element.querySelector('[data-relations]').innerHTML=this.value.similarities.map(row=>{
   const product=this.products.find(product=>Number(product.flavorId)===row.flavor_id);
   return '<div class="fi-relation" data-relation="'+row.flavor_id+'"><strong>'+esc(product?product.brand+' · '+cleanName(product.name):'Flavor '+row.flavor_id)+'</strong><label>Weight<input type="number" min=".05" max="1" step=".05" value="'+row.weight+'" data-relation-weight></label><label>Relationship<input type="text" maxlength="500" value="'+esc(row.reason)+'" data-relation-reason></label><button type="button" class="fi-text-button" data-remove-related="'+row.flavor_id+'">Remove</button></div>';
  }).join('')||'<p class="fi-hint">No manually linked flavors.</p>';
 }
 captureRelations(){this.value.similarities=[...this.element.querySelectorAll('[data-relation]')].map(row=>({flavor_id:Number(row.dataset.relation),weight:Number(row.querySelector('[data-relation-weight]').value),reason:row.querySelector('[data-relation-reason]').value.trim()}))}
 updateAxis(axis){const checked=this.element.querySelector('[data-axis-known="'+CSS.escape(axis)+'"]').checked;const slider=this.element.querySelector('[data-axis="'+CSS.escape(axis)+'"]');this.element.querySelector('[data-axis-output="'+CSS.escape(axis)+'"]').textContent=checked?level(Number(slider.value)/5):'Unclassified'}
 read(){
  this.captureRelations();
  if(this.options.onlySimilarities)return classification(this.value);
  const sensory={};for(const input of this.element.querySelectorAll('[data-axis-known]'))if(input.checked){const slider=this.element.querySelector('[data-axis="'+CSS.escape(input.dataset.axisKnown)+'"]');sensory[input.dataset.axisKnown]=slider.dataset.changed==='true'||slider.dataset.originalValue===''?Number(slider.value)/5:Number(slider.dataset.originalValue)}
  return classification({description:this.element.querySelector('[data-description]').value,notes:[...this.element.querySelectorAll('[data-field="notes"]:checked')].map(input=>input.value),categories:[...this.element.querySelectorAll('[data-field="categories"]:checked')].map(input=>Number(input.value)),sensory,similarities:this.value.similarities});
 }
 set(profile){this.value=classification(profile);this.render()}
}
export function previewMarkup(preview,taxonomy,products=[]){
 return '<div class="fi-preview-header"><div><p class="v2-eyebrow">Review before publishing</p><h2>'+Number(preview.changedCount||0)+' product'+(Number(preview.changedCount)===1?'':'s')+' will change</h2><p class="fi-hint">'+Number(preview.unchangedCount||0)+' unchanged'+(preview.expiresAt?' · Preview expires '+esc(when(preview.expiresAt)):'')+'</p></div></div>'+(preview.warnings||[]).map(warning=>'<p class="fi-warning">'+esc(typeof warning==='string'?warning:warning.message||JSON.stringify(warning))+'</p>').join('')+'<div class="fi-preview-products">'+(preview.products||[]).map(product=>'<details '+((preview.products||[]).length<=3?'open':'')+'><summary>'+esc(cleanName(product.name||product.id))+'</summary>'+diffMarkup(product.before,product.after,taxonomy,products)+'</details>').join('')+'</div><div class="fi-actions"><button type="button" class="v2-button v2-button--secondary" data-cancel-preview>Back to editing</button><button type="button" class="v2-button v2-button--primary" data-confirm-preview>Confirm this operation</button></div>';
}
