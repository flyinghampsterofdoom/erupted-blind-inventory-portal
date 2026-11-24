import {NOTES,CATEGORIES,STORES} from './catalog.mjs';
import {loadCatalog,safeProductImage,productImageCaption} from './data-source.mjs';
let catalogProducts=[],catalogNotes=[...NOTES],catalogStores=STORES,catalogMeta=null,catalogLoadError='',visibleLimit=24,currentPayload=null,currentExpiryState=false;
import {discover,availabilityFor,brandNamesFor,brandKey} from './engine.mjs';
import {productArt,categoryArt} from './art.mjs';

const $=selector=>document.querySelector(selector);
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const noteMap=new Map(NOTES.map(n=>[n.id,n]));
const state={type:'all',strength:null,brand:'',sensory:[],notes:[],categories:[],query:'',mode:'explore',storeId:catalogStores[0].id};
const noteDefaults=['raspberry','strawberry','mango','watermelon','vanilla','mint'];
const labels={available:'Available',out_of_stock:'Out of stock',stale:'Check with staff',check_staff:'Check with staff',unknown:'Availability unknown'};
const sensoryLabels={sweetness:'Sweet',tartness:'Tart',creaminess:'Creamy',richness:'Rich',brightness:'Bright',cooling:'Cooling',crispness:'Crisp'};
let expiryTimer=null,catalogFetchInFlight=false;
let showAllNotes=false,currentResults=[],detailProduct=null,detailStrength=null,detailSource=null;
const mg=value=>value==null?'Strength unconfirmed':Number(value)===0?'0 mg':Number(value)+' mg';
const nameForNote=id=>noteMap.get(id)?.label||id;
const intensity=value=>value==null?'Unclassified':Number(value)<=0?'None':Number(value)<=2?'Light':Number(value)===3?'Moderate':'Intense';

function applyNoteDefinitions(data){
 const canonical=new Map(NOTES.map(note=>[note.id,{...note}]));
 const additions=new Map();
 for(const definition of Array.isArray(data.noteDefinitions)?data.noteDefinitions:[]){
  if(!definition||typeof definition.id!=='string'||!/^[a-z0-9-]{1,80}$/.test(definition.id)||typeof definition.label!=='string'||!definition.label.trim())continue;
  const note={...(canonical.get(definition.id)||{}),id:definition.id,label:definition.label.trim().slice(0,120)};
  if(canonical.has(note.id))canonical.set(note.id,note);else additions.set(note.id,note);
 }
 catalogNotes=[...canonical.values(),...additions.values()].sort((left,right)=>{
  const a=NOTES.findIndex(note=>note.id===left.id),b=NOTES.findIndex(note=>note.id===right.id);
  if(a>=0&&b>=0)return a-b;if(a>=0)return -1;if(b>=0)return 1;
  return left.label.localeCompare(right.label);
 });
 // Retain only display labels for active removed notes, not every past option.
 // The filter itself remains selected and cannot silently broaden recommendations.
 const activeLabels=new Map(state.notes.map(id=>[id,noteMap.get(id)]));
 noteMap.clear();
 for(const note of catalogNotes)noteMap.set(note.id,note);
 for(const [id,note] of activeLabels)if(note&&!noteMap.has(id))noteMap.set(id,note);
}


