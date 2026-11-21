import test from 'node:test';
import assert from 'node:assert/strict';
import {createDiscoveryAdmin} from '../app/static/v2/flavor-explorer/discovery-admin.mjs';
const flush=()=>new Promise(setImmediate);
function surface(){
 const nodes=new Map();let writes=0;
 const node=selector=>{
  if(!nodes.has(selector)){
   let html='',text='';
   nodes.set(selector,{value:'',hidden:false,checked:false,disabled:false,indeterminate:false,
    get innerHTML(){return html},set innerHTML(value){html=value;writes++},
    get textContent(){return text},set textContent(value){text=value;writes++},
   });
  }return nodes.get(selector);
 };
 const root=node('root');root.querySelector=node;
 const closest=(selector,value={})=>({closest:query=>query===selector?value:null});
 const click=selector=>root.onclick({target:closest(selector)});
 const select=(id,checked=true)=>root.onchange({target:closest('[data-discovery-product]',{dataset:{discoveryProduct:id},checked})});
 const matching=checked=>root.onchange({target:closest('[data-discovery-select-matching]',{checked})});
 return {root,node,click,select,matching,writes:()=>writes};
}
const row=(id,visibility='included',total=2,excluded=0)=>({
 id,brand:'Preview brand',name:'Product '+id,type:'salt',explorerVisibility:visibility,
 variationCount:total,excludedVariationCount:excluded,visibilityVersion:id+'-version',
 squareVariationIds:Array.from({length:total},(_v,index)=>id+'-variation-'+index),
});
const initial=[row('a'),row('b','partial',3,1),row('c','excluded',1,1)];
const plan=(ids,excluded=true)=>({
 previewId:'preview-'+ids.join('-'),selectedCount:ids.length,changedCount:1,unchangedCount:ids.length-1,
 variationChangedCount:2,excluded,products:ids.map(id=>({...initial.find(item=>item.id===id),
  before:initial.find(item=>item.id===id).explorerVisibility,after:excluded?'excluded':'included'})),warnings:[],
});

test('visibility filtering retains off-filter selections and changing selection invalidates a pending preview',async()=>{
 const view=surface(),calls=[];let finishPreview,previews=0;
 const ui=createDiscoveryAdmin({request:async(path,options)=>{
  calls.push({path,options});
  if(path.startsWith('/discovery/products?')){
   const query=new URLSearchParams(path.split('?')[1]);return {items:query.get('visibility')==='excluded'?[initial[2]]:initial};
  }
  assert.equal(path,'/discovery/preview');previews++;
  if(previews===2)return new Promise(resolve=>{finishPreview=resolve});
  assert.deepEqual(options.body.productIds,['a','c']);assert.equal(options.body.excluded,true);
  assert.equal(Object.hasOwn(options.body,'actor'),false);return plan(['a','c']);
 }});
 await ui.mount(view.root);
 assert.match(view.node('[data-discovery-products]').innerHTML,/Partially excluded/);
 view.select('a');
 view.node('[data-discovery-visibility]').value='excluded';view.node('[data-discovery-visibility]').onchange();await flush();
 assert.equal(view.node('[data-discovery-selected]').textContent,'1 selected');
 assert.equal(view.node('[data-discovery-outside]').textContent,' · 1 outside current filters');
 view.matching(true);view.click('[data-discovery-exclude]');await flush();
 const preview=view.node('[data-discovery-preview]');
 assert.equal(preview.hidden,false);assert.match(preview.innerHTML,/1 products/);assert.match(preview.innerHTML,/2 current variants will change/);
 assert.match(preview.innerHTML,/Product a/);assert.match(preview.innerHTML,/Product c/);
 view.click('[data-discovery-restore]');view.click('[data-discovery-clear]');
 finishPreview(plan(['a','c'],false));await flush();
 assert.equal(preview.hidden,true);assert.equal(view.node('[data-discovery-selection]').hidden,true);
 assert.equal(calls.some(call=>call.path==='/discovery/commit'),false);
 ui.unmount();
});

