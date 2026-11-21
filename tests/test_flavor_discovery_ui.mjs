import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {NOTES,CATEGORIES,STORES} from '../app/static/v2/flavor-explorer/catalog.mjs';
import {discover,availabilityFor,brandNamesFor,brandKey} from '../app/static/v2/flavor-explorer/engine.mjs';
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
    setAttribute(){},addEventListener(){},focus(){},
    showModal(){this.open=true},close(){this.open=false},
    querySelector(){return null},querySelectorAll(){return []},
   });
  }
  return nodes.get(selector);
 }
 const document={querySelector:element,querySelectorAll(){return []},
  addEventListener(){},activeElement:null,body:element('body')};
 let source=readFileSync(new URL('../app/static/v2/flavor-explorer/explorer.mjs',import.meta.url),'utf8');
 source=source.replace(/^import .+;\s*$/gm,'')
  .replace(/\ninitialize\(\);\s*\nsetInterval\(refreshCatalog,15000\);/,'\n');
 const create=new Function('document','NOTES','CATEGORIES','STORES','loadCatalog',
  'safeProductImage','productImageCaption','discover','availabilityFor','brandNamesFor','brandKey','productArt','categoryArt',
  'setTimeout','clearTimeout',source+'\nreturn {initialize,refreshCatalog,openProduct};');
 const load=async()=>{const value=payloads.shift();if(value instanceof Error)throw value;assert.ok(value,'Expected synthetic read response');return value};
 const controller=create(document,NOTES,CATEGORIES,STORES,load,safeProductImage,productImageCaption,
  discover,availabilityFor,brandNamesFor,brandKey,productArt,categoryArt,()=>1,()=>{});
 return {controller,element};
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
