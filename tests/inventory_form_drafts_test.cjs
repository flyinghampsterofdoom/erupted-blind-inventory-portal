const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),{test}=require('node:test');
const source=fs.readFileSync('app/static/v2/inventory-form-drafts.js','utf8');
const tick=async()=>{for(let i=0;i<300;i++)await Promise.resolve();};
const response=(data,status=200)=>({ok:status<300,status,redirected:false,json:async()=>data});
function element(props={}){const handlers={};return Object.assign({handlers,value:'',checked:false,type:'text',hidden:false,disabled:false,addEventListener:(n,f)=>{handlers[n]=f;},fire(n,extra={}){return handlers[n]?.({preventDefault(){},target:this,...extra});},dispatchEvent(){},append(){},getAttribute(){return null;}},props);}
async function setup(fetcher,options={}){
 let redirect=null,url=null,interval,uid=0,logoutCount=0;
 const name=element({name:'employee_name',value:'Counter'}),qty=element({name:'qty__1',value:'0'}),checkbox=element({name:'completed_task_ids',type:'checkbox',value:'1'}),context=element({value:'old'});
 const controls=[name,qty,checkbox],children=[];
 const form=element({dataset:{principalId:'4',recordUuid:'count-a',storeId:'1',kind:'non-sellable-stock-take',recordId:'7',autosaveStatusId:'status',draftRevision:'v0'},action:'/store/non-sellable-stock-take/7/save',append:node=>children.push(node),querySelectorAll:()=>controls,querySelector:()=>context});
 const status=element(),logout=element({submit:()=>{logoutCount++;}}),storage=options.storage||new Map();if(options.saved)storage.set('record',structuredClone(options.saved));
 const cache={recordName:'record',scope:{principal_uuid:'account-a',store_uuid:'store-a',record_uuid:'count-a'},headers:()=>({'X-Inventory-Draft':'true','X-Inventory-Identity':'count-a','X-Inventory-Principal':'account-a','X-Inventory-Store':'store-a'}),quarantineLegacy:async()=>{},state:options.state||{submitted:false,revision:'v0'},context:'new',records:async()=>[...storage].map(([key,value])=>({name:key,value})),adopt(){},save:async data=>{if(options.storageError)throw Error('quota');storage.set('record',structuredClone(data));},remove:record=>storage.delete(record.name),clear:async()=>storage.clear()};
 const document={addEventListener(){},cookie:'csrf_token=abc',hidden:false,querySelectorAll:()=>[form],querySelector:()=>logout,getElementById:()=>status,createElement:()=>element()};
 const window=element({confirm:()=>true,history:{replaceState:(_,__,next)=>{url=next;}},location:{assign:next=>{redirect=next;}},eruptedDrafts:{open:async()=>{if(options.keyError)throw Error('Sign in');return cache;}},eruptedSessionStatus:async()=>options.expired?response({},401):response({principal_id:options.otherAccount?9:4,store_id:options.otherStore?2:1,principal_uuid:'account-a',store_uuid:'store-a',working_context:'fresh'})});
 const calls=[];
 vm.runInNewContext(source,{document,window,Date,JSON,Object,String,Map,Error,FormData,Event,crypto:{randomUUID:()=>`op-${++uid}`},setTimeout:()=>1,clearTimeout(){},setInterval:fn=>{interval=fn;},fetch:async(path,opts)=>{const call={path,...opts,fields:opts?.body?[...opts.body.entries()]:null};calls.push(call);return fetcher(path,opts);}});
 await tick();
 return{form,name,qty,checkbox,context,status,storage,calls,options,logout,children,edit:value=>{qty.value=value;form.fire('input');},save:async()=>{children[0].fire('click');await tick();},submit:async()=>{form.fire('submit',{submitter:{getAttribute:()=>'/store/non-sellable-stock-take/7/submit'}});await tick();},poll:async()=>{interval();await tick();},redirect:()=>redirect,url:()=>url,logoutCount:()=>logoutCount};
}
const saved=(revision='v1')=>response({submitted:false,revision});
test('stock-take save serializes disabled-during-request fields and refresh-safe record URL',async()=>{
 const app=await setup(()=>saved());app.edit('12');await app.save();assert.equal(app.url(),'/store/drafts/non-sellable-stock-take/7/resume');
 assert.equal(app.calls[0].fields.find(([name])=>name==='qty__1')[1],'12');assert.equal(app.calls[0].headers['X-Working-Context'],'fresh');assert.equal(app.calls[0].headers['X-Draft-Version'],'v0');assert.equal(app.status.textContent,'Saved to server');
});
test('expired auth retains all quantities then hides them without navigation',async()=>{
 const app=await setup(()=>saved());app.edit('12');app.options.expired=true;await app.save();assert.equal(app.calls.length,0);assert.equal(app.storage.get('record').fields.find(([key])=>key==='qty__1')[1],'12');assert.equal(app.qty.value,'');assert.equal(app.redirect(),null);
 app.options.expired=false;await app.save();assert.equal(app.qty.value,'12');assert.equal(app.calls.length,1);
});
test('actual reassignment sends no write and retains original-store ciphertext payload',async()=>{
 const app=await setup(()=>saved());app.edit('12');app.options.otherStore=true;await app.save();assert.equal(app.calls.length,0);assert.equal(app.qty.value,'');assert.equal(app.storage.size,1);
});
test('lost form-save acknowledgment retries exactly the same stable command',async()=>{
 let calls=0;const app=await setup(()=>{if(++calls===1)throw Error('offline');return saved();});app.edit('12');await app.save();await app.save();
 assert.equal(app.calls[0].headers['X-Draft-Operation'],app.calls[1].headers['X-Draft-Operation']);assert.deepEqual(app.calls[0].fields,app.calls[1].fields);
});
test('unchecked chores are represented as explicit absence while other fields remain intact',async()=>{
 const app=await setup(()=>saved());app.checkbox.checked=false;app.edit('12');await app.save();assert.equal(app.calls[0].fields.some(([key])=>key==='completed_task_ids'),false);assert.equal(app.calls[0].fields.find(([key])=>key==='employee_name')[1],'Counter');
});
test('protected form draft restores automatically after refresh',async()=>{
 const app=await setup(()=>saved(),{saved:{revision:'v0',generation:1,acknowledged:0,fields:[['employee_name','Counter'],['qty__1','12']]}});
 assert.equal(app.qty.value,'12');assert.equal(app.status.textContent,'Saved to server');
});
test('a committed receipt after interrupted submission never creates another draft or resubmits',async()=>{
 const app=await setup(()=>{throw Error('unexpected write');},{state:{submitted:true,revision:'v1',receipt:{id:7,operation_id:'op-known',principal_id:4}},saved:{revision:'v0',pending:{submit:true,operation_id:'op-known',fields:[['qty__1','12']]}}});
 assert.equal(app.calls.length,0);assert.equal(app.storage.size,0);assert.equal(app.redirect(),'/store/drafts/non-sellable-stock-take/7/resume');
});
test('submission intent persists before network failure and retry checks receipt first',async()=>{
 let tries=0;const app=await setup(path=>{if(path.endsWith('/state'))return response({submitted:true,revision:'v1',receipt:{operation_id:'op-1',principal_id:4}});tries++;throw Error('lost response');});app.edit('12');await app.submit();assert.equal(app.storage.get('record').pending.submit,true);assert.equal(app.qty.disabled,true);
 await app.save();assert.equal(tries,1);assert.equal(app.redirect(),'/store/drafts/non-sellable-stock-take/7/resume');
});
test('known conflicts survive refresh without automatically replaying an obsolete command',async()=>{
 const app=await setup(()=>{throw Error('must review first');},{saved:{revision:'v0',generation:1,needsReview:true,pending:{fields:[['qty__1','12']]},fields:[['qty__1','12']]},state:{submitted:false,revision:'v1'}});
 assert.equal(app.calls.length,0);assert.equal(app.qty.disabled,true);assert.equal(app.storage.size,1);
});
test('storage failure retains displayed values and blocks further edits',async()=>{
 const app=await setup(()=>saved());app.options.storageError=true;app.edit('12');await tick();assert.equal(app.qty.value,'12');assert.equal(app.qty.disabled,true);assert.match(app.status.textContent,/Could not protect/);
});
test('another counter’s receipt cannot discard a different protected stock-take command',async()=>{
 const app=await setup(()=>{throw Error('no write permitted');},{state:{submitted:true,revision:'v2',receipt:{id:7,operation_id:'other-op',principal_id:9}},saved:{revision:'v0',generation:1,pending:{submit:true,operation_id:'our-op'},fields:[['qty__1','12']]}});
 assert.equal(app.redirect(),null);assert.equal(app.storage.size,1);assert.equal(app.qty.disabled,true);assert.match(app.status.textContent,/additional protected entries/);
});
test('quota plus expiry retains the last stock-take edit privately until same-account retry',async()=>{
 const app=await setup(()=>saved());app.options.storageError=true;app.edit('12');await tick();app.options.expired=true;await app.save();
 assert.equal(app.qty.value,'');assert.match(app.status.textContent,/keep this tab open/);
 app.options.storageError=false;app.options.expired=false;await app.save();assert.equal(app.qty.value,'12');
 assert.equal(app.calls[0].fields.find(([key])=>key==='qty__1')[1],'12');
});
test('a form changed between rendering and authorization requires fresh server values before entry',async()=>{
 const app=await setup(()=>{throw Error('must reopen first');},{state:{revision:'v1',submitted:false}});
 assert.equal(app.qty.disabled,true);assert.equal(app.calls.length,0);assert.match(app.status.textContent,/changed while opening/);
});
