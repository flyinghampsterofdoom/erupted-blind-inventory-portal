import test from 'node:test';
import assert from 'node:assert/strict';
import {productArt} from '../app/static/v2/flavor-explorer/art.mjs';
const catalog={products:[],stores:[{id:'h'},{id:'l'},{id:'a'},{id:'s'}],meta:{source:'square'}};
const response=(status,payload=null,etag=null)=>({status,ok:status>=200&&status<300,
 headers:{get:name=>name==='ETag'?etag:null},json:async()=>payload});

test('catalog automatically recovers from initial failure and reuses a later 304 response',async()=>{
 const original=globalThis.fetch,calls=[];
 const queue=[response(503),response(200,catalog,'W/"v1"'),response(304)];
 globalThis.fetch=async(url,options)=>{calls.push({url,options});return queue.shift();};
 try{
  const {loadCatalog}=await import('../app/static/v2/flavor-explorer/data-source.mjs?initial-recovery');
  await assert.rejects(loadCatalog(),/ask our team/);
  assert.equal(await loadCatalog(),catalog);
  assert.equal(await loadCatalog(),catalog);
  assert.equal(calls[2].options.headers['If-None-Match'],'W/"v1"');
  assert.equal(calls[2].options.credentials,'same-origin');
 }finally{globalThis.fetch=original;}
});

test('transient API failure does not destroy the last successful conditional response',async()=>{
 const original=globalThis.fetch;
 const queue=[response(200,catalog,'W/"v2"'),response(503),response(304)];
 globalThis.fetch=async()=>queue.shift();
 try{
  const {loadCatalog}=await import('../app/static/v2/flavor-explorer/data-source.mjs?existing-recovery');
  const first=await loadCatalog();
  await assert.rejects(loadCatalog());
  assert.equal(await loadCatalog(),first);
 }finally{globalThis.fetch=original;}
});

test('Square products use neutral missing-photo placeholders without invented packaging or brand art',()=>{
 for(const type of ['freebase','salt','disposable']){
  const svg=productArt({inventorySource:'square',id:'real-id',type,name:'Actual Product',brand:'Actual Brand',accent:'#abcd12'});
  assert.match(svg,/IMAGE COMING SOON/);
  assert.doesNotMatch(svg,/Actual Product|Actual Brand|FLAVOR SERIES|ILLUSTRATED|bottle|glass|cap/);
  assert.match(svg,/<svg\b/);
 }
});