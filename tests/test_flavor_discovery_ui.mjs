import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {NOTES,CATEGORIES,STORES} from '../app/static/v2/flavor-explorer/catalog.mjs';
import {discover,availabilityFor,brandNamesFor,brandKey,sensoryPresence,matchesSensory} from '../app/static/v2/flavor-explorer/engine.mjs';
import {productArt,categoryArt} from '../app/static/v2/flavor-explorer/art.mjs';
import {safeSquareImage,safeProductImage,productImageCaption} from '../app/static/v2/flavor-explorer/data-source.mjs';

// Run the actual customer controller against a small DOM surface and supplied
// read responses. No browser, provider, timer loop or fabricated catalog fallback.
function explorerController(payloads){
 const nodes=new Map();
 function element(selector){
  if(!nodes.has(selector)){
   const classes=new Set();
   nodes.set(selector,{
    innerHTML:'',textContent:'',hidden:false,value:'',dataset:{},open:false,isConnected:true,
    classList:{add:(...values)=>values.forEach(v=>classes.add(v)),
     remove:(...values)=>values.forEach(v=>classes.delete(v)),
     toggle:(value,force)=>{const next=force??!classes.has(value);next?classes.add(value):classes.delete(value);return next},
     contains:value=>classes.has(value)},
    setAttribute(){},listeners:{},addEventListener(name,handler){this.listeners[name]=handler},focus(){},
    showModal(){this.open=true},close(){this.open=false},
    querySelector(){return null},querySelectorAll(){return []},
   });
  }
  return nodes.get(selector);
 }
 const images=[];
 const document={querySelector:element,querySelectorAll(selector){return selector==='.square-product-image'?images:[]},
  addEventListener(){},activeElement:null,body:element('body')};
 let source=readFileSync(new URL('../app/static/v2/flavor-explorer/explorer.mjs',import.meta.url),'utf8');
 source=source.replace(/^import .+;\s*$/gm,'')
  .replace(/\ninitialize\(\);\s*\nsetInterval\(refreshCatalog,15000\);/,'\n');
 const create=new Function('document','NOTES','CATEGORIES','STORES','loadCatalog',
  'safeProductImage','productImageCaption','discover','availabilityFor','brandNamesFor','brandKey','sensoryPresence','matchesSensory','productArt','categoryArt',
  'setTimeout','clearTimeout',source+'\nreturn {initialize,refreshCatalog,openProduct,bindImages};');
 const load=async()=>{const value=payloads.shift();if(value instanceof Error)throw value;assert.ok(value,'Expected synthetic read response');return value};
 const controller=create(document,NOTES,CATEGORIES,STORES,load,safeProductImage,productImageCaption,
  discover,availabilityFor,brandNamesFor,brandKey,sensoryPresence,matchesSensory,productArt,categoryArt,()=>1,()=>{});
 return {controller,element,images};
}
function product(id,name,imageUrl,notes=[]){
 return {id,name,brand:'Synthetic catalog',type:'freebase',formatLabel:'Freebase e-liquid',
  sizeLabel:'100 mL',seriesName:'',description:'',notes,categories:[],sensory:{},
  strengths:[3],variations:[{id:id+'-3',strengthMg:3,availability:{}}],
  imageUrl,inventorySource:'square',classificationConfidence:'provisional',accent:'#aabbcc'};
}
function payload(products){
 return {products,stores:STORES,noteDefinitions:[],meta:{source:'square',
  assignedStoreId:'hwy99',locationSelectionAllowed:true,
  inventoryObservedAt:new Date().toISOString(),inventoryTtlSeconds:300}};
}

