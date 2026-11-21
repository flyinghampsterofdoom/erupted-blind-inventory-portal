import{esc,person,when}from'./shared-admin.mjs';
const API='/v2/compatibility-explorer/api';
const $=selector=>document.querySelector(selector);
const state={results:[],resultQuery:'',resultMode:'name',matchType:'none',active:-1,selected:null,identifierId:null,trail:[],scannedIdentifier:null,suggestion:null,queue:[],review:null};
const draftCache=new Map();
state.stockValidUntil=0;
let boot,searchNumber=0,detailNumber=0,stockNumber=0,queueNumber=0,targetNumber=0,catalogNumber=0,searchTimer,targetTimer,catalogTimer,stockExpiryTimer,stockExpiryCheck=null,keyTimes=[];
const kindLabel=kind=>({device:'Device',coil:'Coil',pod:'Pod',family:'Product family',tank:'Tank'}[kind]||'Product');
const suggestionLabel=kind=>({addition:'New product',correction:'Product correction',identifier:'Missing SKU or barcode',relationship:'Compatibility relationship',manufacturer_alias:'Manufacturer alias',catalog_association:'Verified catalog association'}[kind]||kind);
const numberText=value=>value===null||value===undefined||value===''?'':String(value);
const variantLabel=product=>[product.resistanceOhm!=null?numberText(product.resistanceOhm)+'Ω':'',product.capacityMl!=null?numberText(product.capacityMl)+' ml':'',product.configuration].filter(Boolean).join(' · ');
const productLabel=product=>[product.manufacturer,product.name,variantLabel(product)].filter(Boolean).join(' · ');
const keyOf=(id,identifierId)=>String(id)+'|'+String(identifierId??'');
function message(value,error=false){const element=$('#ce-message');element.textContent=value||'';element.hidden=!value;element.className=error?'fi-notice fi-notice--error':'fi-notice';if(error)element.focus({preventScroll:true})}
function report(error){message(error.message||String(error),true)}
export function identifierLike(value){return /^\d{6,18}$/.test(value.trim())}
export function scannerBurst(times,now){return times.length>=5&&now-times.at(-1)<140&&(times.at(-1)-times[0])/(times.length-1)<55}
function externalURL(value){try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:null}catch{return null}}
function localURL(value){if(typeof value!=='string'||!value.trim())return null;try{const url=new URL(value,location.origin);return url.origin===location.origin?url.pathname+url.search+url.hash:null}catch{return null}}
function requestReturnURL(value){
 const local=localURL(value);if(!local)return null;
 const url=new URL(local,location.origin);url.searchParams.set('q',$('#ce-query').value);return url.pathname+url.search+url.hash;
}
async function restoreRequestContext(){
 const params=new URLSearchParams(location.search||''),query=params.get('q'),productId=params.get('productId'),identifierId=params.get('identifierId');
 if(params.get('request')==='submitted')message('Customer request submitted.');
 if(query!==null)$('#ce-query').value=query;
 if(query?.trim()){
  const expected=searchNumber+1;await search({mode:identifierId?'identifier':'name',openExact:false});
  if(searchNumber!==expected||$('#ce-query').value!==query)return;
 }
 if(productId)await openProduct(productId,{identifierId:identifierId||null});
}
async function request(path,{method='GET',body}={}){
 const headers={Accept:'application/json'};
 if(method!=='GET'){
  headers['Content-Type']='application/json';
  const cookie=document.cookie.split(';').map(part=>part.trim()).find(part=>part.startsWith('csrf_token='));
  if(cookie)headers['X-CSRF-Token']=decodeURIComponent(cookie.slice(11));
 }
 let response;
 try{response=await fetch(API+path,{method,headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)})}
 catch{throw new Error('Unable to connect. Your current search and edits are preserved; try again.')}
 const data=await response.json().catch(()=>({}));
 if(!response.ok){
  let detail=typeof data.detail==='string'?data.detail:Array.isArray(data.detail)?data.detail.map(row=>row.msg||'Invalid value').join('; '):data.message;
  if(response.status===401)detail='Your session ended. Sign in again in another tab, then retry. Your draft is preserved.';
  if(response.status===409)detail=(detail||'This record changed while you were working.')+' Review the current record before trying again. Your draft is preserved.';
  const error=new Error(detail||'The action could not be completed. Your draft is preserved.');error.status=response.status;throw error;
 }
 return data;
}
const empty=text=>'<p class="fi-empty">'+esc(text)+'</p>';
const editableIdentifier=identifier=>typeof identifier.id==='string'&&!identifier.id.startsWith('square:');
function cancelStockExpiry(){clearTimeout(stockExpiryTimer);stockExpiryCheck=null;state.stockValidUntil=0}
function invalidateStock(){stockNumber++;cancelStockExpiry();const area=$('#ce-stock');if(area)area.innerHTML=empty('Check stock again for current quantities.')}
function scheduleStockExpiry(data,product,identifierId,sequence,button){
 const now=Date.now(),provided=Date.parse(data.validUntil||''),observed=Date.parse(data.observedAt||'');
 const until=Math.min(now+60000,Number.isFinite(provided)?provided:Infinity,Number.isFinite(observed)?observed+60000:Infinity);
 state.stockValidUntil=until;
 const expire=()=>{
  if(sequence!==stockNumber||keyOf(state.selected?.id,state.identifierId)!==keyOf(product.id,identifierId)||state.stockValidUntil!==until)return;
  const remaining=until-Date.now();if(remaining>0){stockExpiryTimer=setTimeout(expire,remaining);return}
  state.stockValidUntil=0;stockExpiryCheck=null;$('#ce-stock').innerHTML=empty('Stock check expired. Check stock again for current quantities.');if(button.isConnected)button.textContent='Check stock again';
 };
 stockExpiryCheck=expire;stockExpiryTimer=setTimeout(expire,Math.max(0,until-now));if(until<=now)expire();
}
function identifierKindOptions(value){
 const options=[['alternate','Other exact identifier'],['sku','SKU'],['gtin','UPC / EAN / GTIN'],['mpn','Manufacturer part number']];
 if(['upc','ean','barcode'].includes(value))options.push([value,value==='upc'?'UPC':value==='ean'?'EAN':'Barcode']);
 return options;
}
function associationWarning(row,product){
 const prior=row.original?._catalogAssociationBefore,owner=prior?.product;if(!owner||!row.proposed?.reassign)return '';
 return '<p class="fi-warning">Reassigns this catalog variation from '+esc(typeof owner==='string'?owner:productLabel(owner))+' to '+esc(productLabel(product||row.product||{}))+'. The current association will be replaced.</p>';
}
function catalogLabel(item){return [item.itemName,item.variationName].filter(Boolean).join(' · ')||item.label||'Erupted catalog product'}
function manufacturerLabel(id,fallback=''){return (boot.manufacturers||[]).find(row=>String(row.id)===String(id))?.name||fallback||'Not specified'}
function proposalSummary(row,product,target=null){
 const proposed=row.proposed||{},original=row.original||{},lines=[];
 const add=(label,before,after)=>lines.push('<div class="fi-diff-row"><strong>'+esc(label)+'</strong><div><span class="fi-diff-label">Before</span><p>'+esc(before??'Not specified')+'</p></div><div class="fi-diff-after"><span class="fi-diff-label">Proposed</span><p>'+esc(after??'Not specified')+'</p></div></div>');
 const labels={name:'Product name',manufacturerId:'Manufacturer',manufacturerName:'Manufacturer',kind:'Product type',model:'Model',family:'Family',generation:'Generation',resistanceOhm:'Resistance',capacityMl:'Capacity',configuration:'Configuration',integratedCoil:'Coil design',aliases:'Alternate product names'};
 const privateKeys={manufacturerId:'manufacturer_id',resistanceOhm:'resistance_ohm',capacityMl:'capacity_ml',integratedCoil:'integrated_coil'};
 const display=(key,value)=>{
  if(value===null||value===undefined||value==='')return 'Not specified';
  if(key==='manufacturerId')return manufacturerLabel(value);
  if(key==='kind')return kindLabel(value);
  if(key==='integratedCoil')return typeof value==='boolean'?(value?'Integrated coil':'Replaceable coil'):'Not specified';
  if(key==='resistanceOhm')return String(value)+'Ω';if(key==='capacityMl')return String(value)+' ml';
  return Array.isArray(value)?value.join(', '):String(value);
 };
 const identity=value=>value?[String(value.kind||'identifier').toUpperCase(),value.value,value.packageConfiguration??value.package_configuration].filter(Boolean).join(' · '):'No identifier';
 if(row.kind==='correction'){
  for(const [key,value] of Object.entries(proposed.patch||{})){
   if(key==='evidence')continue;
   add(labels[key]||'Product information',display(key,original[key]??original[privateKeys[key]]??product?.[key]),display(key,value));
  }
 }else if(row.kind==='addition'){
  for(const key of ['name','manufacturerId','manufacturerName','kind','model','family','generation','resistanceOhm','capacityMl','configuration','integratedCoil','aliases'])if(proposed[key]!==undefined&&proposed[key]!==null&&proposed[key]!=='')add(labels[key],'New product',display(key,proposed[key]));
  for(const identifier of proposed.identifiers||[])add('Identifier','None',identity(identifier));
 }else if(row.kind==='identifier'){
  add(proposed.remove?'Retire identifier':'Identifier',identity(original._identifierBefore),proposed.remove?'Removed from compatibility lookup':identity(proposed));
 }else if(row.kind==='relationship'){
  const prior=original._relationshipBefore||{},relationLabels={accepts_pod:'Accepts pod',accepts_coil:'Accepts coil',includes_tank:'Includes tank'};
  const other=target?productLabel(target):'Selected compatible product';
  const subject=product?productLabel(product):'This product';
  const direction=proposed.direction||(prior.from_id===product?.id?'outgoing':'incoming');
  const relation=relationLabels[proposed.relationship||prior.kind]||'Compatibility';
  add('Compatible products',prior.id?'Existing verified relationship':'No relationship',proposed.remove?'Remove relationship':direction==='incoming'?other+' → '+subject:subject+' → '+other);
  add('Relationship',prior.id?(relationLabels[prior.kind]||'Verified compatibility'):'None',proposed.remove?'Removed':relation);
  if(proposed.conditions||prior.conditions)add('Conditions',prior.conditions||'None',proposed.remove?'Relationship removed':proposed.conditions||'None');
 }else if(row.kind==='catalog_association'){
  const prior=original._catalogAssociationBefore,known=original._catalogVariationBefore||{};
  const owner=prior?.product;
  add('Erupted catalog product',prior?catalogLabel(known)||prior.package_configuration:'No association',catalogLabel(known));
  add('Catalog association',owner?(typeof owner==='string'?owner:productLabel(owner)):'No current owner',proposed.remove?'Remove association':product?productLabel(product):'Selected compatibility product');
  if(proposed.packageConfiguration||prior?.package_configuration)add('Retail package',prior?.package_configuration||'Not specified',proposed.remove?'Association removed':proposed.packageConfiguration||'Catalog package');
 }else if(row.kind==='manufacturer_alias'){
  add('Manufacturer',manufacturerLabel(proposed.manufacturerId),manufacturerLabel(proposed.manufacturerId));
  add('Manufacturer alias','Not listed',proposed.alias);
 }
 const sources=proposed.evidence||[];
 const sourceMarkup=sources.map(source=>{const url=externalURL(source.url);return url?'<li><a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(source.title||source.publisher||'Verification source')+'</a>'+(source.claim?'<p>'+esc(source.claim)+'</p>':'')+'</li>':''}).filter(Boolean).join('');
 return '<section class="ce-context"><h3>Proposed change</h3><div class="fi-diff">'+lines.join('')+'</div>'+(sourceMarkup?'<div class="ce-evidence"><strong>Supporting evidence</strong><ul>'+sourceMarkup+'</ul></div>':'<p class="fi-hint">No supporting source supplied.</p>')+'</section>';
}
function catalogAssociationFields(raw,context){
 const matches=context.product?.catalogAssociation?.allMatches||context.product?.catalogAssociation?.matches||[],locked=Boolean(context.lockCatalog);
 const selected=matches.find(match=>String(match.variationId)===String(raw.squareVariationId));
 const label=raw.catalogLabel||(selected?catalogLabel(selected):context.catalogIdentity?catalogLabel(context.catalogIdentity):'');
 return selectField('Catalog association action','catalogAction',raw.catalogAction||'add',[['add','Add verified association'],['reassign','Reassign from another product'],['remove','Remove incorrect association']])+
 '<input type="hidden" data-draft="squareVariationId" name="squareVariationId" value="'+esc(raw.squareVariationId||'')+'">'+
 '<label class="fi-field is-wide">Erupted catalog product — SKU, barcode or name<input type="search" id="ce-catalog-query" name="catalogLabel" data-draft="catalogLabel" value="'+esc(label)+'" placeholder="Search Erupted’s current catalog" '+(locked?'readonly':'')+'><div class="ce-target-results" id="ce-catalog-results"></div></label>'+
 (matches.length?'<label class="fi-field is-wide">Existing association to correct or remove<select name="catalogExisting" '+(locked?'disabled':'')+'><option value="">Choose an existing association</option>'+matches.map(match=>'<option value="'+esc(match.variationId)+'" '+(String(match.variationId)===String(raw.squareVariationId)?'selected':'')+'>'+esc(catalogLabel(match)+(match.packageConfiguration?' · '+match.packageConfiguration:'')+(match.available===false?' · Catalog unavailable':''))+'</option>').join('')+'</select></label>':'')+
 '<details class="is-wide"><summary>Advanced: catalog identifier or unavailable product</summary><label class="fi-field">Square variation ID<input data-catalog-advanced value="'+esc(raw.squareVariationId||'')+'" '+(locked?'readonly':'')+'></label></details>'+
 field('Retail package configuration','packageConfiguration',raw.packageConfiguration,{wide:true});
}
function wireCatalogSearch(form,context){
 const query=form.querySelector('#ce-catalog-query'),hidden=form.querySelector('[name="squareVariationId"]');if(!query||!hidden||context.lockCatalog)return;
 const results=form.querySelector('#ce-catalog-results'),advanced=form.querySelector('[data-catalog-advanced]'),existing=form.querySelector('[name="catalogExisting"]');
 const choose=item=>{clearTimeout(catalogTimer);catalogNumber++;hidden.value=item.id||item.variationId;query.value=catalogLabel(item);if(advanced)advanced.value=hidden.value;if(item.packageConfiguration)form.querySelector('[name="packageConfiguration"]').value=item.packageConfiguration;context.catalogIdentity=item;results.innerHTML=''};
 const lookup=async()=>{
  clearTimeout(catalogTimer);const sequence=++catalogNumber,value=query.value;
  hidden.value='';if(advanced)advanced.value='';if(existing)existing.value='';
  if(!value.trim()){results.innerHTML='';return}
  results.innerHTML=empty('Searching Erupted catalog…');
  try{
   const data=await request('/catalog/search?'+new URLSearchParams({q:value}));
   if(sequence!==catalogNumber||!query.isConnected||query.value!==value)return;
   if(!data.available){results.innerHTML=empty('Catalog temporarily unavailable. Existing associations remain available below.');return}
   const rows=data.items||[];results.innerHTML=rows.slice(0,20).map((item,index)=>'<button type="button" data-catalog-result="'+index+'">'+esc(catalogLabel(item))+'<small>'+esc([item.sku?'SKU '+item.sku:'',item.upc?'Barcode '+item.upc:''].filter(Boolean).join(' · '))+'</small></button>').join('')||empty('No catalog match. Try its exact SKU or barcode.');
   results.querySelectorAll('[data-catalog-result]').forEach(button=>button.onclick=()=>choose(rows[Number(button.dataset.catalogResult)]));
  }catch(error){if(sequence===catalogNumber)report(error)}
 };
 query.oninput=()=>{clearTimeout(catalogTimer);catalogNumber++;hidden.value='';if(advanced)advanced.value='';if(existing)existing.value='';catalogTimer=setTimeout(lookup,180)};
 query.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();lookup()}};
 if(existing)existing.onchange=()=>{const match=(context.product?.catalogAssociation?.allMatches||context.product?.catalogAssociation?.matches||[]).find(item=>String(item.variationId)===existing.value);if(match){catalogNumber++;choose(match)}};
 if(advanced)advanced.oninput=()=>{clearTimeout(catalogTimer);catalogNumber++;hidden.value=advanced.value.trim();query.value=hidden.value?'Advanced catalog identifier':'';if(existing)existing.value='';results.innerHTML=''};
}

