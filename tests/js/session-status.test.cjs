const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
test('session probes share only the in-flight fetch and clone responses',async()=>{
  let resolve,count=0;
  const context={window:{},fetch:()=>{count++;return new Promise(r=>{resolve=r;});}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../app/static/v2/session-status.js'),'utf8'),context);
  const one=context.window.eruptedSessionStatus(),two=context.window.eruptedSessionStatus();
  assert.equal(count,1);
  resolve({clone:()=>({ok:true,json:async()=>({principal_id:1})})});
  assert.notEqual(await one,await two);
  const three=context.window.eruptedSessionStatus();assert.equal(count,2);
  resolve({clone:()=>({ok:true})});await three;
});