test('live exclusion refresh removes the old hero and details, then clears the final hero for an empty catalog',async()=>{
 const hidden=product('hidden','Raspberry hidden','https://images.squareup.com/hidden.jpg',['raspberry']);
 const visible=product('visible','Mango visible','https://images.squareup.com/visible.jpg',['mango']);
 const {controller,element}=explorerController([
  payload([hidden,visible]),payload([visible]),payload([]),
 ]);
 await controller.initialize();
 assert.match(element('#hero-art').innerHTML,/hidden\.jpg/);
 controller.openProduct(hidden.id);
 assert.equal(element('#product-dialog').open,true);
 element('#hero-art').classList.add('has-square-image');
 await controller.refreshCatalog();
 assert.doesNotMatch(element('#hero-art').innerHTML,/hidden\.jpg/);
 assert.match(element('#hero-art').innerHTML,/visible\.jpg/);
 assert.equal(element('#hero-art').classList.contains('has-square-image'),false);
 assert.equal(element('#product-dialog').open,false);
 assert.doesNotMatch(element('#product-grid').innerHTML,/Raspberry hidden/);
 element('#hero-art').classList.add('has-square-image');
 await controller.refreshCatalog();
 assert.equal(element('#hero-art').innerHTML,'');
 assert.equal(element('#hero-art').classList.contains('has-square-image'),false);
 assert.equal(element('#product-grid').innerHTML,'');
 assert.equal(element('#result-count').textContent,'0 matches');
});

test('initial empty customer projection succeeds without a sample hero or invented availability',async()=>{
 const {controller,element}=explorerController([payload([])]);
 await controller.initialize();
 assert.equal(element('#hero-art').innerHTML,'');
 assert.equal(element('#product-grid').innerHTML,'');
 assert.equal(element('#result-count').textContent,'0 matches');
 assert.equal(element('#empty-state').hidden,false);
 assert.equal(element('#empty-reset').hidden,false);
});