test('commit uses the reviewed token and retains a newer selection while refreshing visibility metadata',async()=>{
 const view=surface(),calls=[];let finishCommit,committed=false;
 const ui=createDiscoveryAdmin({request:async(path,options)=>{
  calls.push({path,options});
  if(path.startsWith('/discovery/products?'))return {items:committed?[row('a','excluded',2,2),initial[1]]:initial.slice(0,2)};
  if(path==='/discovery/preview'){assert.deepEqual(options.body.productIds,['a']);return plan(['a'])}
  assert.equal(path,'/discovery/commit');assert.deepEqual(options.body,{previewId:'preview-a'});
  return new Promise(resolve=>{finishCommit=()=>{committed=true;resolve({operationId:'visibility-operation',changedCount:1,variationChangedCount:2})}});
 }});
 await ui.mount(view.root);view.select('a');view.click('[data-discovery-exclude]');await flush();
 view.click('[data-discovery-apply]');view.select('b');
 assert.equal(view.node('[data-discovery-exclude]').disabled,true);
 finishCommit();await flush();
 assert.equal(view.node('[data-discovery-selected]').textContent,'1 selected');
 assert.doesNotMatch(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="a"[^>]*checked/);
 assert.match(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="b"[^>]*checked/);
 assert.match(view.node('[data-discovery-products]').innerHTML,/Excluded/);
 assert.match(view.node('[data-discovery-message]').textContent,/Excluded 1 product \(2 current variants\)/);
 assert.equal(calls.every(call=>call.path.startsWith('/discovery/')),true,'visibility actions never call classification endpoints');
 ui.unmount();
});

test('unmount rejects late rows/previews and remount preserves filters, reason and exact selections',async()=>{
 const view=surface();let listCalls=0,finishRows,finishPreview;
 const ui=createDiscoveryAdmin({request:async(path,options)=>{
  if(path.startsWith('/discovery/products?')){
   listCalls++;if(listCalls===1)return {items:initial};
   const query=new URLSearchParams(path.split('?')[1]);assert.equal(query.get('q'),'berry');assert.equal(query.get('type'),'salt');assert.equal(query.get('visibility'),'included');
   if(listCalls===2)return new Promise(resolve=>{finishRows=resolve});
   return {items:[initial[0]]};
  }
  assert.equal(path,'/discovery/preview');assert.deepEqual(options.body.productIds,['b']);
  assert.equal(options.body.reason,'Assortment review');return new Promise(resolve=>{finishPreview=resolve});
 }});
 await ui.mount(view.root);view.select('b');
 const note=view.node('[data-discovery-note]');note.value='Assortment review';note.oninput({target:note});
 view.node('[data-discovery-search]').value='berry';view.node('[data-discovery-type]').value='salt';view.node('[data-discovery-visibility]').value='included';
 view.node('[data-discovery-filter]').onsubmit({preventDefault(){}});
 view.click('[data-discovery-exclude]');
 ui.unmount();const writes=view.writes();
 finishRows({items:[row('late','excluded')]});finishPreview(plan(['b']));await flush();
 assert.equal(view.writes(),writes,'a detached setup view receives no late UI updates');
 await ui.mount(view.root);
 assert.equal(view.node('[data-discovery-search]').value,'berry');assert.equal(view.node('[data-discovery-type]').value,'salt');
 assert.equal(view.node('[data-discovery-visibility]').value,'included');assert.equal(view.node('[data-discovery-note]').value,'Assortment review');
 assert.equal(view.node('[data-discovery-selected]').textContent,'1 selected');
 assert.equal(view.node('[data-discovery-outside]').textContent,' · 1 outside current filters');
 assert.equal(view.node('[data-discovery-preview]').hidden,true);
 ui.unmount();
});


