import test from 'node:test';
import assert from 'node:assert/strict';
import {createProductPhotosAdmin,MAX_PHOTO_BYTES} from '../app/static/v2/flavor-explorer/product-photos-admin.mjs';
const flush=()=>new Promise(setImmediate);
function surface(){
 const nodes=new Map();let html='';
 const decode=value=>String(value).replace(/&(amp|lt|gt|quot|#39);/g,(_all,key)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[key]));
 const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',className:'',isConnected:true,hidden:false,src:''});return nodes.get(selector)};
 const root={
  get innerHTML(){return html},
  set innerHTML(value){
   html=value;for(const element of nodes.values())element.isConnected=false;nodes.clear();
   for(const match of html.matchAll(/<input\b[^>]*data-photo-field="([^"]+)"[^>]*value="([^"]*)"[^>]*>/g))node('[data-photo-field="'+match[1]+'"]').value=decode(match[2]);
   for(const match of html.matchAll(/<textarea\b[^>]*data-photo-field="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g))node('[data-photo-field="'+match[1]+'"]').value=decode(match[2]);
  },
  querySelector(selector){
   const match=selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
   if(match&&!html.includes(match[1]+(match[2]!==undefined?'="'+match[2]+'"':'')))return null;
   return node(selector);
  },
 };
 const click=(selector,dataset={})=>root.onclick({target:{closest:value=>value===selector?{dataset}:null}});
 const choose=file=>root.querySelector('[data-photo-file]').onchange({target:{files:[file]}});
 const source=value=>root.querySelector('[data-photo-source]').onchange({target:{value}});
 const save=()=>root.querySelector('[data-photo-form]').onsubmit({preventDefault(){}});
 return {root,node,click,choose,source,save};
}
const product=id=>({id,brand:'Photo pilot',name:'Product '+id,type:'freebase'});
const result=(id,mode='catalog',token=id+'-token',history=[])=>({
 product:product(id),photo:{productId:id,stateToken:token,mode,
  assetId:mode==='custom'?'b'.repeat(64):null,
  url:mode==='custom'?'/flavor-explorer/media/photos/'+'b'.repeat(64)+'.webp':mode==='catalog'?'https://images.squareup.com/'+id+'.jpg':null,
  sourcePageUrl:'',sourceImageUrl:'',sourceLabel:'',rightsNote:'',history},
});
const file=name=>({name,type:'image/png',size:16});

test('late file reads and uploads cannot replace a different product photo draft',async()=>{
 const view=surface(),calls=[];let finishReadA,finishUploadB,reads=0;
 const ui=createProductPhotosAdmin({readFile:async chosen=>{
  reads++;if(chosen.name==='slow-A.png')return new Promise(resolve=>{finishReadA=resolve});
  return 'data:image/png;base64,'+(chosen.name==='B.png'?'Qg==':'QQ==');
 },request:async(path,options)=>{
  calls.push({path,options});const id=path.split('/')[2];
  if(!options)return result(id);
  assert.equal(path,'/products/B/photo');assert.equal(options.body.expectedToken,'B-token');
  assert.equal(options.body.dataBase64,'Qg==');assert.equal(options.body.sourceKind,'own');return new Promise(resolve=>{finishUploadB=resolve});
 }});
 await ui.mount(view.root,'A');
 view.choose({...file('too-large.png'),size:MAX_PHOTO_BYTES+1});await flush();
 assert.equal(reads,0);assert.match(view.node('[data-photo-message]').textContent,/2 MiB/);
 view.choose(file('slow-A.png'));await ui.mount(view.root,'B');
 view.choose(file('B.png'));await flush();
 assert.equal(view.node('[data-photo-selected]').src,'data:image/png;base64,Qg==');
 finishReadA('data:image/png;base64,QQ==');await flush();
 assert.equal(view.node('[data-photo-selected]').src,'data:image/png;base64,Qg==');
 view.source('own');view.save();await flush();
 await ui.mount(view.root,'A');view.choose(file('new-A.png'));await flush();
 finishUploadB(result('B','custom','B-saved'));await flush();
 assert.match(view.root.innerHTML,/Product A/);
 assert.equal(view.node('[data-photo-selected]').src,'data:image/png;base64,QQ==');
 assert.equal(calls.filter(call=>call.options).length,1);
 ui.unmount();
});

test('remove and catalog reset use captured tokens and late removal cannot repaint another product',async()=>{
 const view=surface(),posts=[];let finishRemoveA;
 const history=[{eventId:12,mode:'hidden',at:'2026-11-20T12:00:00Z',revertible:true}];
 const ui=createProductPhotosAdmin({request:async(path,options)=>{
  const id=path.split('/')[2];if(!options)return result(id,'custom',id+'-token',history);
  posts.push({path,body:options.body});
  if(id==='A')return new Promise(resolve=>{finishRemoveA=resolve});
  if(options.body.restoreEventId){assert.equal(options.body.expectedToken,'B-catalog');return result('B','hidden','B-restored',history)}
  assert.equal(options.body.expectedToken,'B-token');assert.equal(options.body.mode,'catalog');
  return result('B','catalog','B-catalog',history);
 }});
 await ui.mount(view.root,'A');view.click('[data-photo-remove]');await flush();
 assert.deepEqual(posts[0],{path:'/products/A/photo',body:{expectedToken:'A-token',mode:'hidden'}});
 await ui.mount(view.root,'B');finishRemoveA(result('A','hidden','A-hidden'));await flush();
 assert.match(view.root.innerHTML,/Product B/);assert.match(view.root.innerHTML,/Custom photo/);
 view.click('[data-photo-catalog]');await flush();
 assert.match(view.root.innerHTML,/Catalog photo/);assert.match(view.root.innerHTML,/https:\/\/images.squareup.com\/B.jpg/);
 assert.equal(Object.hasOwn(posts[1].body,'dataBase64'),false);
 view.click('[data-photo-restore]',{photoRestore:'0'});await flush();
 assert.deepEqual(posts[2].body,{expectedToken:'B-catalog',mode:'hidden',restoreEventId:12});
 assert.match(view.root.innerHTML,/Photo removed/);
 ui.unmount();
});


test('explicit source slots keep manufacturer metadata out of our upload and remove with the current token',async()=>{
 const view=surface(),posts=[];
 const primary=result('P','custom','priority-token');
 primary.photo.effectiveSourceKind='manufacturer';
 primary.photo.sourceLabel='Manufacturer original';
 primary.photo.sourcePageUrl='https://maker.example/product';
 primary.photo.candidates={manufacturer:{url:primary.photo.url,sourceLabel:'Manufacturer original'},own:null};
 primary.photo.originalFallback={url:'/flavor-explorer/media/originals/17'};
 const ui=createProductPhotosAdmin({readFile:async()=> 'data:image/png;base64,QQ==',request:async(path,options)=>{
  if(!options)return primary;
  posts.push(options.body);
  if(options.body.action){
   assert.deepEqual(options.body,{expectedToken:'own-saved',action:'remove_candidate',sourceKind:'manufacturer'});
   return {product:product('P'),photo:{...primary.photo,stateToken:'manufacturer-removed',effectiveSourceKind:'own',candidates:{manufacturer:null,own:{url:primary.photo.url,sourceLabel:'Our photo'}}}};
  }
  assert.equal(options.body.sourceKind,'own');
  assert.equal(options.body.sourcePageUrl,'');
  assert.equal(options.body.sourceLabel,'Our photo');
  return {product:product('P'),photo:{...primary.photo,stateToken:'own-saved',candidates:{...primary.photo.candidates,own:{url:primary.photo.url,sourceLabel:'Our photo'}}}};
 }});
 await ui.mount(view.root,'P');
 assert.match(view.root.innerHTML,/Manufacturer photo/);
 assert.match(view.root.innerHTML,/Existing original · fallback/);
 view.choose(file('own.png'));await flush();
 view.save();await flush();
 assert.equal(posts.length,0);
 assert.match(view.node('[data-photo-message]').textContent,/Choose Manufacturer photo or Our photo/);
 view.source('own');
 assert.equal(view.node('[data-photo-field="sourcePageUrl"]').value,'');
 view.node('[data-photo-field="sourceLabel"]').value='Our photo';
 view.save();await flush();
 assert.match(view.node('[data-photo-message]').textContent,/manufacturer photo remains primary/);
 view.click('[data-photo-candidate-remove]',{photoCandidateRemove:'manufacturer'});await flush();
 assert.equal(posts.length,2);
 assert.match(view.root.innerHTML,/Current · Our photo/);
 assert.match(view.root.innerHTML,/Existing original · fallback/);
 ui.unmount();
});

test('only fixed local original asset paths are allowed alongside unchanged Square image hosts',async()=>{
 const {safeProductImage,productImageCaption}=await import('../app/static/v2/flavor-explorer/data-source.mjs');
 assert.equal(safeProductImage('/flavor-explorer/media/originals/17'),'/flavor-explorer/media/originals/17');
 assert.equal(productImageCaption({imageUrl:'/flavor-explorer/media/originals/17'}),'PRODUCT PHOTO');
 for(const value of ['/flavor-explorer/media/originals/0','/flavor-explorer/media/originals/01','/flavor-explorer/media/originals/17?token=x','/flavor-explorer/media/originals/17#x','https://evil.example/photo.jpg','//evil.example/flavor-explorer/media/originals/17','/flavor-explorer/media/originals/../17'])assert.equal(safeProductImage(value),null);
 assert.equal(safeProductImage('https://images.squareup.com/p.jpg'),'https://images.squareup.com/p.jpg');
});