test('staff photos use only the fixed local raster path and render in hero, cards and detail with catalog reset fallback',async()=>{
 const local='/flavor-explorer/media/photos/'+'a'.repeat(64)+'.webp';
 assert.equal(safeSquareImage(local),null,'the Square-origin policy remains unchanged');
 assert.equal(safeProductImage(local),local);
 for(const value of [local+'?x=1',local+'#fragment',local.replace('.webp','.svg'),local.replace('a'.repeat(64),'../private'),
  'https://untrusted.example'+local,'//untrusted.example/photo.webp','data:image/svg+xml,<svg/>','/other/photo.webp'])
  assert.equal(safeProductImage(value),null);
 const custom={...product('photo','Raspberry photo',local,['raspberry']),imageSource:'staff-photo'};
 const hidden={...custom,imageUrl:null,imageSource:'illustration-fallback'};
 const catalog={...custom,imageUrl:'https://images.squareup.com/catalog-photo.jpg',imageSource:'square'};
 const {controller,element}=explorerController([payload([custom]),payload([hidden]),payload([catalog])]);
 await controller.initialize();
 assert.match(element('#hero-art').innerHTML,/\/flavor-explorer\/media\/photos\/a{64}\.webp/);
 assert.match(element('#product-grid').innerHTML,/\/flavor-explorer\/media\/photos\/a{64}\.webp/);
 assert.match(element('.header-preview').textContent,/STAFF PREVIEW/);
 controller.openProduct(custom.id);
 assert.match(element('#detail-content').innerHTML,/PRODUCT PHOTO/);
 assert.doesNotMatch(element('#detail-content').innerHTML,/PRODUCT IMAGE FROM SQUARE/);
 await controller.refreshCatalog();
 assert.doesNotMatch(element('#hero-art').innerHTML,/\/media\/photos\//);
 assert.doesNotMatch(element('#product-grid').innerHTML,/\/media\/photos\//);
 assert.match(element('#detail-content').innerHTML,/PRODUCT IMAGE COMING SOON/);
 await controller.refreshCatalog();
 assert.match(element('#hero-art').innerHTML,/catalog-photo\.jpg/);
 assert.match(element('#detail-content').innerHTML,/PRODUCT IMAGE FROM SQUARE/);
});

test('brand selector exposes overlapping families without duplicate spelling options',async()=>{
 const p={...product('reds','Apple',null,[]),brand:"Red's",brands:["Red's",'Reds Apple','7 Daze','7Daze']};
 const {controller,element}=explorerController([payload([p])]);
 await controller.initialize();
 assert.equal((element('#brands').innerHTML.match(/data-brand="7 Daze"/g)||[]).length,1);
 assert.equal((element('#brands').innerHTML.match(/data-brand="Red&#39;s"/g)||[]).length,1);
});

test('browse sorts unavailable products after all pages and reorders on store, strength and catalog updates',async()=>{
 const checkedAt=new Date().toISOString(),validUntil=new Date(Date.now()+300000).toISOString();
 const stock=(state)=>({state,checkedAt,validUntil});
 const first=product('first','First unavailable',null);
 first.variations=[{id:'first-3',strengthMg:3,availability:{hwy99:stock('out_of_stock'),longview:stock('available')}},
  {id:'first-6',strengthMg:6,availability:{hwy99:stock('out_of_stock'),longview:stock('available')}}];first.strengths=[3,6];
 const rest=Array.from({length:25},(_,i)=>{
  const p=product('other-'+i,'Other '+i,null);
  p.variations[0].availability={hwy99:stock('available'),longview:stock('out_of_stock')};
  if(i===0){p.variations[0].availability.hwy99=stock('out_of_stock');p.variations.push({id:p.id+'-6',strengthMg:6,availability:{hwy99:stock('available'),longview:stock('out_of_stock')}});p.strengths=[3,6]}
  return p;
 });
 const refreshed=structuredClone(first);
 refreshed.variations.forEach(v=>v.availability.hwy99=stock('available'));
 const {controller,element}=explorerController([payload([first,...rest]),payload([refreshed,...rest])]);
 const ids=()=>[...element('#product-grid').innerHTML.matchAll(/data-product="([^"]+)"/g)].map(m=>m[1]);
 const click=(selector,key,value)=>element(selector).listeners.click({target:{closest:()=>({dataset:{[key]:value}})}});
 await controller.initialize();
 assert.equal(ids().length,24);assert.equal(ids().includes('first'),false,'out-of-stock cannot take a first-page position');
 click('#more-products','unused','unused');
 assert.equal(ids().at(-1),'first');
 click('#location-list','store','longview');
 assert.equal(ids()[0],'first','selected store stock changes the order immediately');
 click('#location-list','store','hwy99');
 assert.equal(ids().at(-1),'first');
 await controller.refreshCatalog();
 assert.equal(ids()[0],'first','live stock refresh restores catalog position');
 click('#strengths','strength','3');
 assert.equal(ids().includes('other-0'),false,'a stocked alternate strength does not override the selected strength');
 click('#more-products','unused','unused');
 assert.equal(ids().at(-1),'other-0');
 click('#strengths','strength','6');
 assert.deepEqual(ids(),['first','other-0']);
});

test('a failed photo survives and retries after an unchanged catalog response',async()=>{
 const data=payload([product('p','Peach','/flavor-explorer/media/photos/'+'a'.repeat(64)+'.webp')]);
 const {controller,element,images}=explorerController([data,data]);
 await controller.initialize();
 const image=element('synthetic-image');image.parentElement=element('synthetic-parent');
 image.getAttribute=()=>'/flavor-explorer/media/photos/'+'a'.repeat(64)+'.webp';
 image.remove=()=>assert.fail('Failed photos must remain available for recovery');
 images.push(image);controller.bindImages();
 image.listeners.error();assert.equal(image.dataset.failed,'true');
 await controller.refreshCatalog();
 assert.equal(image.src,image.getAttribute('src'));assert.equal(image.dataset.failed,undefined);
 image.listeners.load();assert.ok(image.parentElement.classList.contains('has-square-image'));
});

test('unrated controls are explicit while verified cooling and cream remain filterable',async()=>{
 const p={...product('ice','Reviewed Ice',null),classificationStatus:'approved',classificationConfidence:'established',description:'Menthol cooling.',categories:['ice'],sensory:{}};
 const q={...product('cream','Reviewed Cream',null,['cream']),classificationStatus:'approved',classificationConfidence:'established',categories:['dessert'],sensory:{}};
 const {controller,element}=explorerController([payload([p,q])]);
 await controller.initialize();
 const html=element('#sensory-filters').innerHTML;
 assert.match(html,/data-sensory="sweetness"[^>]* disabled[^>]*>Sweet · unrated/);
 assert.match(html,/data-sensory="cooling" aria-pressed="false">Cooling/);
 assert.match(html,/data-sensory="creaminess" aria-pressed="false">Creamy/);
 assert.match(element('#product-grid').innerHTML,/Menthol cooling\./);
 controller.openProduct('ice');
 assert.match(element('#detail-content').innerHTML,/Present · intensity unrated/);
 assert.match(element('#detail-content').innerHTML,/class="profile-meter" aria-hidden="true"/);
});