test('acknowledged exclusion clears every reviewed selection, including off-filter and unchanged products',async()=>{
 const view=surface();let committed=false;
 const ui=createDiscoveryAdmin({request:async(path,options)=>{
  if(path.startsWith('/discovery/products?'))return {items:new URLSearchParams(path.split('?')[1]).get('visibility')==='excluded'?[initial[2]]:initial};
  if(path==='/discovery/preview'){assert.deepEqual(options.body.productIds,['a','c']);return plan(['a','c'])}
  assert.equal(path,'/discovery/commit');committed=true;return {operationId:'done'};
 }});
 await ui.mount(view.root);view.select('a');view.select('c');
 const filter=view.node('[data-discovery-visibility]');filter.value='excluded';filter.onchange();await flush();
 view.click('[data-discovery-exclude]');await flush();view.click('[data-discovery-apply]');await flush();
 assert.equal(committed,true);assert.equal(view.node('[data-discovery-selection]').hidden,true);
 assert.equal(view.node('[data-discovery-select-matching]').checked,false);
 assert.equal(view.node('[data-discovery-select-matching]').indeterminate,false);
 assert.doesNotMatch(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="[^"]+"[^>]*checked/);
 filter.value='all';filter.onchange();await flush();
 assert.doesNotMatch(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="[^"]+"[^>]*checked/);
 ui.unmount();
});

test('failed exclusion retains selection; acknowledged retry clears it even when list refresh fails',async()=>{
 const view=surface();let attempts=0,done=false;
 const ui=createDiscoveryAdmin({request:async(path)=>{
  if(path.startsWith('/discovery/products?')){if(done)throw new Error('Refresh unavailable');return {items:[initial[0]]}}
  if(path==='/discovery/preview')return plan(['a']);
  assert.equal(path,'/discovery/commit');if(++attempts===1)throw new Error('Not saved');done=true;return {operationId:'done'};
 }});
 await ui.mount(view.root);view.select('a');view.click('[data-discovery-exclude]');await flush();view.click('[data-discovery-apply]');await flush();
 assert.equal(view.node('[data-discovery-selection]').hidden,false);
 assert.match(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="a"[^>]*checked/);
 view.click('[data-discovery-apply]');await flush();
 assert.equal(attempts,2);assert.equal(view.node('[data-discovery-selection]').hidden,true);
 assert.doesNotMatch(view.node('[data-discovery-products]').innerHTML,/data-discovery-product="a"[^>]*checked/);
 ui.unmount();
});

test('vendor filtering is Setup-only, retains its choice on remount, and escapes labels',async()=>{
 const view=surface(),calls=[],vendors=[{id:7,name:'<Supplier>',active:true},{id:8,name:'Supplier',active:false}];
 const ui=createDiscoveryAdmin({request:async(path)=>{
  calls.push(path);assert.ok(path.startsWith('/discovery/products?'));
  const vendor=new URLSearchParams(path.split('?')[1]).get('vendor');
  return {vendors,items:vendor==='unmapped'?[]:[{...initial[0],vendors:[vendors[0]],vendorUnmappedVariationCount:1}]};
 }});
 await ui.mount(view.root);
 const products=view.node('[data-discovery-products]');
 assert.match(products.innerHTML,/Vendor: &lt;Supplier&gt;/);
 assert.match(products.innerHTML,/1 variant without a mapped vendor/);
 assert.doesNotMatch(products.innerHTML,/<Supplier>/);
 const vendor=view.node('[data-discovery-vendor]');
 assert.match(vendor.innerHTML,/value="7"/);assert.match(vendor.innerHTML,/inactive vendor/);
 vendor.value='7';vendor.onchange();await flush();
 assert.equal(new URLSearchParams(calls.at(-1).split('?')[1]).get('vendor'),'7');
 ui.unmount();await ui.mount(view.root);assert.equal(vendor.value,'7');
 vendor.value='unmapped';vendor.onchange();await flush();
 assert.equal(new URLSearchParams(calls.at(-1).split('?')[1]).get('vendor'),'unmapped');
 assert.equal(view.node('[data-discovery-product-count]').textContent,'0 matching products');
 ui.unmount();
});


test('a removed vendor mapping keeps the selected filter explicit instead of displaying All vendors',async()=>{
 const view=surface();let requests=0;
 const ui=createDiscoveryAdmin({request:async()=>({vendors:++requests===1?[{id:7,name:'Supplier',active:true}]:[],items:[]})});
 await ui.mount(view.root);
 const vendor=view.node('[data-discovery-vendor]');vendor.value='7';vendor.onchange();await flush();
 assert.equal(vendor.value,'7');
 assert.match(vendor.innerHTML,/value="7"/);
 assert.match(vendor.innerHTML,/Previously selected vendor/);
 ui.unmount();
});