function manufacturerOptions(selected){return '<option value="">Choose manufacturer</option>'+(boot.manufacturers||[]).map(row=>'<option value="'+esc(row.id)+'" '+(String(row.id)===String(selected)?'selected':'')+'>'+esc(row.name)+'</option>').join('')}
function evidenceMarkup(evidence=[]){
 const sources=evidence.map(source=>{const url=externalURL(source.url);return url?'<li><a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(source.title||source.publisher||'Verification source')+'</a>'+(source.publisher?'<span> · '+esc(source.publisher)+'</span>':'')+'</li>':''}).filter(Boolean);
 return sources.length?'<details class="ce-evidence"><summary>Verification sources · '+sources.length+'</summary><ul>'+sources.join('')+'</ul></details>':'<p class="ce-evidence">No source link supplied.</p>';
}
function resultMarkup(product,index){
 const match=product.match||{},identifier=product.matchedIdentifier;
 return '<button type="button" class="ce-result" role="option" aria-selected="'+String(index===state.active)+'" id="ce-option-'+index+'" data-result="'+index+'"><strong>'+esc(product.name)+'</strong><small>'+esc([product.manufacturer,variantLabel(product)].filter(Boolean).join(' · '))+'</small><span class="ce-type">'+esc(kindLabel(product.kind))+'</span>'+(match.label?'<small class="'+(state.matchType==='exact_identifier'?'ce-exact':'')+'">'+esc(match.label)+'</small>':'')+(identifier?'<small>'+esc([identifier.kind,identifier.value,identifier.packageConfiguration].filter(Boolean).join(' · '))+'</small>':'')+'</button>';
}
function renderResults(){
 $('#ce-results').innerHTML=state.results.map(resultMarkup).join('')||empty(state.resultQuery?'No matching products.':'Search a product name or scan a package to begin.');
 const input=$('#ce-query');input.setAttribute('aria-expanded',String(Boolean(state.results.length)));if(state.active>=0)input.setAttribute('aria-activedescendant','ce-option-'+state.active);else input.removeAttribute('aria-activedescendant');
}
function revealResults(){
 detailNumber++;invalidateStock();state.selected=null;state.identifierId=null;state.trail=[];
 $('#ce-detail').hidden=true;$('.ce-layout').classList.remove('has-selection');
}
function clearSearch(){
 clearTimeout(searchTimer);searchNumber++;keyTimes=[];state.results=[];state.resultQuery='';state.scannedIdentifier=null;state.active=-1;$('#ce-query').value='';revealResults();renderResults();$('#ce-search-context').textContent='Search across the compatibility database, including products outside Erupted’s catalog.';$('#ce-query').focus();
}
async function search({mode='auto',openExact=true}={}){
 const query=$('#ce-query').value;
 clearTimeout(searchTimer);const sequence=++searchNumber;keyTimes=[];
 if(!query.trim()){clearSearch();return}
 $('#ce-search-context').textContent='Searching…';
 try{
  const data=await request('/search?'+new URLSearchParams({q:query,mode}));
  if(sequence!==searchNumber||$('#ce-query').value!==query)return;
  revealResults();state.results=data.items||[];state.resultQuery=query;state.resultMode=data.mode||mode;state.matchType=data.matchType;state.active=-1;
  state.scannedIdentifier=data.matchType==='exact_identifier'||data.matchType==='ambiguous'||state.resultMode==='identifier'||mode==='identifier'?query:null;
  renderResults();
  if(data.matchType==='exact_identifier'){
   $('#ce-search-context').innerHTML='<span class="ce-exact">Exact identifier match</span> · '+esc(query)+'. The matched resistance and package are retained.';
   if(openExact&&data.exactProductId!=null){const product=state.results.find(row=>String(row.id)===String(data.exactProductId));if(product)await openProduct(product.id,{identifierId:product.matchedIdentifier?.id})}
  }else if(data.matchType==='ambiguous'){
   $('#ce-search-context').textContent='This identifier matches more than one record. Select the exact variant and package; no product has been substituted.';
  }else if(!state.results.length){
   $('#ce-search-context').innerHTML=(state.scannedIdentifier?'<span>Product not found — </span>':'<span>No matching product names — </span>')+'<button type="button" class="fi-text-button" data-suggest-unknown>Suggest Addition</button>'+(state.scannedIdentifier?'<span> · '+esc(query)+'</span> <button type="button" class="fi-text-button" data-name-fallback>Search this as a product name</button>':'');
  }else{
   $('#ce-search-context').textContent=state.results.length+' name match'+(state.results.length===1?'':'es')+' · Exact model, partial, alias and approximate results are labeled. Select the correct product; name similarity does not verify compatibility.';
  }
 }catch(error){if(sequence===searchNumber)report(error)}
}
function detailPath(id,suffix='',identifierId=state.identifierId){return '/products/'+encodeURIComponent(id)+suffix+(identifierId!=null?'?'+new URLSearchParams({identifierId:String(identifierId)}):'')}
async function openProduct(id,{identifierId=null,push=false}={}){
 clearTimeout(searchTimer);searchNumber++;
 const sequence=++detailNumber;invalidateStock();
 const previous=state.selected?{id:state.selected.id,identifierId:state.identifierId}:null;
 try{
  const product=await request(detailPath(id,'',identifierId));
  if(sequence!==detailNumber)return;
  if(push&&previous)state.trail.push(previous);
  state.selected=product;state.identifierId=identifierId;renderDetail(product);
  $('.ce-layout').classList.add('has-selection');$('#ce-detail').hidden=false;
  $('#ce-detail').scrollIntoView({behavior:'smooth',block:'start'});
 }catch(error){if(sequence===detailNumber)report(error)}
}
function backFromProduct(){
 const previous=state.trail.pop();
 if(previous){openProduct(previous.id,{identifierId:previous.identifierId});return}
 const selectedId=state.selected?.id;revealResults();renderResults();
 const index=state.results.findIndex(row=>String(row.id)===String(selectedId));
 const target=index>=0?$('#ce-option-'+index):$('#ce-query');target?.focus({preventScroll:true});
}
function renderDetail(product){
 const facts=[['Type',kindLabel(product.kind)],['Coil design',['pod','tank'].includes(product.kind)&&typeof product.integratedCoil==='boolean'?(product.integratedCoil?'Integrated coil — replace whole '+(product.kind==='pod'?'pod':'tank'):'Replaceable coil'):''],['Model',product.model],['Family',product.family],['Generation',product.generation],['Resistance',product.resistanceOhm!=null?numberText(product.resistanceOhm)+'Ω':''],['Capacity',product.capacityMl!=null?numberText(product.capacityMl)+' ml':''],['Configuration',product.configuration]].filter(([,value])=>value!==null&&value!==undefined&&value!=='');
 const relationships=product.relationships||[],carried=product.catalogAssociation?.verified===true,associationStatus=product.catalogAssociation?.status||(carried?'verified':'unmatched'),requestURL=requestReturnURL(product.customerRequestUrl);
 $('#ce-detail').innerHTML='<button type="button" class="fi-text-button ce-back" data-product-back>← '+(state.trail.length?'Back to previous product':'Back to results')+'</button><div class="ce-detail-heading"><div><h2>'+esc(product.name)+'</h2><p>'+esc(product.manufacturer)+'</p></div><span class="ce-type">'+esc(kindLabel(product.kind))+'</span></div><div class="ce-actions">'+(carried?'<button type="button" class="v2-button v2-button--primary" data-check-stock>We Carry It — Check Stock?</button>':associationStatus==='unmatched'&&requestURL?'<a class="v2-button v2-button--primary" href="'+esc(requestURL)+'">Customer Request</a>':'<span class="fi-hint">'+esc(associationStatus==='unavailable'?'Catalog temporarily unavailable':'Catalog match needs review')+'</span>')+'<button type="button" class="v2-button v2-button--secondary" data-suggest-current>Suggest Correction</button></div>'+(carried&&product.catalogAssociation.label?'<p class="fi-hint">'+esc(product.catalogAssociation.label)+'</p>':'')+'<div class="ce-stock" id="ce-stock"></div><h3>Verified compatibility</h3>'+(relationships.length?'<div class="ce-relations">'+relationships.map((relation,index)=>'<div class="ce-relation"><button type="button" class="fi-text-button" data-related="'+index+'">'+esc(productLabel(relation.target))+'</button><p>'+esc(kindLabel(relation.target.kind))+(relation.conditions?' · '+esc(relation.conditions):'')+'</p>'+((Array.isArray(relation.via)?relation.via.length:Boolean(relation.via))?'<p>Via '+esc(Array.isArray(relation.via)?relation.via.map(person).join(' → '):person(relation.via))+'</p>':'')+evidenceMarkup(relation.evidence)+(!relation.via||(Array.isArray(relation.via)&&!relation.via.length)?'<button type="button" class="fi-text-button" data-correct-relation="'+index+'">Suggest relationship correction</button>':'')+'</div>').join('')+'</div>':empty('No verified compatibility records yet. Suggest a relationship when you have supporting information.'))+'<dl class="ce-facts">'+facts.map(([label,value])=>'<div><dt>'+esc(label)+'</dt><dd>'+esc(value)+'</dd></div>').join('')+'</dl><details class="ce-identifiers"><summary>SKU, barcode and package identifiers · '+(product.identifiers||[]).length+'</summary><ul>'+(product.identifiers||[]).map(identifier=>'<li>'+esc(identifier.kind)+' · <code>'+esc(identifier.value)+'</code>'+(identifier.packageConfiguration?' · '+esc(identifier.packageConfiguration):'')+(editableIdentifier(identifier)?' <button type="button" class="fi-text-button" data-correct-identifier="'+esc(identifier.id)+'">Suggest identifier correction</button>':' <small>Catalog identifier · Read-only</small>')+'</li>').join('')+'</ul></details><div class="ce-actions"><button type="button" class="fi-text-button" data-suggest-relationship>+ Suggest a missing compatibility relationship</button></div>';
 $('#ce-detail').querySelector('[data-product-back]').onclick=backFromProduct;
 $('#ce-detail').querySelector('[data-suggest-current]').onclick=()=>openSuggestion('correction');
 $('#ce-detail').querySelector('[data-suggest-relationship]').onclick=()=>openSuggestion('relationship');
 $('#ce-detail').querySelectorAll('[data-related]').forEach(button=>button.onclick=()=>openProduct(relationships[Number(button.dataset.related)].target.id,{push:true}));
 $('#ce-detail').querySelectorAll('[data-correct-relation]').forEach(button=>button.onclick=()=>openSuggestion('relationship',relationships[Number(button.dataset.correctRelation)]));
 $('#ce-detail').querySelectorAll('[data-correct-identifier]').forEach(button=>button.onclick=()=>openSuggestion('identifier',null,(product.identifiers||[]).find(identifier=>identifier.id===button.dataset.correctIdentifier)));
 const stock=$('#ce-detail').querySelector('[data-check-stock]');if(stock)stock.onclick=()=>checkStock(stock);
}
async function checkStock(button){
 const product=state.selected,identifierId=state.identifierId,sequence=++stockNumber;if(!product)return;cancelStockExpiry();
 button.disabled=true;$('#ce-stock').textContent='Checking current stock across all four stores…';
 try{
  const data=await request(detailPath(product.id,'/stock',identifierId));
  if(sequence!==stockNumber||keyOf(state.selected?.id,state.identifierId)!==keyOf(product.id,identifierId))return;
  $('#ce-stock').innerHTML='<div class="ce-stock-grid">'+(data.stores||[]).map(store=>'<div class="ce-stock-row"><span>'+esc(store.name)+'</span>'+(store.variations?.length?store.variations.map(variation=>'<div class="ce-package-stock"><strong>'+esc(variation.label||variation.packageConfiguration||'Verified package')+'</strong>'+(variation.label&&variation.packageConfiguration&&String(variation.label).trim()!==String(variation.packageConfiguration).trim()?'<small>'+esc(variation.packageConfiguration)+'</small>':'')+'<span>'+stockLabel(variation)+'</span></div>').join(''):'<strong>'+stockLabel(store)+'</strong>')+'<small>'+esc(when(store.observedAt||data.observedAt))+'</small></div>').join('')+'</div><p class="fi-hint">Zero stock still means Erupted carries this product. Different retail packages are shown separately.</p>';
  button.textContent='Check stock again';scheduleStockExpiry(data,product,identifierId,sequence,button);
 }catch(error){if(sequence===stockNumber){$('#ce-stock').textContent='Stock is unavailable. Quantities are unknown; try again.';report(error)}}
 finally{if(button.isConnected)button.disabled=false}
}
function stockLabel(row){return row.quantity==null||row.state==='unknown'?'Unavailable':esc(String(row.quantity))+(row.state==='out_of_stock'?' · Out of stock':' available')}
function field(label,name,value='',{type='text',wide=false,placeholder=''}={}){
 return '<label class="fi-field '+(wide?'is-wide':'')+'">'+esc(label)+'<input data-draft="'+esc(name)+'" name="'+esc(name)+'" type="'+type+'" value="'+esc(value??'')+'" '+(type==='number'?'step="any" min="0"':'')+' '+(placeholder?'placeholder="'+esc(placeholder)+'"':'')+'></label>';
}
function selectField(label,name,value,options){return '<label class="fi-field">'+esc(label)+'<select data-draft="'+esc(name)+'" name="'+esc(name)+'">'+options.map(([key,text])=>'<option value="'+esc(key)+'" '+(String(value)===String(key)?'selected':'')+'>'+esc(text)+'</option>').join('')+'</select></label>'}
function readRaw(form){return Object.fromEntries([...form.querySelectorAll('[data-draft]')].map(input=>[input.name,input.value]))}
function evidenceFields(raw={}){
 return '<details class="is-wide" '+(raw.sourceURL?'open':'')+'><summary>Supporting manufacturer source</summary><div class="ce-form-grid">'+field('Source URL','sourceURL',raw.sourceURL,{type:'url',wide:true,placeholder:'https://manufacturer.example/product'})+field('Source title','sourceTitle',raw.sourceTitle)+field('Publisher','sourcePublisher',raw.sourcePublisher)+field('What does this source confirm?','sourceClaim',raw.sourceClaim,{wide:true})+'</div></details>';
}
function proposedRaw(kind,proposed={},product=null,scannedIdentifier=null){
 const value=kind==='correction'?{...(product||{}),...(proposed.patch||{})}:proposed,source=proposed.evidence?.[0]||{};
 const identifier=proposed.identifiers?.[0]||{};
 return {...value,identifierAction:proposed.remove?'remove':proposed.identifierId?'update':'add',catalogAction:proposed.remove?'remove':proposed.reassign?'reassign':'add',relationshipAction:proposed.remove?'remove':'update',manufacturerId:value.manufacturerId||product?.manufacturerId||'',manufacturerName:value.manufacturerName||'',targetProductId:proposed.targetProductId||'',identifierKind:kind==='identifier'?(proposed.kind||'alternate'):identifier.kind||'alternate',identifierValue:kind==='identifier'?(proposed.value||scannedIdentifier||''):identifier.value||scannedIdentifier||'',sourceURL:source.url||'',sourceTitle:source.title||'',sourcePublisher:source.publisher||'',sourceClaim:source.claim||''};
}
function proposedFields(kind,raw,context){
 const manufacturer='<label class="fi-field">Manufacturer<select name="manufacturerId" data-draft="manufacturerId">'+manufacturerOptions(raw.manufacturerId)+'</select></label>';
 let markup='';
 if(kind==='addition'||kind==='correction'){
  markup=manufacturer+field('Manufacturer if not listed','manufacturerName',raw.manufacturerName)+field('Product name','name',raw.name,{wide:true})+selectField('Product type','kind',raw.kind||'device',[['device','Device'],['coil','Coil'],['pod','Pod'],['tank','Tank'],['family','Product family']])+field('Model','model',raw.model)+field('Family','family',raw.family)+field('Generation / version','generation',raw.generation)+field('Resistance (Ω), if applicable','resistanceOhm',raw.resistanceOhm,{type:'number'})+field('Capacity (ml), if applicable','capacityMl',raw.capacityMl,{type:'number'})+field('Configuration / package','configuration',raw.configuration,{wide:true})+selectField('Coil design (pods / tanks)','integratedCoil',raw.integratedCoil===true?'true':raw.integratedCoil===false?'false':raw.integratedCoil??'',[['','Choose for a pod or tank'],['true','Integrated coil'],['false','Replaceable coil']]);
  if(kind==='addition')markup+=selectField('Identifier type','identifierKind',raw.identifierKind,identifierKindOptions(raw.identifierKind))+field('SKU or barcode, if known','identifierValue',raw.identifierValue)+field('Identifier package configuration','packageConfiguration',raw.packageConfiguration,{wide:true});
 }else if(kind==='identifier'){
  markup=(raw.identifierId||context.identifierId?'<input type="hidden" data-draft="identifierId" name="identifierId" value="'+esc(raw.identifierId||context.identifierId)+'">'+selectField('Identifier action','identifierAction',raw.identifierAction||'update',[['update','Correct identifier'],['remove','Remove incorrect identifier']]):'')+selectField('Identifier type','identifierKind',raw.identifierKind,identifierKindOptions(raw.identifierKind))+field('Exact identifier','identifierValue',raw.identifierValue)+field('Package configuration','packageConfiguration',raw.packageConfiguration,{wide:true});
 }else if(kind==='relationship'){
  markup=((raw.relationshipId||context.relationshipId)?'<input type="hidden" name="relationshipId" data-draft="relationshipId" value="'+esc(raw.relationshipId||context.relationshipId)+'">'+selectField('Change to this direct relationship','relationshipAction',raw.relationshipAction||'update',[['update','Correct relationship details'],['remove','Remove incorrect relationship']]):'')+'<label class="fi-field is-wide">Compatible product<input type="search" id="ce-target-query" value="'+esc(context.target?productLabel(context.target):raw.targetLabel||'')+'" placeholder="Search an existing device, coil or pod"><input name="targetProductId" data-draft="targetProductId" type="hidden" value="'+esc(raw.targetProductId||context.target?.id||'')+'"><div class="ce-target-results" id="ce-target-results"></div></label>'+selectField('Relationship','relationship',raw.relationship||'accepts_pod',[['accepts_pod','Accepts pod'],['accepts_coil','Accepts coil'],['includes_tank','Includes tank']])+selectField('Direction','direction',raw.direction||'outgoing',[['outgoing','This product accepts / includes the selected product'],['incoming','The selected product accepts / includes this product']])+field('Conditions or generation exceptions','conditions',raw.conditions,{wide:true});
 }else if(kind==='manufacturer_alias')markup=manufacturer+field('Missing manufacturer alias','alias',raw.alias);
 else if(kind==='catalog_association')markup=catalogAssociationFields(raw,context);
 return '<div class="ce-form-grid">'+markup+evidenceFields(raw)+'</div>';
}
function sourceFrom(raw){return raw.sourceURL?.trim()?[{url:raw.sourceURL.trim(),title:raw.sourceTitle?.trim()||'',publisher:raw.sourcePublisher?.trim()||'',claim:raw.sourceClaim?.trim()||''}]:[]}
function numberField(raw,key){if(raw[key]===''||raw[key]==null)return null;const value=Number(raw[key]);if(!Number.isFinite(value)||value<0)throw new Error('Enter a valid non-negative '+key+'.');return value}
function buildProposed(kind,raw,context){
 const evidence=sourceFrom(raw);
 if(kind==='addition'||kind==='correction'){
  const value={name:raw.name?.trim()||'',manufacturerId:raw.manufacturerId||null,model:raw.model?.trim()||'',family:raw.family?.trim()||'',generation:raw.generation?.trim()||'',resistanceOhm:numberField(raw,'resistanceOhm'),capacityMl:numberField(raw,'capacityMl'),configuration:raw.configuration?.trim()||'',kind:raw.kind};
  const integrated=raw.integratedCoil===true||raw.integratedCoil==='true'?true:raw.integratedCoil===false||raw.integratedCoil==='false'?false:null;
  if(['pod','tank'].includes(value.kind)&&integrated===null)throw new Error('Choose whether this pod or tank has an integrated or replaceable coil.');
  if(integrated!==null)value.integratedCoil=integrated;
  if(raw.manufacturerName?.trim())value.manufacturerName=raw.manufacturerName.trim();
  if(kind==='addition'){
   if(!value.name||(!value.manufacturerId&&!value.manufacturerName))throw new Error('Enter a product name and choose or name its manufacturer.');
   if(raw.identifierValue)value.identifiers=[{kind:raw.identifierKind,value:raw.identifierValue,packageConfiguration:raw.packageConfiguration||''}];
   if(evidence.length)value.evidence=evidence;
   return value;
  }
  const patch={};for(const [key,valueOfField] of Object.entries(value)){const before=context.product?.[key]??(typeof valueOfField==='number'||valueOfField===null?null:'');if(String(before??'')!==String(valueOfField??''))patch[key]=valueOfField}
  if(!Object.keys(patch).length)throw new Error('Change at least one product field.');
  return {patch,evidence};
 }
 if(kind==='identifier'){
  const identifierId=raw.identifierId||context.identifierId;
  if(raw.identifierAction==='remove'&&identifierId)return {identifierId,remove:true,evidence};
  if(!raw.identifierValue)throw new Error('Enter the complete exact identifier.');
  return {kind:raw.identifierKind,value:raw.identifierValue,packageConfiguration:raw.packageConfiguration||'',evidence,...(identifierId?{identifierId}:{})};
 }
 if(kind==='relationship'){
  const relationshipId=raw.relationshipId||context.relationshipId;
  if(raw.relationshipAction==='remove'&&relationshipId)return {relationshipId,remove:true,evidence};
  if(!raw.targetProductId)throw new Error('Select the compatible product from search results.');
  return {targetProductId:raw.targetProductId,relationship:raw.relationship,direction:raw.direction,conditions:raw.conditions?.trim()||'',evidence,...(relationshipId?{relationshipId}:{})};
 }
 if(kind==='manufacturer_alias'){
  if(!raw.manufacturerId||!raw.alias?.trim())throw new Error('Choose a manufacturer and enter its missing alias.');
  return {manufacturerId:raw.manufacturerId,alias:raw.alias.trim(),evidence};
 }
 if(kind==='catalog_association'){
  if(!raw.squareVariationId?.trim())throw new Error('Select an Erupted catalog product or enter its advanced identifier.');
  return {squareVariationId:raw.squareVariationId.trim(),packageConfiguration:raw.packageConfiguration||'',evidence,...(raw.catalogAction==='remove'?{remove:true}:raw.catalogAction==='reassign'?{reassign:true}:{})};
 }
 throw new Error('Choose a supported suggestion type.');
}
function saveSuggestionDraft(){const form=$('#ce-suggestion-form');if(state.suggestion&&form)draftCache.set(state.suggestion.key,{kind:state.suggestion.kind,raw:readRaw(form),context:state.suggestion})}
function openSuggestion(kind=null,relation=null,identifier=null){
 invalidateStock();
 if(state.suggestion)saveSuggestionDraft();
 const product=state.selected,key=(product?'product:'+keyOf(product.id,state.identifierId):'search:'+$('#ce-query').value)+(relation?':relationship:'+relation.id:'')+(identifier?':identifier:'+identifier.id:'');
 const scannedIdentifier=product?(state.identifierId!=null?state.scannedIdentifier:null):state.scannedIdentifier;
 const saved=draftCache.get(key);
 const context=saved?.context||{key,product,target:relation?.target||null,relationshipId:relation?.id||null,identifierId:identifier?.id||null,scannedIdentifier,kind:kind||(product?'correction':'addition')};
 context.kind=kind||saved?.kind||context.kind;state.suggestion=context;
 let raw=saved?.raw||proposedRaw(context.kind,identifier?{...identifier,identifierId:identifier.id}:relation?{relationshipId:relation.id,targetProductId:relation.target.id,relationship:relation.relationship,direction:relation.direction,conditions:relation.conditions,evidence:relation.evidence}: {},product,context.scannedIdentifier);
 if(!product&&context.kind==='addition'&&!context.scannedIdentifier&&!raw.name)raw.name=$('#ce-query').value;
 $('#ce-explorer').hidden=true;$('#ce-review').hidden=true;$('#ce-suggestion').hidden=false;
 renderSuggestion(context,raw);$('#ce-suggestion').scrollIntoView({behavior:'smooth',block:'start'});
}
function closeSuggestion({save=true}={}){
 if(save)saveSuggestionDraft();targetNumber++;catalogNumber++;clearTimeout(catalogTimer);$('#ce-suggestion').hidden=true;$('#ce-explorer').hidden=false;state.suggestion=null;$('#ce-query').focus({preventScroll:true});
}
function renderSuggestion(context,raw){
 targetNumber++;catalogNumber++;clearTimeout(catalogTimer);
 const kinds=context.product?['correction','identifier','relationship','addition','manufacturer_alias','catalog_association']:['addition','manufacturer_alias'];
 $('#ce-suggestion').innerHTML='<button class="fi-text-button ce-back" type="button" data-close-suggestion>← Back to Explorer</button><h2>Suggest an addition or correction</h2><p class="fi-hint">A Lead reviews this information before it becomes available company-wide.</p><div class="ce-context">'+(context.product?esc(productLabel(context.product)):'New product')+(context.scannedIdentifier?'<br>Scanned identifier: <code>'+esc(context.scannedIdentifier)+'</code>':'')+'</div><form id="ce-suggestion-form">'+selectField('What should change?','suggestionKind',context.kind,kinds.map(kind=>[kind,suggestionLabel(kind)]))+'<div id="ce-proposed-fields">'+proposedFields(context.kind,raw,context)+'</div><label class="fi-field">Anything the reviewer should know?<textarea data-draft="comment" name="comment" rows="2" maxlength="2000">'+esc(raw.comment||'')+'</textarea></label><div class="ce-actions"><button type="submit" class="v2-button v2-button--primary">Submit for Lead review</button></div></form>';
 $('#ce-suggestion').querySelector('[data-close-suggestion]').onclick=()=>closeSuggestion();
 const form=$('#ce-suggestion-form');
 form.querySelector('[name="suggestionKind"]').onchange=event=>{const nextRaw=readRaw(form);context.kind=event.target.value;renderSuggestion(context,nextRaw)};
 form.onsubmit=event=>{event.preventDefault();submitSuggestion(form,context).catch(report)};
 wireTargetSearch(form,context);wireCoilDesign(form,context);wireCatalogSearch(form,context);
}
function wireCoilDesign(form,context){
 const kind=form.querySelector('[name="kind"]'),design=form.querySelector('[name="integratedCoil"]');if(!kind||!design)return;
 kind.onchange=()=>{if(['pod','tank'].includes(kind.value)&&!['pod','tank'].includes(context.product?.kind))design.value=''};
}
function applyRelationshipDefaults(form,context,target){
 if(context.relationshipId||form.querySelector('[name="relationshipId"]')?.value)return;
 const current=context.product?.kind,other=target.kind;let relationship,direction;
 if(current==='device'&&other==='pod'){relationship='accepts_pod';direction='outgoing'}
 else if(current==='pod'&&other==='device'){relationship='accepts_pod';direction='incoming'}
 else if(current==='device'&&other==='tank'){relationship='includes_tank';direction='outgoing'}
 else if(current==='tank'&&other==='device'){relationship='includes_tank';direction='incoming'}
 else if(['device','pod','tank'].includes(current)&&other==='coil'){relationship='accepts_coil';direction='outgoing'}
 else if(current==='coil'&&['device','pod','tank'].includes(other)){relationship='accepts_coil';direction='incoming'}
 if(relationship){form.querySelector('[name="relationship"]').value=relationship;form.querySelector('[name="direction"]').value=direction}
}
function wireTargetSearch(form,context){
 const query=form.querySelector('#ce-target-query');if(!query)return;
 query.oninput=()=>{
  clearTimeout(targetTimer);const sequence=++targetNumber;form.querySelector('[name="targetProductId"]').value='';context.target=null;
  targetTimer=setTimeout(async()=>{
   const value=query.value;if(!value.trim()){$('#ce-target-results').innerHTML='';return}
   try{
    const data=await request('/search?'+new URLSearchParams({q:value,mode:'name'}));
    if(sequence!==targetNumber||!query.isConnected)return;
    const rows=(data.items||[]).filter(product=>String(product.id)!==String(context.product?.id));
    const results=form.querySelector('#ce-target-results');results.innerHTML=rows.slice(0,12).map((product,index)=>'<button type="button" data-target="'+index+'">'+esc(productLabel(product))+'<small>'+esc(kindLabel(product.kind))+'</small></button>').join('')||empty('No matching record. Suggest a new product first.');
    results.querySelectorAll('[data-target]').forEach(button=>button.onclick=()=>{const product=rows[Number(button.dataset.target)];context.target=product;form.querySelector('[name="targetProductId"]').value=product.id;applyRelationshipDefaults(form,context,product);query.value=productLabel(product);results.innerHTML=''});
   }catch(error){if(sequence===targetNumber)report(error)}
  },180);
 };
}
async function submitSuggestion(form,context){
 const raw=readRaw(form),proposed=buildProposed(context.kind,raw,context),body={kind:context.kind,proposed,comment:raw.comment?.trim()||''};
 if(context.product){body.productId=context.product.id;body.baseVersion=context.product.version}
 if(context.scannedIdentifier!=null)body.scannedIdentifier=context.scannedIdentifier;
 if(proposed.targetProductId)body.targetProductId=proposed.targetProductId;
 const controls=[...form.querySelectorAll('input,select,textarea,button')];controls.forEach(control=>control.disabled=true);
 try{
  const result=await request('/suggestions',{method:'POST',body});
  const cached=draftCache.get(context.key);
  if(cached&&cached.kind===body.kind&&JSON.stringify(cached.raw)===JSON.stringify(raw))draftCache.delete(context.key);
  if(state.suggestion===context&&$('#ce-suggestion-form')===form&&!$('#ce-suggestion').hidden)closeSuggestion({save:false});
  message('Suggestion '+(result.id||result.suggestionId||'')+' submitted for Lead review. Verified records remain unchanged until approval.');
 }finally{controls.forEach(control=>{if(control.isConnected)control.disabled=false})}
}
function showExplorer(){queueNumber++;catalogNumber++;clearTimeout(catalogTimer);$('#ce-review').hidden=true;$('#ce-suggestion').hidden=true;$('#ce-explorer').hidden=false;$('#ce-query').focus({preventScroll:true})}
async function openReview(){
 if(!boot.capabilities?.review)return;
 if(state.suggestion)saveSuggestionDraft();invalidateStock();
 $('#ce-explorer').hidden=true;$('#ce-suggestion').hidden=true;$('#ce-review').hidden=false;
 $('#ce-review').innerHTML='<button class="fi-text-button ce-back" type="button" data-review-back>← Back to Explorer</button><div class="fi-toolbar"><div><h2>Lead review</h2><p class="fi-hint">Review staff suggestions and publish verified changes company-wide.</p></div><button class="v2-button v2-button--secondary" type="button" data-refresh-queue>Refresh</button></div><div class="ce-queue-layout"><div class="fi-card fi-list" id="ce-queue">'+empty('Loading suggestions…')+'</div><section class="fi-card" id="ce-review-detail">'+empty('Choose a suggestion to review.')+'</section></div>';
 $('#ce-review').querySelector('[data-review-back]').onclick=showExplorer;
 $('#ce-review').querySelector('[data-refresh-queue]').onclick=loadQueue;
 await loadQueue();
}
async function loadQueue(){
 const sequence=++queueNumber;
 try{
  const data=await request('/suggestions?status=PENDING');if(sequence!==queueNumber||$('#ce-review').hidden)return;
  state.queue=data.items||[];renderQueue();$('#ce-pending-count').textContent=state.queue.length?'('+state.queue.length+')':'';
 }catch(error){if(sequence===queueNumber)report(error)}
}
function renderQueue(){
 $('#ce-queue').innerHTML=state.queue.map(row=>'<button type="button" class="ce-queue-row" data-review-id="'+esc(row.id)+'"><strong>'+esc(row.product?productLabel(row.product):row.proposed?.name||suggestionLabel(row.kind))+'</strong><small>'+esc(suggestionLabel(row.kind))+'</small><small>'+esc(person(row.employee))+' · '+esc(person(row.store))+' · '+esc(when(row.submittedAt))+'</small></button>').join('')||empty('No pending compatibility suggestions.');
 $('#ce-queue').querySelectorAll('[data-review-id]').forEach(button=>button.onclick=()=>reviewSuggestion(button.dataset.reviewId));
}
async function reviewSuggestion(id){
 const row=state.queue.find(item=>String(item.id)===String(id));if(!row)return;state.review=row;$('#ce-review-detail').innerHTML=empty('Loading suggestion…');
 const sequence=++targetNumber;catalogNumber++;clearTimeout(catalogTimer);let product=row.product||null;
 try{
  if(row.productId!=null)product=await request(detailPath(row.productId,'',null));
  if(sequence!==targetNumber||state.review!==row)return;
  const existing=row.kind==='relationship'&&row.proposed?.relationshipId?(product?.relationships||[]).find(relation=>String(relation.id)===String(row.proposed.relationshipId)&&(!relation.via||(Array.isArray(relation.via)&&!relation.via.length))):null;
  const existingIdentifier=row.kind==='identifier'&&row.proposed?.identifierId?((product?.identifiers||[]).find(identifier=>identifier.id===row.proposed.identifierId)||row.original?._identifierBefore):null;
  const context={product,target:existing?.target||null,relationshipId:row.proposed?.relationshipId||null,identifierId:row.proposed?.identifierId||null,lockCatalog:true,catalogIdentity:row.original?._catalogVariationBefore||null};
  const targetId=row.proposed?.targetProductId||row.targetProductId;
  if(row.kind==='relationship'&&targetId&&!context.target){context.target=await request(detailPath(targetId,'',null));if(sequence!==targetNumber||state.review!==row)return}
  const proposal=existingIdentifier?{...existingIdentifier,packageConfiguration:existingIdentifier.packageConfiguration??existingIdentifier.package_configuration??'',...row.proposed}:existing?{targetProductId:existing.target.id,relationship:existing.relationship,direction:existing.direction,conditions:existing.conditions,...row.proposed}:row.proposed;
  const raw=proposedRaw(row.kind,proposal,product,row.scannedIdentifier);
  $('#ce-review-detail').innerHTML='<h2>'+esc(suggestionLabel(row.kind))+'</h2><p class="fi-hint">'+esc(person(row.employee))+' · '+esc(person(row.store))+' · '+esc(when(row.submittedAt))+'</p>'+(product?'<div class="ce-context">'+esc(productLabel(product))+'</div>':'')+proposalSummary(row,product,context.target)+associationWarning(row,product)+(row.comment?'<p>'+esc(row.comment)+'</p>':'')+'<details class="ce-proposal"><summary>Original submitted proposal</summary><pre>'+esc(JSON.stringify(row.proposed,null,2))+'</pre></details><form id="ce-review-form"><div id="ce-modified-fields" hidden>'+proposedFields(row.kind,raw,context)+'</div><label class="fi-field">Review note<input name="reason" maxlength="2000"></label><div class="ce-review-actions"><button type="button" class="v2-button v2-button--secondary" data-decision="REJECTED">Reject</button><button type="button" class="v2-button v2-button--secondary" data-modify>Modify & approve</button><button type="button" class="v2-button v2-button--primary" data-decision="APPROVED">Approve & publish</button></div></form>';
  const form=$('#ce-review-form');let modifying=false;
  const associationIdInput=form.querySelector('[name="squareVariationId"]');if(associationIdInput)associationIdInput.readOnly=true;
  form.onsubmit=event=>event.preventDefault();
  form.querySelector('[data-modify]').onclick=event=>{
   if(!modifying){modifying=true;$('#ce-modified-fields').hidden=false;event.currentTarget.textContent='Publish modified suggestion';wireTargetSearch(form,context);wireCoilDesign(form,context);wireCatalogSearch(form,context);return}
   const value=readRaw(form);let proposed;
   try{proposed=buildProposed(row.kind,value,context)}catch(error){report(error);return}
   decide(row,'MODIFIED_APPROVED',form,{proposed}).catch(report);
  };
  form.querySelectorAll('[data-decision]').forEach(button=>button.onclick=()=>decide(row,button.dataset.decision,form).catch(report));
 }catch(error){if(sequence===targetNumber)report(error)}
}
async function decide(row,decision,form,extra={}){
 const body={decision,reason:form.querySelector('[name="reason"]').value.trim(),...extra};
 const controls=[...form.querySelectorAll('input,select,textarea,button')];controls.forEach(control=>control.disabled=true);
 try{
  await request('/suggestions/'+encodeURIComponent(row.id)+'/review',{method:'POST',body});
  state.queue=state.queue.filter(item=>String(item.id)!==String(row.id));renderQueue();$('#ce-pending-count').textContent=state.queue.length?'('+state.queue.length+')':'';
  if(state.review===row){state.review=null;$('#ce-review-detail').innerHTML=empty(decision==='REJECTED'?'Suggestion rejected.':'Published. Approved records are available company-wide.')}
  message(decision==='REJECTED'?'Suggestion rejected.':'Approved compatibility information is now searchable company-wide.');
  searchNumber++;
 }finally{controls.forEach(control=>{if(control.isConnected)control.disabled=false})}
}
function bindSearch(){
 document.addEventListener?.('visibilitychange',()=>{if(!document.hidden&&state.stockValidUntil&&Date.now()>=state.stockValidUntil)stockExpiryCheck?.()});
 $('#ce-search-form').onsubmit=event=>{event.preventDefault();search({mode:identifierLike($('#ce-query').value)?'identifier':'auto'}).catch(report)};
 $('#ce-query').oninput=()=>{
  clearTimeout(searchTimer);state.active=-1;detailNumber++;invalidateStock();if(state.selected)revealResults();const query=$('#ce-query').value;
  searchTimer=setTimeout(()=>search({mode:identifierLike(query)?'identifier':'auto'}).catch(report),180);
 };
 $('#ce-query').onkeydown=event=>{
  if(event.isComposing)return;
  if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){keyTimes.push(Date.now());keyTimes=keyTimes.slice(-18);return}
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){
   if(!state.results.length)return;event.preventDefault();keyTimes=[];if(state.selected)revealResults();state.active=state.active<0?(event.key==='ArrowDown'?0:state.results.length-1):(state.active+(event.key==='ArrowDown'?1:-1)+state.results.length)%state.results.length;renderResults();$('#ce-option-'+state.active)?.scrollIntoView({block:'nearest'});return;
  }
  if(event.key==='Enter'){
   event.preventDefault();const query=$('#ce-query').value,scanned=scannerBurst(keyTimes,Date.now());
   if(!scanned&&!identifierLike(query)&&state.resultQuery===query&&state.active>=0){const product=state.results[state.active];openProduct(product.id,{identifierId:product.matchedIdentifier?.id});return}
   search({mode:scanned||identifierLike(query)?'identifier':'auto'}).catch(report);
  }else if(event.key==='Escape'){event.preventDefault();if(state.selected)backFromProduct();else clearSearch()}
 };
 $('#ce-clear').onclick=clearSearch;
 $('#ce-results').onclick=event=>{const button=event.target.closest('[data-result]');if(button){const product=state.results[Number(button.dataset.result)];state.active=Number(button.dataset.result);renderResults();openProduct(product.id,{identifierId:product.matchedIdentifier?.id})}};
 $('#ce-search-context').onclick=event=>{if(event.target.closest('[data-suggest-unknown]'))openSuggestion('addition');else if(event.target.closest('[data-name-fallback]')){state.scannedIdentifier=null;search({mode:'name'}).catch(report)}};
}
async function start(){
 try{
  boot=await request('/bootstrap');bindSearch();renderResults();
  $('#ce-open-suggestion').onclick=()=>openSuggestion();
  $('#ce-open-review').hidden=!boot.capabilities?.review;$('#ce-open-review').onclick=openReview;
  const pending=boot.counts?.pendingSuggestions;$('#ce-pending-count').textContent=Number(pending)>0?'('+Number(pending)+')':'';
  $('#ce-query').focus();await restoreRequestContext();
 }catch(error){report(error);$('#ce-results').innerHTML=empty('Compatibility records are temporarily unavailable. Try again when your connection is restored.')}
}
start();
