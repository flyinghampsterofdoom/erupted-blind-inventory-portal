const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('app/static/v2/inventory-receipt.js','utf8');
const tick=async()=>{for(let i=0;i<200;i++)await Promise.resolve();};
function node(tag='div'){const n={tag,textContent:'',disabled:false,children:[],handlers:{},setAttribute(){},append(...children){for(const child of children){child.parent=this;this.children.push(child);}},addEventListener(name,fn){this.handlers[name]=fn;},querySelectorAll(tag){return this.children.filter(child=>child.tag===tag);},remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};return n;}
function setup(options={}){
 const host=node();host.dataset={principalId:'4',recordUuid:'count-a',storeId:'1',kind:'blind-count',recordId:'7'};
 const records=options.records||[{name:'a',value:{revision:1,generation:2,acknowledged:1,values:{V1:{front_qty:'12',back_qty:'34'}}}}];
 const removed=[];const state={revision:2,submitted:true,receipt:{id:7}};
 const window={eruptedDrafts:{open:async()=>{if(options.denied)throw Error('Sign in with same account');return {scope:{principal_uuid:'account-a',store_uuid:'store-a',record_uuid:'count-a'},state,records:async()=>records,labels:{V1:'Synthetic Freebase'},remove:r=>removed.push(r.name)};}},eruptedSessionStatus:async()=>({ok:true,json:async()=>({principal_id:options.otherAccount?9:4,store_id:1,principal_uuid:'account-a',store_uuid:'store-a'})})};
 let interval;const document={hidden:false,getElementById:()=>host,createElement:node,addEventListener(){}};
 vm.runInNewContext(source,{document,window,localStorage:{'inventory-draft:v1:4:1:blind-count:7:a':'ciphertext'},Object,String,Date,setInterval:fn=>{interval=fn;}});
 return {host,options,removed,click:async()=>{await host.children[0].handlers.click();await tick();},poll:async()=>{interval();await tick();}};
}
function text(n){return n.textContent+' '+n.children.map(text).join(' ');}
test('submitted receipt leaves unconfirmed device work intact until authorized review',async()=>{
 const app=setup();assert.equal(app.removed.length,0);await app.click();assert.equal(app.removed.length,0);assert.match(text(app.host),/Synthetic Freebase · Front\s+12/);assert.match(text(app.host),/cannot|will not be overwritten/);
});
test('a different account cannot decrypt or display a previous account’s device entries',async()=>{
 const app=setup({denied:true});await app.click();assert.equal(app.host.querySelectorAll('details').length,0);assert.equal(app.removed.length,0);assert.match(text(app.host),/same account/);
});
test('receipt review removes only proven acknowledgments and retains different unfinished work',async()=>{
 const app=setup({records:[{name:'confirmed',value:{pendingSubmission:{revision:2}}},{name:'other-tab',value:{revision:1,generation:2,acknowledged:1,values:{V1:{front_qty:'12',back_qty:'34'}}}}]});
 await app.click();assert.deepEqual(app.removed,['confirmed']);assert.equal(app.host.querySelectorAll('details').length,1);
});
test('reviewed quantities are hidden again when the browser account changes',async()=>{
 const app=setup();await app.click();assert.equal(app.host.querySelectorAll('details').length,1);app.options.otherAccount=true;await app.poll();assert.equal(app.host.querySelectorAll('details').length,0);assert.equal(app.removed.length,0);
});
