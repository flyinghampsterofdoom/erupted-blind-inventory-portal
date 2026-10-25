const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),{test}=require('node:test');
const source=fs.readFileSync('app/static/v2/inventory-form-drafts.js','utf8');
const tick=async()=>{for(let i=0;i<300;i++)await Promise.resolve();};
const response=(data,status=200)=>({ok:status<300,status,redirected:false,json:async()=>data});
function element(props={}){const handlers={};return Object.assign({handlers,value:'',checked:false,type:'text',hidden:false,disabled:false,addEventListener:(n,f)=>{handlers[n]=f;},fire(n,extra={}){return handlers[n]?.({preventDefault(){},target:this,...extra});},dispatchEvent(){},append(){},getAttribute(){return null;}},props);}
async function setup(fetcher,options={}){
 let redirect=null,url=null,interval,uid=0,logoutCount=0,timerId=0;const timers=new Map();
 const name=element({name:'employee_name',value:'Counter'}),qty=element({name:'qty__1',value:'0'}),checkbox=element({name:'completed_task_ids',type:'checkbox',value:'1'}),context=element({value:'old'});
 const controls=[name,qty,checkbox],children=[],timeCell=element({dataset:{taskTimeFor:'1'},textContent:'-'});
 const kind=options.kind||(options.chore?'daily-chore-sheet':'non-sellable-stock-take');
 const form=element({reportValidity:()=>!options.invalid,dataset:{principalId:'4',recordUuid:'count-a',storeId:'1',kind,recordId:'7',autosaveStatusId:'status',draftRevision:'v0'},action:`/store/${kind}/7/save`,append:node=>children.push(node),querySelectorAll:selector=>selector==='[data-task-time-for]'?[timeCell]:selector==='input[name="completed_task_ids"]'?[checkbox]:controls,querySelector:()=>context});
 const status=element(),logout=element({submit:()=>{logoutCount++;}}),storage=options.storage||new Map();if(options.saved)storage.set('record',structuredClone(options.saved));
 const cache={recordName:'record',scope:{principal_uuid:'account-a',store_uuid:'store-a',record_uuid:'count-a'},headers:()=>({'X-Inventory-Draft':'true','X-Inventory-Identity':'count-a','X-Inventory-Principal':'account-a','X-Inventory-Store':'store-a'}),quarantineLegacy:async()=>{},state:options.state||{submitted:false,revision:'v0'},context:'new',records:async()=>[...storage].map(([key,value])=>({name:key,value})),adopt(){},save:async data=>{if(options.storageError)throw Error('quota');storage.set('record',structuredClone(data));},remove:record=>storage.delete(record.name),clear:async()=>storage.clear()};
 const document={addEventListener(){},cookie:'csrf_token=abc',hidden:false,querySelectorAll:()=>[form],querySelector:selector=>selector==='form[action="/logout"]'?logout:(options.chore?form:null),getElementById:()=>status,createElement:()=>element()};
 const window=element({confirm:()=>true,history:{replaceState:(_,__,next)=>{url=next;}},location:{assign:next=>{redirect=next;}},eruptedDrafts:{open:async()=>{if(options.keyError)throw Error('Sign in');return cache;}},eruptedSessionStatus:async()=>options.expired?response({},401):response({principal_id:options.otherAccount?9:4,store_id:options.otherStore?2:1,principal_uuid:'account-a',store_uuid:'store-a',working_context:'fresh'})});
 const calls=[];
 class BrowserFormData extends FormData {
   constructor(form){super();if(form)for(const c of controls)if(c.name&&!c.disabled&&(!['checkbox','radio'].includes(c.type)||c.checked))this.append(c.name,c.value);}
 }
 const sandbox={AbortController,document,window,Date,JSON,Object,String,Map,Error,FormData:BrowserFormData,Event,crypto:{randomUUID:()=>`op-${++uid}`},setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),setInterval:fn=>{interval=fn;},fetch:async(path,opts)=>{const call={path,...opts,fields:opts?.body?[...opts.body.entries()]:null};calls.push(call);return fetcher(path,opts);}};
 if(options.chore){
   const template=fs.readFileSync('app/templates/store_daily_chore_sheet.html','utf8').replaceAll('{{ sheet.id }}','7');
   for(const match of template.matchAll(/<script>([\s\S]*?)<\/script>/g))vm.runInNewContext(match[1],sandbox);
 }
 vm.runInNewContext(source,sandbox);
 await tick();
 return{form,name,qty,checkbox,timeCell,context,status,storage,calls,options,logout,children,timers,advance:async ms=>{const due=[...timers].filter(([,v])=>v.ms<=ms);for(const [id,v]of due){timers.delete(id);v.fn();}await tick();},edit:value=>{qty.value=value;form.fire('input');},save:async()=>{children[0].fire('click');await tick();},submit:async()=>{form.fire('submit',{submitter:{getAttribute:()=>`/store/${kind}/7/submit`}});await tick();},poll:async()=>{interval();await tick();},redirect:()=>redirect,url:()=>url,logoutCount:()=>logoutCount};
}
const saved=(revision='v1')=>response({submitted:false,revision});
test('stock-take save serializes fields without changing editability and refresh-safe record URL',async()=>{
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
 let tries=0;const app=await setup(path=>{if(path.endsWith('/state'))return response({submitted:tries>1,revision:'v2',receipt:{operation_id:'op-2',principal_id:4}});tries++;if(path.endsWith('/save'))return saved();throw Error('lost response');});app.edit('12');await app.submit();assert.equal(app.storage.get('record').pending.submit,true);assert.equal(app.qty.disabled,true);
 await app.save();assert.equal(tries,2);assert.equal(app.redirect(),'/store/drafts/non-sellable-stock-take/7/resume');
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

test('rendered chore page has one save owner and stays editable across successive checkbox changes',async()=>{
 const app=await setup(()=>response({submitted:false,revision:'v1',completed_at:{'1':'2026-10-25T04:06:00Z'}}),{chore:true});
 app.checkbox.checked=true;app.form.fire('input');app.checkbox.fire('change');await tick();await app.save();
 assert.equal(app.calls.length,1,'a checkbox must not trigger an additional legacy write');
 assert.equal(app.calls[0].headers['X-Inventory-Draft'],'true');
 assert.equal(app.timeCell.textContent,'9:06 PM');assert.equal(app.checkbox.disabled,false);
 app.checkbox.checked=false;app.form.fire('input');app.checkbox.fire('change');await tick();await app.save();
 assert.equal(app.calls.length,2);assert.equal(app.calls[1].headers['X-Draft-Version'],'v1');
 assert.equal(app.calls[1].fields.some(([key])=>key==='completed_task_ids'),false);
 assert.equal(app.checkbox.disabled,false);
});
test('chore completion display clears only after a server acknowledgment',async()=>{
 const app=await setup(()=>response({submitted:false,revision:'v1',completed_at:{'1':null}}),{chore:true});
 app.timeCell.textContent='9:06 PM';app.checkbox.checked=false;app.form.fire('input');await tick();
 assert.equal(app.timeCell.textContent,'9:06 PM');await app.save();assert.equal(app.timeCell.textContent,'-');
});

const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
for(const kind of ['change-box-count','daily-chore-sheet','non-sellable-stock-take']){
 test(`${kind}: edits stay enabled and immutable snapshots drain in revision order`,async()=>{
  const first=deferred();let n=0;
  const app=await setup(()=>++n===1?first.promise:saved('v2'),{kind});
  app.edit('12');await app.save();assert.equal(app.qty.disabled,false);assert.equal(app.name.disabled,false);
  app.edit('123');await app.save();assert.equal(app.calls.length,1);
  assert.equal(app.calls[0].fields.find(([k])=>k==='qty__1')[1],'12');
  first.resolve(saved('v1'));await tick();
  assert.equal(app.calls.length,2);assert.equal(app.calls[1].headers['X-Draft-Version'],'v1');
  assert.equal(app.calls[1].fields.find(([k])=>k==='qty__1')[1],'123');
  assert.equal(app.qty.value,'123');assert.equal(app.storage.get('record').acknowledged,2);
 });
 test(`${kind}: Submit queues behind pending draft and newer edits, once`,async()=>{
  const first=deferred();let drafts=0;
  const app=await setup((path,opts)=>{
   if(path.endsWith('/save'))return ++drafts===1?first.promise:saved('v2');
   return response({submitted:true,revision:'v3',receipt:{operation_id:opts.headers['X-Draft-Operation'],principal_id:4}});
  },{kind});
  app.edit('12');await app.save();app.edit('123');await app.submit();await app.submit();
  assert.equal(app.qty.disabled,true);assert.equal(app.calls.length,1);
  first.resolve(saved('v1'));await tick();
  assert.deepEqual(app.calls.map(c=>c.path.split('/').pop()),['save','save','submit']);
  assert.equal(app.calls[2].fields.find(([k])=>k==='qty__1')[1],'123');
  assert.equal(app.calls[2].headers['X-Draft-Version'],'v2');assert.equal(app.redirect(),`/store/drafts/${kind}/7/resume`);
 });
 test(`${kind}: a failed required save cancels Submit and retries the exact snapshot`,async()=>{
  const first=deferred();let attempts=0;
  const app=await setup(()=>++attempts===1?first.promise:saved('v1'),{kind});
  app.edit('12');await app.save();app.edit('123');await app.submit();first.resolve(Promise.reject(Error('offline')));await tick();
  assert.equal(app.calls.length,1);assert.match(app.status.textContent,/Submission stopped.*Save \/ Retry/);assert.equal(app.qty.disabled,false);
  await app.save();assert.equal(app.calls.length,3);
  assert.equal(app.calls[0].headers['X-Draft-Operation'],app.calls[1].headers['X-Draft-Operation']);
  assert.deepEqual(app.calls[0].fields,app.calls[1].fields);assert.equal(app.calls.some(c=>c.path.endsWith('/submit')),false);
  assert.equal(app.calls[2].fields.find(([k])=>k==='qty__1')[1],'123');
 });
 test(`${kind}: conflict retains later edits and prevents queued submission`,async()=>{
  const first=deferred();const app=await setup(path=>path.endsWith('/state')?response({submitted:false}):first.promise,{kind});
  app.edit('12');await app.save();app.edit('123');await app.submit();first.resolve(response({detail:'Draft changed elsewhere'},409));await tick();
  assert.equal(app.storage.get('record').fields.find(([k])=>k==='qty__1')[1],'123');assert.equal(app.storage.get('record').needsReview,true);
  assert.equal(app.qty.disabled,true);assert.equal(app.calls.some(c=>c.path.endsWith('/submit')),false);
 });
 test(`${kind}: refresh restores newer edits and retries an older pending command first`,async()=>{
  const pending={action:`/store/${kind}/7/save`,submit:false,revision:'v0',operation_id:'original',generation:1,fields:[['employee_name','Counter'],['qty__1','12']]};
  const app=await setup(()=>saved('v1'),{kind,saved:{revision:'v0',generation:2,acknowledged:0,pending,fields:[['employee_name','Counter'],['qty__1','123']]}});
  await app.advance(0);assert.equal(app.calls.length,2);assert.equal(app.calls[0].headers['X-Draft-Operation'],'original');
  assert.equal(app.calls[0].fields.find(([k])=>k==='qty__1')[1],'12');assert.equal(app.calls[1].fields.find(([k])=>k==='qty__1')[1],'123');assert.equal(app.qty.value,'123');
 });
 test(`${kind}: autosave debounce and request deadline preserve editability and retry identity`,async()=>{
  let attempts=0;const app=await setup(()=>++attempts===1?new Promise(()=>{}):saved(),{kind});
  app.edit('12');assert.equal(app.calls.length,0);await app.advance(800);assert.equal(app.calls.length,1);assert.equal(app.qty.disabled,false);
  await app.advance(20000);assert.match(app.status.textContent,/timed out/);assert.equal(app.qty.disabled,false);
  app.edit('123');await app.save();assert.equal(app.calls.length,3);assert.equal(app.calls[0].headers['X-Draft-Operation'],app.calls[1].headers['X-Draft-Operation']);assert.equal(app.qty.value,'123');
 });
}
test('invalid fields cannot queue final submission and validation errors leave fields correctable',async()=>{
 const app=await setup(path=>path.endsWith('/state')?response({submitted:false}):response({detail:'Name is required'},400),{invalid:true});
 app.edit('12');await app.submit();assert.equal(app.calls.length,0);app.options.invalid=false;await app.submit();
 assert.equal(app.calls.some(c=>c.path.endsWith('/submit')),false);assert.equal(app.qty.disabled,false);assert.equal(app.storage.get('record').pending,null);assert.match(app.status.textContent,/Name is required/);
});
test('explicit logout freezes its final snapshot, but a failed logout save unlocks for retry',async()=>{
 const first=deferred();let attempts=0;
 const app=await setup(()=>++attempts===1?first.promise:saved());app.edit('12');
 const completion=app.logout.fire('submit');await tick();assert.equal(app.qty.disabled,true);
 first.resolve(Promise.reject(Error('offline')));await completion;assert.equal(app.logoutCount(),0);assert.equal(app.qty.disabled,false);assert.equal(app.qty.value,'12');
 await app.logout.fire('submit');assert.equal(app.logoutCount(),1);assert.equal(app.qty.disabled,true);
});
test('background acknowledgment coalesces newer typing behind the 800 ms debounce',async()=>{
 const first=deferred();let attempts=0;const app=await setup(()=>++attempts===1?first.promise:saved('v2'));
 app.edit('1');await app.advance(800);app.edit('12');first.resolve(saved('v1'));await tick();
 assert.equal(app.calls.length,1);assert.equal(app.qty.disabled,false);assert.notEqual(app.storage.get('record').generation,app.storage.get('record').acknowledged);
 app.edit('123');await app.advance(800);assert.equal(app.calls.length,2);assert.equal(app.calls[1].fields.find(([k])=>k==='qty__1')[1],'123');
 assert.equal(app.calls[1].headers['X-Draft-Version'],'v1');
});
