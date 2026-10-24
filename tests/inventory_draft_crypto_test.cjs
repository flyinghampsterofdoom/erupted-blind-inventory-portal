const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {webcrypto}=require('node:crypto');
const source=fs.readFileSync('app/static/v2/inventory-drafts.js','utf8');
const scope={principal_id:4,store_id:1,kind:'blind-count',record_id:7};
const key=Buffer.alloc(32,42).toString('base64');
const response=(data,status=200)=>({ok:status<300,status,redirected:false,json:async()=>data});
function storage(){const target={};return new Proxy(target,{get(o,k){if(k==='getItem')return name=>o[name]??null;if(k==='setItem')return(name,value)=>{o[name]=value;};if(k==='removeItem')return name=>{delete o[name];};return o[k];}});}
function environment(localStorage,fetcher=()=>response({scope,key,state:{submitted:false,revision:0},working_context:'current'})){
 const window={};vm.runInNewContext(source,{window,localStorage,fetch:fetcher,document:{getElementById:()=>null},crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Date,JSON,Object,Error,btoa:v=>Buffer.from(v,'binary').toString('base64'),atob:v=>Buffer.from(v,'base64').toString('binary')});return window.eruptedDrafts;
}
test('encrypted draft survives a new page/browser session without persisting the key or plaintext',async()=>{
 const s=storage(),api=environment(s),first=await api.open('/key',scope);
 await first.save({revision:0,values:{V1:{front_qty:'12345',back_qty:'98765'}}});
 const raw=JSON.stringify(s);assert.equal(raw.includes('12345'),false);assert.equal(raw.includes('98765'),false);assert.equal(raw.includes(key),false);
 const second=await environment(s).open('/key',scope),records=await second.records();assert.equal(records[0].value.values.V1.front_qty,'12345');
});
test('authorization failure does not delete or rewrite encrypted recovery',async()=>{
 const s=storage(),first=await environment(s).open('/key',scope);await first.save({values:{V1:{front_qty:'12'}}});const before=JSON.stringify(s);
 await assert.rejects(environment(s,()=>response({},401)).open('/key',scope),/Sign in/);assert.equal(JSON.stringify(s),before);
});
test('another account cannot open the original scope even if it has same-store access',async()=>{
 const s=storage();await assert.rejects(environment(s,()=>response({scope:{...scope,principal_id:9},key})).open('/key',scope),/same account/);
});
test('different keys cannot decrypt the original account draft; ciphertext remains intact',async()=>{
 const s=storage(),first=await environment(s).open('/key',scope);await first.save({values:{V1:{front_qty:'12'}}});const before=JSON.stringify(s);
 const wrong=await environment(s,()=>response({scope,key:Buffer.alloc(32,43).toString('base64')})).open('/key',scope);
 await assert.rejects(wrong.records(),/retained/);assert.equal(JSON.stringify(s),before);
});
test('corrupt/tampered drafts are retained and cannot be silently overwritten',async()=>{
 const s=storage(),first=await environment(s).open('/key',scope);await first.save({values:{V1:{front_qty:'12'}}});s[Object.keys(s)[0]]='tampered';
 await assert.rejects(first.records(),/retained/);assert.equal(Object.values(s)[0],'tampered');
});
test('serialized writes cannot leave an older edit in durable storage',async()=>{
 const s=storage(),first=await environment(s).open('/key',scope);await Promise.all([first.save({values:{V1:{front_qty:'1'}}}),first.save({values:{V1:{front_qty:'2'}}})]);assert.equal((await first.records())[0].value.values.V1.front_qty,'2');
});
test('claiming a recovered draft writes replacement before removing only the adopted snapshot',async()=>{
 const s=storage(),a=await environment(s).open('/key',scope);await a.save({values:{V1:{front_qty:'1'}}});
 const b=await environment(s).open('/key',scope),record=(await b.records())[0];b.adopt(record);await b.save(record.value);assert.equal(Object.keys(s).length,1);
 const c=await environment(s).open('/key',scope);await c.save({values:{V1:{front_qty:'2'}}});assert.equal(Object.keys(s).length,2);
});
test('storage denial stops initialization instead of allowing plaintext or unprotected entry',async()=>{
 const s=storage();s.setItem=undefined;const bad=new Proxy(s,{get(o,k){if(k==='setItem')return()=>{throw Error('quota');};return o[k];}});
 await assert.rejects(environment(bad).open('/key',scope),/quota/);
});
test('large inventory drafts can be encrypted and restored without argument-size overflow',async()=>{
 const s=storage(),a=await environment(s).open('/key',scope);await a.save({values:'x'.repeat(400000)});assert.equal((await a.records())[0].value.values.length,400000);
});
test('confirming one tab’s submission never deletes another tab’s pending work',async()=>{
 const s=storage(),a=await environment(s).open('/key',scope),b=await environment(s).open('/key',scope);
 await a.save({values:{V1:{front_qty:'1'}}});await b.save({values:{V1:{front_qty:'2'}}});await a.clear();
 assert.equal(Object.keys(s).length,1);assert.equal((await b.records())[0].value.values.V1.front_qty,'2');
});
