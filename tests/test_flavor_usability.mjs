
import test from 'node:test';
import assert from 'node:assert/strict';
import {discover,brandNamesFor,brandKey} from '../app/static/v2/flavor-explorer/engine.mjs';
import {PRODUCTS} from '../app/static/v2/flavor-explorer/catalog.mjs';
import {conciseName,relationshipGroup} from '../app/static/v2/flavor-explorer/compatibility-display.mjs';
test('brand + freebase + 3mg + fruit remain strict in both matching modes',()=>{
 const p={...PRODUCTS[0],id:'reds',brand:"Red's",type:'freebase',categories:['fruit'],notes:['raspberry']};
 const alternatives=[p,{...p,id:'brand',brand:'Other'},{...p,id:'type',type:'salt'},{...p,id:'category',categories:['mint']},
  {...p,id:'strength',variations:p.variations.map(v=>({...v,strengthMg:6}))}];
 for(const mode of ['exact','explore']){
  const results=discover(alternatives,{brand:"Red's",type:'freebase',strength:3,categories:['fruit'],notes:['raspberry'],mode});
  assert.deepEqual(results.map(r=>r.product.id),['reds']);
  assert.deepEqual(discover(alternatives,{brand:'Unavailable brand',mode}),[]);
 }
});
test('mint and ice are independent, multi-category and available in both modes',()=>{
 const base={...PRODUCTS[0],notes:['mint']};
 const products=[{...base,id:'mint',categories:['mint']},{...base,id:'ice',categories:['fruit','ice']},{...base,id:'both',categories:['mint','ice']}];
 for(const mode of ['exact','explore']){
  assert.deepEqual(new Set(discover(products,{categories:['mint'],mode}).map(r=>r.product.id)),new Set(['mint','both']));
  assert.deepEqual(new Set(discover(products,{categories:['ice'],mode}).map(r=>r.product.id)),new Set(['ice','both']));
 }
 assert.equal(discover([{...base,sensory:{cooling:null}}],{sensory:['cooling']}).length,0);
 assert.equal(discover([{...base,sensory:{cooling:4}}],{sensory:['cooling']}).length,1);
});
test('concise names remove packaging while retaining compatibility and resistance distinctions',()=>{
 const original='SMOK Nord 5 Replacement Pod Cartridge 5mL 3-Pack RPM 3 Compatible';
 assert.equal(conciseName({name:original}),'SMOK Nord 5 Pods RPM 3 Compatible');
 assert.equal(conciseName({name:'Nord 5 Pods — corrected'}),'Nord 5 Pods — corrected');
 assert.notEqual(conciseName({name:original}),conciseName({name:original.replace('RPM 3','RPM 2')}));
 assert.match(conciseName({name:'X Replacement Coil 0.15Ω 5-Pack'}),/0\.15Ω/);
 assert.equal(relationshipGroup('device'),'Verified Compatible Devices');
 assert.equal(relationshipGroup('coil'),relationshipGroup('pod'));
 assert.notEqual(relationshipGroup('tank'),relationshipGroup('device'));
});

test('overlapping brands return the same product once and preserve strict gates',()=>{
 const base={...PRODUCTS[0],type:'freebase',categories:['fruit'],notes:['raspberry']};
 const reds={...base,id:'reds',brand:"Red's",brands:["Red's","7 Daze"],brandAliases:['Reds','7Daze','Seven Daze']};
 const fusion={...base,id:'fusion',brand:'Fusion',brands:['Fusion','7 Daze'],brandAliases:['7Daze','Seven Daze']};
 const other={...base,id:'other',brand:'Other'};
 const products=[reds,fusion,other,{...reds,id:'salt',type:'salt'}];
 for(const mode of ['exact','explore']){
  const options={type:'freebase',strength:3,categories:['fruit'],notes:['raspberry'],mode};
  assert.deepEqual(discover(products,{...options,brand:'7Daze'}).map(r=>r.product.id).sort(),['fusion','reds']);
  assert.deepEqual(discover(products,{...options,brand:'Reds Apple'}).map(r=>r.product.id),['reds']);
  assert.deepEqual(discover(products,{...options,query:'7Daze'}).map(r=>r.product.id).sort(),['fusion','reds']);
 }
 assert.deepEqual(brandNamesFor({...reds,brands:["Red's",'Reds Apple','7Daze','7 Daze']}),["Red's",'7 Daze']);
 assert.equal(brandKey('Seven Daze'),brandKey('7Daze'));
 assert.equal(discover([{...base,id:'rinn',brand:'RINN Bar',name:'Reds Apple'}],{brand:"Red's"}).length,0);
});