function visualFor(product,priority=false){
 const image=safeProductImage(product.imageUrl);
 return productArt(product)+(image?'<img class="square-product-image" src="'+escapeHtml(image)+'" alt="'+escapeHtml(product.brand+' '+displayName(product))+'" loading="'+(priority?'eager':'lazy')+'" decoding="async" referrerpolicy="no-referrer">':'');
}
function renderHero(){
 const hero=catalogProducts.find(product=>safeProductImage(product.imageUrl)&&product.notes.includes('raspberry'))||catalogProducts.find(product=>safeProductImage(product.imageUrl)&&product.type==='freebase')||catalogProducts[0];
 const target=$('#hero-art');
 target.classList.remove('has-square-image');
 target.innerHTML=hero?visualFor(hero,true):'';
 if(hero)bindImages();
}
const boundImages=new WeakSet();
function bindImages(){
 document.querySelectorAll('.square-product-image').forEach(image=>{
  if(boundImages.has(image))return;boundImages.add(image);
  if(image.complete){if(image.naturalWidth>0)image.parentElement.classList.add('has-square-image');else image.dataset.failed='true';}
  image.addEventListener('load',()=>{delete image.dataset.failed;image.parentElement?.classList.add('has-square-image');});
  image.addEventListener('error',()=>{image.dataset.failed='true';image.parentElement?.classList.remove('has-square-image');});
 });
}
function retryFailedImages(){
 document.querySelectorAll('.square-product-image').forEach(image=>{
  if(image.dataset.failed==='true'){delete image.dataset.failed;image.src=image.getAttribute('src');}
 });
}
function recoverSession(error){
 if(error.code==='AUTH_REQUIRED'&&typeof window!=='undefined')window.location.assign('/login?return_to=%2Fexplorer%2F');
}
function updateDataNotice(){
 if(!catalogMeta)return;
 const checked=catalogMeta.inventoryObservedAt||catalogMeta.observedAt;
 const age=checked?Date.now()-Date.parse(checked):Infinity;
 const ttl=Number(catalogMeta.inventoryTtlSeconds||300)*1000;
 const date=checked?new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(checked)):'unconfirmed';
 $('#data-notice').textContent=(age>=ttl||catalogLoadError)?'Availability needs confirming. Our team can check for you.':catalogMeta.syncEnabled?'Availability updates automatically at all four stores.':'Availability checked '+date+' Pacific. Please confirm with our team.';
 $('.header-preview').textContent='STAFF PREVIEW · SQUARE CATALOG';
 $('#footer-source').textContent=catalogMeta.syncEnabled?'Availability updates automatically':'Availability checked '+date+' Pacific';
 $('#footer-classification').textContent='Your Erupted team can help you find your next favorite.';
}
function profileIsReviewed(product){return ['existing_verified','established'].includes(product.classificationConfidence);}
function displayName(product){return String(product.name||'').replace(/^\*+\s*/,'').replace(/ · \*+/g,' · ');}
function profileDescription(product){
 if(profileIsReviewed(product))return product.description||'';
 if(!product.notes.length)return 'Our team can help you explore this flavor while its flavor guide is being reviewed.';
 return product.notes.map(nameForNote).join(' · ')+'. Notes suggested by the product name; our team can help confirm the profile.';
}
function expiryTransition(data){
 let count=0;
 for(const product of data.products)for(const variation of product.variations)for(const record of Object.values(variation.availability||{}))
  if(['available','out_of_stock'].includes(record.state)&&record.validUntil&&Date.parse(record.validUntil)<=Date.now())count++;
 return count;
}
function scheduleExpiry(data){
 clearTimeout(expiryTimer);
 const dates=data.products.flatMap(product=>product.variations.flatMap(variation=>Object.values(variation.availability||{}).filter(record=>['available','out_of_stock'].includes(record.state)).map(record=>Date.parse(record.validUntil)))).filter(value=>Number.isFinite(value)&&value>Date.now());
 if(!dates.length)return;
 expiryTimer=setTimeout(()=>{const restore=preserveFocusedElement();currentExpiryState=expiryTransition(data);renderCards();if($('#product-dialog').open)renderDetail();restore();scheduleExpiry(data);},Math.max(1,Math.min(...dates)-Date.now()+1));
}
function sensoryText(product,key){
 const value=product.notes.length&&Number.isFinite(product.sensory[key])?product.sensory[key]:null;
 return value===null?'Awaiting review':profileIsReviewed(product)?intensity(value):'May be '+intensity(value).toLowerCase();
}
function startBrowsing(){
 document.body.classList.add('is-browsing','hero-reduced');
 $('#restore-intro').hidden=false;
}
function normalizeAvailability(value){if(typeof value==='string')return {state:value};return value||{state:'unknown'}}
function availabilityText(value){const item=normalizeAvailability(value);return labels[item.state]||labels.unknown}
function availabilityMarkup(value){const item=normalizeAvailability(value);return '<span class="availability-dot '+escapeHtml(item.state)+'" aria-hidden="true"></span>'+escapeHtml(availabilityText(item))}
function topSensory(product){return Object.entries(product.sensory||{}).filter(([key,val])=>val>=3&&sensoryLabels[key]).sort((a,b)=>b[1]-a[1]).slice(0,2).map(([key])=>sensoryLabels[key])}
function renderCategories(){
 $('#categories').innerHTML=CATEGORIES.map(category=>'<button class="category-button" data-category="'+escapeHtml(category.id)+'" aria-pressed="'+state.categories.includes(category.id)+'">'+categoryArt(category.id)+'<span>'+escapeHtml(category.label)+'</span></button>').join('');
}
function renderNotes(){
 const selected=new Set(state.notes);
 const visible=showAllNotes?[...catalogNotes]:catalogNotes.filter(note=>noteDefaults.includes(note.id)||selected.has(note.id));
 for(const id of selected)if(!visible.some(note=>note.id===id))visible.push(noteMap.get(id)||{id,label:id.replace(/-/g," ").replace(/\b[a-z]/g,letter=>letter.toUpperCase())});
 $('#notes').innerHTML=visible.map(note=>'<button class="note-chip" data-note="'+escapeHtml(note.id)+'" aria-pressed="'+selected.has(note.id)+'">'+(selected.has(note.id)?'<span class="chip-icon" aria-hidden="true">✓</span>':'')+escapeHtml(note.label)+'</button>').join('');
 $('#all-notes').innerHTML=showAllNotes?'Fewer notes <span>−</span>':'All '+catalogNotes.length+' notes <span>+</span>';
 $('#all-notes').setAttribute('aria-label',showAllNotes?'Show fewer flavor notes':'Show all '+catalogNotes.length+' flavor notes');
 $('#selected-note-count').textContent=selected.size?selected.size+' selected':'Mix your favorites';
 $('#shortcut-note-count').textContent=selected.size?selected.size+' selected':'Choose a few';
 $('#flavor-notes').classList.toggle('has-selected-notes',selected.size>0);
 $('#flavor-notes').classList.toggle('all-notes-visible',showAllNotes);
 $('#all-notes').setAttribute('aria-expanded',String(showAllNotes));
}
function renderStrengths(){
 const strengths=[...new Set(catalogProducts.filter(p=>state.type==='all'||p.type===state.type).flatMap(p=>p.strengths))].sort((a,b)=>a-b);
 $('#strengths').innerHTML=[null,...strengths].map(strength=>'<button class="strength-chip" data-strength="'+(strength===null?'all':strength)+'" aria-pressed="'+(strength===state.strength)+'">'+(strength===null?'Any':mg(strength))+'</button>').join('');
}
function renderBrands(){
 const needle=brandKey($('#brand-search').value);
 const byKey=new Map(catalogProducts.flatMap(brandNamesFor).map(brand=>[brandKey(brand),brand]));
 const brands=[...byKey.values()].sort((a,b)=>a.localeCompare(b));
 const visible=brands.filter(brand=>brandKey(brand).includes(needle)||brand===state.brand);
 $('#brands').innerHTML=['',...visible].map(brand=>'<button class="strength-chip" data-brand="'+escapeHtml(brand)+'" aria-pressed="'+(state.brand===brand)+'">'+escapeHtml(brand||'Any brand')+'</button>').join('');
 $('#brand-status').textContent=visible.length?'':'No brands found. Try another name.';
 $('#sensory-filters').innerHTML=Object.entries(sensoryLabels).map(([axis,label])=>'<button class="note-chip" data-sensory="'+axis+'" aria-pressed="'+state.sensory.includes(axis)+'">'+label+'</button>').join('');
}
function renderSummary(){
 const parts=[];
 if(state.brand)parts.push({type:'brand',label:state.brand});
 state.sensory.forEach(axis=>parts.push({type:'sensory',value:axis,label:sensoryLabels[axis]}));
 if(state.type!=='all')parts.push({type:'type',value:state.type,label:state.type==='freebase'?'Freebase':state.type==='salt'?'Nicotine salts':'Disposables'});
 if(state.strength!==null)parts.push({type:'strength',label:mg(state.strength)});
 state.categories.forEach(id=>parts.push({type:'category',value:id,label:CATEGORIES.find(c=>c.id===id)?.label||id}));
 state.notes.forEach(id=>parts.push({type:'note',value:id,label:nameForNote(id)}));
 if(state.query.trim())parts.push({type:'query',label:'“'+state.query.trim()+'”'});
 const summary=$('#active-summary');summary.hidden=!parts.length;
 summary.innerHTML=parts.map(part=>'<button class="active-chip" data-remove="'+part.type+'" data-value="'+escapeHtml(part.value||'')+'" aria-label="Remove '+escapeHtml(part.label)+' filter">'+escapeHtml(part.label)+' <span aria-hidden="true">×</span></button>').join('');
 $('#reset').hidden=!parts.length;
}
function renderCards(){
 if(!catalogMeta)return;
 currentResults=discover(catalogProducts,{...state,noteDefinitions:catalogNotes});
 if(!state.notes.length&&!state.query.trim()){
  const featured=['raspberry-ripple','mango-sunrise','strawberry-silk','mint-glacier'];
  const order=product=>featured.includes(product.id)?featured.indexOf(product.id):featured.length+catalogProducts.indexOf(product);
  currentResults.sort((a,b)=>Number(a.availability.state==='out_of_stock')-Number(b.availability.state==='out_of_stock')||order(a.product)-order(b.product));
 }
 $('#result-count').textContent=currentResults.length+' '+(currentResults.length===1?'match':'matches');
 const hasNotes=state.notes.length>0;
 $('#mode-description').hidden=!hasNotes;
 $('#results-eyebrow').textContent=hasNotes?(state.mode==='exact'?'JUST YOUR FLAVOR':'SELECTED FOR YOUR PALATE'):'YOUR NEXT DISCOVERY';
 $('#results-title').textContent=hasNotes?(state.mode==='exact'?'Right on the notes.':'A little familiar. A little unexpected.'):'Good taste starts here.';
 $('#mode-description').textContent=hasNotes?(state.mode==='exact'?'Only products containing every selected flavor note.':'Out-of-stock options appear last. Exact matches lead the other results, followed by related notes and sensory profiles.'):'Explore the collection, or choose a flavor note to find your match.';
 $('#product-grid').innerHTML=currentResults.slice(0,visibleLimit).map(result=>{
  const {product,match}=result,availability=result.availability||availabilityFor(product,state.storeId,state.strength);
  const strengths=result.strengths||[...new Set((result.eligibleVariations||product.variations).map(v=>v.strengthMg))];
  const badge=match.kind==='browse'?'Discover':match.label;
  return '<article class="product-card" style="--card-accent:'+escapeHtml(product.accent)+'"><button type="button" data-product="'+escapeHtml(product.id)+'" aria-label="Explore '+escapeHtml(product.brand+' '+displayName(product))+'"><div class="card-visual"><span class="card-badge '+escapeHtml(match.kind)+'">'+escapeHtml(badge)+'</span><span class="card-format">'+escapeHtml(product.type==='salt'?'Nicotine salt':product.type==='freebase'?'Freebase':'Disposable')+'</span>'+visualFor(product)+'</div><div class="card-body"><span class="card-brand">'+escapeHtml(product.brand)+'</span><h3>'+escapeHtml(displayName(product))+'</h3><p class="card-notes">'+(product.notes.length?product.notes.slice(0,3).map(nameForNote).map(escapeHtml).join(' · '):'Flavor guide coming soon')+'</p><div class="sensory-tags">'+topSensory(product).map(name=>'<span>'+escapeHtml(profileIsReviewed(product)?name:'May feel '+name.toLowerCase())+'</span>').join('')+'</div>'+(match.shortReason&&!['exact','browse'].includes(match.kind)?'<p class="card-match-reason">'+escapeHtml(match.shortReason)+'</p>':'')+'<p class="card-product-format">'+escapeHtml(product.formatLabel)+(product.sizeLabel?' · '+escapeHtml(product.sizeLabel):'')+(product.seriesName?' · '+escapeHtml(product.seriesName):'')+'</p><div class="card-bottom"><span class="card-strengths">'+(strengths.length?strengths.map(s=>Number(s)).join(' / ')+' mg':'Strength unconfirmed')+'</span><span class="card-availability">'+availabilityMarkup(availability)+'</span></div></div></button></article>';
 }).join('');
 $('#empty-state').hidden=currentResults.length>0;
 $('#empty-message').textContent='No '+(state.type==='all'?'products':state.type==='freebase'?'freebase products':state.type==='salt'?'nicotine salts':'disposables')+(state.strength===null?'':' at '+mg(state.strength))+' match these preferences. Adjust your flavor notes or clear a filter.';
 $('#more-products').hidden=currentResults.length<=visibleLimit;
 $('#showing-count').textContent=currentResults.length>visibleLimit?'Showing '+Math.min(visibleLimit,currentResults.length)+' of '+currentResults.length+' matches':'';
 bindImages();updateDataNotice();renderSummary();
}
function render(){
 visibleLimit=24;
 const active=document.activeElement;
 const focusKey=['category','note','strength','type','mode','brand','sensory'].find(key=>active?.dataset?.[key]!==undefined);
 const focusValue=focusKey?active.dataset[focusKey]:null;
 renderCategories();renderNotes();renderStrengths();renderBrands();renderCards();document.querySelectorAll('[data-type]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.type===state.type)));document.querySelectorAll('[data-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.mode===state.mode)));
 if(focusKey)document.querySelector('[data-'+focusKey+'="'+CSS.escape(focusValue)+'"]')?.focus({preventScroll:true});
}
function reset(){
 Object.assign(state,{type:'all',strength:null,brand:'',sensory:[],notes:[],categories:[],query:'',mode:'explore'});
 showAllNotes=false;
 document.body.classList.remove('is-browsing','hero-reduced');
 $('#restore-intro').hidden=true;
 $('#search').value='';$('#brand-search').value='';
 render();
}
function toggleIn(array,value){return array.includes(value)?array.filter(item=>item!==value):[...array,value]}
function openDialog(dialog){dialog.showModal();document.body.classList.add('modal-open')}
function closeDialog(dialog){dialog.close()}
function openProduct(id,source=null){
 detailProduct=catalogProducts.find(product=>product.id===id);if(!detailProduct)return;
 startBrowsing();
 detailStrength=state.strength;
 detailSource=source||document.activeElement;
 renderDetail();openDialog($('#product-dialog'));$('#detail-close').focus();
}
function renderDetail(){
 const p=detailProduct;
 if(!p)return;
 const currentMatch=currentResults.find(result=>result.product.id===p.id)?.match;
 const stores=[...catalogStores].sort((a,b)=>(a.id===state.storeId?-1:b.id===state.storeId?1:0));
 const similar=discover(catalogProducts,{...state,notes:p.notes,query:'',mode:'explore',noteDefinitions:catalogNotes,anchorProductId:p.id}).filter(result=>result.product.id!==p.id).slice(0,2);
 $('#detail-content').innerHTML='<div class="detail-layout"><div class="detail-visual" style="--card-accent:'+escapeHtml(p.accent)+'">'+visualFor(p)+'<p class="sample-caption">'+productImageCaption(p)+'</p></div><div class="detail-copy"><p class="eyebrow">'+escapeHtml(p.brand)+'</p><h2 id="detail-title">'+escapeHtml(displayName(p))+'</h2><p class="detail-format">'+escapeHtml(p.formatLabel)+(p.sizeLabel?' · '+escapeHtml(p.sizeLabel):'')+(p.seriesName?' · '+escapeHtml(p.seriesName):'')+'</p><p class="detail-description">'+escapeHtml(profileDescription(p))+'</p><div class="detail-notes">'+p.notes.map(note=>'<span>'+escapeHtml(nameForNote(note))+'</span>').join('')+'</div><h3>The flavor profile <small class="provisional-label">'+(profileIsReviewed(p)?'Team-reviewed flavor guide':p.notes.length?'Suggested guide · our team can confirm':'Flavor guide coming soon')+'</small></h3><div class="profile-grid">'+Object.entries(sensoryLabels).map(([key,label])=>'<div class="profile-row"><span>'+escapeHtml(label==='Sweet'?'Sweetness':label==='Tart'?'Tartness':label==='Creamy'?'Creaminess':label==='Rich'?'Richness':label==='Bright'?'Brightness':label==='Crisp'?'Crispness':label)+'</span><span class="profile-level"><span class="profile-meter" role="meter" aria-valuemin="0" aria-valuemax="5" aria-valuenow="'+(p.notes.length&&Number.isFinite(p.sensory[key])?p.sensory[key]:0)+'" aria-valuetext="'+sensoryText(p,key)+'">'+Array.from({length:5},(_,i)=>'<i class="'+(i<(p.notes.length&&Number.isFinite(p.sensory[key])?p.sensory[key]:0)?'filled':'')+'" aria-hidden="true"></i>').join('')+'</span><small class="profile-intensity">'+sensoryText(p,key)+'</small></span></div>').join('')+'</div><h3>Nicotine strength</h3><div class="detail-strengths" role="group" aria-label="Strength for availability">'+[null,...p.strengths].map(strength=>'<button data-detail-strength="'+(strength===null?'all':strength)+'" aria-pressed="'+(strength===detailStrength)+'">'+(strength===null?'All strengths':mg(strength))+'</button>').join('')+'</div><h3>Find it near you</h3><div class="store-availability-list">'+stores.map(store=>{
 const availability=normalizeAvailability(availabilityFor(p,store.id,detailStrength));
 return '<div class="store-row '+(store.id===state.storeId?'assigned':'')+'"><span class="store-name">'+escapeHtml(store.name)+(store.id===state.storeId?'<span>YOUR STORE</span>':'')+'</span><span class="store-state '+escapeHtml(availability.state)+'">'+availabilityMarkup(availability)+'</span></div>';
 }).join('')+'</div><p class="availability-explanation">'+(p.inventorySource==='square'?'Square inventory':'Sample availability')+(detailStrength===null?' across this product’s strengths':(' for '+mg(detailStrength)))+'. “Check with staff” means stale or inconsistent information. Unknown availability is never treated as out of stock.</p>'+(currentMatch&&currentMatch.kind!=='browse'?'<div class="detail-match">'+escapeHtml(currentMatch.label)+'<small>'+escapeHtml(currentMatch.reason)+'</small></div>':'')+(similar.length?'<div class="detail-related"><h3>More along these lines</h3>'+similar.map(result=>'<button data-related="'+escapeHtml(result.product.id)+'">'+escapeHtml(displayName(result.product))+'<span>'+escapeHtml(result.product.brand)+' · '+escapeHtml(result.match.label)+'</span></button>').join('')+'</div>':'')+'</div></div>';
 bindImages();
}
$('#hero-art').innerHTML='';
$('#brand-search').addEventListener('input',renderBrands);
$('#brands').addEventListener('click',event=>{const button=event.target.closest('[data-brand]');if(button){startBrowsing();state.brand=button.dataset.brand;render()}});
$('#sensory-filters').addEventListener('click',event=>{const button=event.target.closest('[data-sensory]');if(button){startBrowsing();state.sensory=toggleIn(state.sensory,button.dataset.sensory);render()}});
$('#categories').addEventListener('click',event=>{const button=event.target.closest('[data-category]');if(button){startBrowsing();state.categories=toggleIn(state.categories,button.dataset.category);render()}});
$('#notes').addEventListener('click',event=>{const button=event.target.closest('[data-note]');if(button){startBrowsing();state.notes=toggleIn(state.notes,button.dataset.note);render()}});
$('#all-notes').addEventListener('click',()=>{startBrowsing();showAllNotes=!showAllNotes;renderNotes()});
$('#types').addEventListener('click',event=>{const button=event.target.closest('[data-type]');if(button){startBrowsing();state.type=button.dataset.type;render()}});
$('#strengths').addEventListener('click',event=>{const button=event.target.closest('[data-strength]');if(button){startBrowsing();state.strength=button.dataset.strength==='all'?null:Number(button.dataset.strength);render()}});
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{startBrowsing();state.mode=button.dataset.mode;render()}));
$('#search').addEventListener('input',event=>{startBrowsing();state.query=event.target.value;visibleLimit=24;renderCards()});
$('#restore-intro').addEventListener('click',()=>{document.body.classList.remove('hero-reduced');$('#restore-intro').hidden=true});
$('#jump-notes').addEventListener('click',()=>{startBrowsing();$('#flavor-notes').scrollIntoView({behavior:'smooth',block:'start'});$('#notes button')?.focus({preventScroll:true})});
$('#reset').addEventListener('click',reset);$('#empty-reset').addEventListener('click',reset);
$('#active-summary').addEventListener('click',event=>{const button=event.target.closest('[data-remove]');if(!button)return;switch(button.dataset.remove){case'brand':state.brand='';break;case'sensory':state.sensory=state.sensory.filter(axis=>axis!==button.dataset.value);break;case'type':state.type='all';break;case'strength':state.strength=null;break;case'note':state.notes=state.notes.filter(id=>id!==button.dataset.value);break;case'category':state.categories=state.categories.filter(id=>id!==button.dataset.value);break;case'query':state.query='';$('#search').value='';break}render()});
$('#product-grid').addEventListener('click',event=>{const button=event.target.closest('[data-product]');if(button)openProduct(button.dataset.product,button)});
$('#detail-close').addEventListener('click',()=>closeDialog($('#product-dialog')));
$('#product-dialog').addEventListener('click',event=>{if(event.target===$('#product-dialog')){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeDialog(event.target)}const strength=event.target.closest('[data-detail-strength]');if(strength){detailStrength=strength.dataset.detailStrength==='all'?null:Number(strength.dataset.detailStrength);renderDetail();$('#detail-content [data-detail-strength="'+strength.dataset.detailStrength+'"]')?.focus()}const related=event.target.closest('[data-related]');if(related){detailProduct=catalogProducts.find(p=>p.id===related.dataset.related);detailStrength=state.strength;renderDetail();$('#product-dialog').scrollTop=0;$('#detail-close').focus()}});
$('#product-dialog').addEventListener('close',()=>{document.body.classList.remove('modal-open');if(detailSource?.isConnected)detailSource.focus()});
$('#location-button').addEventListener('click',()=>{if(catalogMeta?.locationSelectionAllowed===false)return;renderLocations();openDialog($('#location-dialog'))});
$('#location-close').addEventListener('click',()=>closeDialog($('#location-dialog')));
function renderLocations(){$('#location-list').innerHTML=catalogStores.map(store=>'<button class="location-option '+(store.id===state.storeId?'selected':'')+'" data-store="'+escapeHtml(store.id)+'" aria-pressed="'+(store.id===state.storeId)+'"><span>'+escapeHtml(store.name)+'</span>'+(store.id===state.storeId?'<span aria-hidden="true">✓</span>':'')+'</button>').join('')}
$('#location-list').addEventListener('click',event=>{const button=event.target.closest('[data-store]');if(button){state.storeId=button.dataset.store;$('#store-label').textContent=catalogStores.find(s=>s.id===state.storeId).name;closeDialog($('#location-dialog'));renderCards()}});
$('#location-dialog').addEventListener('close',()=>{document.body.classList.remove('modal-open');$('#location-button').focus()});

function preserveFocusedElement(){
 const active=document.activeElement;
 const key=['product','detailStrength','related','note','category','strength','type','mode','brand','sensory'].find(name=>active?.dataset?.[name]!==undefined);
 if(!key)return ()=>{};
 const attr=key.replace(/[A-Z]/g,char=>'-'+char.toLowerCase());
 const value=active.dataset[key];
 return ()=>document.querySelector('[data-'+attr+'="'+CSS.escape(value)+'"]')?.focus({preventScroll:true});
}
function setLoadingControls(disabled){
 document.querySelectorAll('[data-type],[data-mode],#search,#all-notes,#jump-notes,#location-button').forEach(control=>control.disabled=disabled);
}
$('#more-products').addEventListener('click',()=>{visibleLimit+=24;renderCards()});
async function refreshCatalog(){
 if(catalogFetchInFlight)return;
 if(!catalogMeta){await initialize();return;}
 catalogFetchInFlight=true;
 try{
  const data=await loadCatalog();
  const expired=expiryTransition(data);
  catalogLoadError='';
  if(data===currentPayload&&expired===currentExpiryState){updateDataNotice();retryFailedImages();return;}
  currentPayload=data;currentExpiryState=expired;scheduleExpiry(data);
  const restore=preserveFocusedElement();
  catalogProducts=data.products;catalogStores=data.stores;catalogMeta=data.meta;catalogLoadError='';
  renderHero();
  applyNoteDefinitions(data);renderNotes();
  if(detailProduct){
   const current=catalogProducts.find(product=>product.id===detailProduct.id);
   if(current)detailProduct=current;else closeDialog($('#product-dialog'));
  }
  renderCards();if($('#product-dialog').open)renderDetail();restore();
 }catch(error){recoverSession(error);catalogLoadError=error.message;const restore=preserveFocusedElement();renderCards();if($('#product-dialog').open)renderDetail();restore();}
 finally{catalogFetchInFlight=false;}
}
async function initialize(){
 if(catalogFetchInFlight)return;
 catalogFetchInFlight=true;
 setLoadingControls(true);
 $('#data-notice').textContent='Loading the Square catalog…';
 $('#product-grid').setAttribute('aria-busy','true');
 try{
  const data=await loadCatalog();
  catalogProducts=data.products;
  catalogStores=data.stores;
  catalogMeta=data.meta;
  applyNoteDefinitions(data);
  currentPayload=data;currentExpiryState=expiryTransition(data);scheduleExpiry(data);
  setLoadingControls(false);
  state.storeId=catalogStores.some(store=>store.id===catalogMeta.assignedStoreId)?catalogMeta.assignedStoreId:catalogStores.find(store=>store.id==='hwy99')?.id||catalogStores[0].id;
  $('#store-label').textContent=catalogStores.find(store=>store.id===state.storeId).name;
  if(catalogMeta.locationSelectionAllowed===false){$('#location-button').disabled=true;$('.location-note').textContent='This tablet is assigned to its provisioned store.';}
  renderHero();
  catalogLoadError='';
  $('#empty-reset').hidden=false;
  $('#empty-state h3').textContent='A different blend awaits.';
  render();
 }catch(error){
  recoverSession(error);
  $('#data-notice').textContent=error.message;
  $('#result-count').textContent='Catalog unavailable';
  $('#empty-state').hidden=false;
  $('#empty-state h3').textContent='We’ll help you find your flavor.';
  $('#empty-message').textContent='Please check with staff. No sample products or stock are substituted for missing Square data.';
  $('#empty-reset').hidden=true;
 }finally{catalogFetchInFlight=false;$('#product-grid').setAttribute('aria-busy','false');}
}
initialize();
setInterval(refreshCatalog,15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&catalogMeta){const restore=preserveFocusedElement();renderCards();if($('#product-dialog').open)renderDetail();restore();refreshCatalog();}});

if(typeof window!=='undefined'){window.addEventListener('pageshow',()=>{retryFailedImages();refreshCatalog();});window.addEventListener('online',()=>{retryFailedImages();refreshCatalog();});}
