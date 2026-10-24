// Controller integration with deterministic DOM/network/timers. Crypto storage is tested separately.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const {test}=require('node:test');
const source=fs.readFileSync('app/static/v2/blind-count.js','utf8');
const tick=async()=>{for(let i=0;i<300;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return{promise,resolve,reject};};
const response=(data,status=200)=>({ok:status<300,status,redirected:false,json:async()=>data});
function element(props={}) {const handlers={};return Object.assign({handlers,value:'',textContent:'',hidden:true,disabled:false,validity:{valid:true},addEventListener(n,f){handlers[n]=f;},fire(n){return handlers[n]?.({preventDefault(){},target:this});}},props);}
async function setup(fetcher,options={}) {
 const front=element({dataset:{field:'front_qty'}}),back=element({dataset:{field:'back_qty'}}),total=element();
 const row=element({dataset:{variationId:'V1'},querySelectorAll:()=>[front,back],querySelector:()=>total});
 const form=element({dataset:{sessionId:'7',storeId:'1',principalId:'4',recordUuid:'count-a',revision:'0',workingContext:'old'},querySelectorAll:s=>s==='[data-field]'?[front,back]:[row]});
 let loggedOut=0,redirect=null,uid=0,interval;
 const logout=element({submit(){loggedOut++;}}),status=element(),login=element(),recover=element(),save=element();
 const els={'blind-count-form':form,'count-save-status':status,'count-login':login,'count-recover':recover,'count-save':save};
 const storage=options.storage||new Map();if(options.saved)storage.set('record',structuredClone(options.saved));
 const legacy=new Map(),calls=[],timers=new Map();let timerId=0;
 const document=element({hidden:false,cookie:'csrf_token=abc',getElementById:id=>els[id],querySelector:()=>logout});
 const window=element({confirm:()=>true,location:{assign:url=>{redirect=url;}}});
 const vault={recordName:'record',scope:{principal_uuid:'account-a',store_uuid:'store-a',record_uuid:'count-a'},headers:()=>({'X-Inventory-Draft':'true','X-Inventory-Identity':'count-a','X-Inventory-Principal':'account-a','X-Inventory-Store':'store-a'}),quarantineLegacy:async()=>{},state:options.state||{revision:0,submitted:false},context:'current',records:async()=>[...storage].map(([name,value])=>({name,value})),adopt:()=>{},save:async value=>{if(options.storageError)throw Error('quota');storage.set('record',structuredClone(value));},flush:async()=>{},remove:record=>storage.delete(record.name),clear:async()=>storage.clear()};
 window.eruptedDrafts={open:async()=>{if(options.keyError)throw Error('Sign in again');return vault;}};
 vm.runInNewContext(source,{document,window,Date,Math,Number,String,Object,JSON,decodeURIComponent,crypto:{randomUUID:()=>`operation-${++uid}`},
  sessionStorage:{getItem:k=>legacy.get(k),setItem:(k,v)=>legacy.set(k,v),removeItem:k=>legacy.delete(k)},
  setTimeout:(fn,ms)=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),setInterval:fn=>{interval=fn;},
  fetch:async(url,opts)=>{calls.push({url,...opts,payload:opts?.body?JSON.parse(opts.body):null});return fetcher(url,opts);}});
 const edit=(f,b)=>{front.value=f;back.value=b;front.fire('input');};
 await tick();
 return {front,back,total,form,logout,status,login,recover,save,storage,calls,timers,document,edit,options,
  poll:async()=>{await interval();await tick();},submit:async()=>{form.fire('submit');await tick();},retry:async()=>{save.fire('click');await tick();},loggedOut:()=>loggedOut,redirect:()=>redirect};
}
const session=(extra={})=>response({principal_id:4,store_id:1,principal_uuid:'account-a',store_uuid:'store-a',working_context:'current',expires_at:new Date(Date.now()+300000).toISOString(),...extra});
const draft=(revision=1)=>response({revision,errors:{}});

test('same Longview scope revalidates a release-changed version without refresh',async()=>{
 const app=await setup(url=>url==='/session-status'?session():draft());app.edit('12','34');await app.retry();
 assert.equal(app.form.dataset.workingContext,'current');assert.equal(app.calls.find(c=>c.payload).headers['X-Working-Context'],'current');assert.equal(app.status.textContent,'Saved to server');
});
test('an older acknowledgment cannot mark a newer edit saved or delete its durable copy',async()=>{
 const first=deferred(),second=deferred();let posts=0;
 const app=await setup(url=>url==='/session-status'?session():(++posts===1?first.promise:second.promise));
 app.edit('2','3');await app.retry();app.edit('4','3');first.resolve(draft(1));await tick();
 assert.equal(posts,2);assert.equal(app.storage.get('record').values.V1.front_qty,'4');assert.notEqual(app.status.textContent,'Saved to server');
 second.resolve(draft(2));await tick();assert.equal(app.status.textContent,'Saved to server');assert.equal(app.total.textContent,'7');
});
test('lost draft response retries the same durable operation before newer values',async()=>{
 let posts=0;const app=await setup(url=>{if(url==='/session-status')return session();if(++posts===1)throw Error('offline');return draft(posts-1);});
 app.edit('1','0');await app.retry();app.edit('2','0');await app.retry();
 const writes=app.calls.filter(c=>c.payload);assert.equal(writes.length,3);assert.deepEqual(writes[0].payload,writes[1].payload);assert.equal(writes[2].payload.changes.V1.front_qty,'2');
});
test('authentication expiry encrypts work before hiding it and same-account sign-in restores it',async()=>{
 let expired=true;const app=await setup(url=>url==='/session-status'?(expired?response({},401):session()):draft());
 app.edit('12','34');await app.retry();assert.equal(app.calls.filter(c=>c.payload).length,0);assert.equal(app.front.value,'');assert.equal(app.front.disabled,true);
 assert.equal(app.storage.get('record').values.V1.front_qty,'12');expired=false;app.document.cookie='csrf_token=fresh';await app.retry();
 assert.equal(app.front.value,'12');assert.equal(app.calls.find(c=>c.payload).headers['X-CSRF-Token'],'fresh');assert.equal(app.status.textContent,'Saved to server');
});
test('reassignment retains original-store work, hides quantities, and sends no write',async()=>{
 const app=await setup(()=>session({store_id:2}));app.edit('12','34');await app.retry();assert.equal(app.front.value,'');assert.equal(app.storage.get('record').values.V1.front_qty,'12');assert.equal(app.calls.filter(c=>c.payload).length,0);
});
test('another signed-in account cannot restore or submit the original draft',async()=>{
 const app=await setup(()=>session({principal_id:9}));app.edit('12','34');await app.retry();assert.equal(app.front.value,'');assert.equal(app.calls.filter(c=>c.payload).length,0);
 app.options.keyError=true;await app.retry();assert.equal(app.front.value,'');assert.equal(app.storage.get('record').values.V1.front_qty,'12');
});
test('expired authentication before restoration never overwrites the recovery record',async()=>{
 const saved={revision:0,generation:1,acknowledged:0,values:{V1:{front_qty:'12',back_qty:'34'}}};
 const app=await setup(()=>response({},401),{saved,keyError:true});await app.poll();assert.equal(app.storage.get('record').values.V1.front_qty,'12');assert.equal(app.front.value,'');
});
test('refresh and new tab restore protected work automatically with no 24-hour cutoff',async()=>{
 const app=await setup(url=>url==='/session-status'?session():draft(),{saved:{savedAt:Date.now()-86400001,revision:0,generation:1,acknowledged:0,values:{V1:{front_qty:'12',back_qty:'34'}}}});
 assert.equal(app.front.value,'12');assert.equal(app.total.textContent,'46');assert.equal(app.status.textContent,'Saved to server');
});
test('a server revision conflict retains all work until explicit reconciliation',async()=>{
 const app=await setup(()=>session(),{state:{revision:2,submitted:false},saved:{revision:1,generation:1,values:{V1:{front_qty:'12',back_qty:'34'}}}});
 assert.equal(app.front.value,'');assert.equal(app.front.disabled,true);assert.equal(app.recover.hidden,false);assert.equal(app.calls.length,0);assert.equal(app.storage.get('record').values.V1.front_qty,'12');
});
test('concurrent pending tabs require review instead of silently choosing a winner',async()=>{
 const storage=new Map([['a',{revision:0,values:{V1:{front_qty:'1',back_qty:'0'}}}],['b',{revision:0,values:{V1:{front_qty:'2',back_qty:'0'}}}]]);
 const app=await setup(()=>session(),{storage});assert.equal(app.front.disabled,true);assert.equal(app.recover.hidden,false);assert.equal(storage.size,2);
});
test('storage failure stops further entry without discarding the displayed values',async()=>{
 const app=await setup(()=>session());app.options.storageError=true;app.edit('12','34');await tick();assert.equal(app.front.disabled,true);assert.equal(app.front.value,'12');assert.match(app.status.textContent,/Could not protect/);
 app.options.storageError=false;await app.retry();assert.equal(app.storage.get('record').values.V1.front_qty,'12');
});
test('logout waits for acknowledgment and cannot proceed when persistence fails',async()=>{
 const ack=deferred();const app=await setup(url=>url==='/session-status'?session():ack.promise);app.edit('12','34');app.logout.fire('submit');await tick();assert.equal(app.loggedOut(),0);
 ack.resolve(draft());await tick();assert.equal(app.loggedOut(),1);
});
test('lost final response survives refresh and resolves the receipt without another draft',async()=>{
 const storage=new Map();let submits=0;
 const app=await setup(url=>{if(url==='/session-status')return session();if(url.endsWith('/draft'))return draft();if(url.endsWith('/state'))return response({submitted:false});submits++;throw Error('lost final response');},{storage});
 app.edit('12','34');await app.submit();assert.equal(submits,1);assert.equal(app.storage.get('record').pendingSubmission.revision,1);assert.equal(app.front.disabled,true);
 const refreshed=await setup(()=>{throw Error('no further requests needed');},{storage,state:{revision:1,submitted:true,receipt:{id:7}}});
 assert.equal(refreshed.redirect(),'/store/sessions/7');assert.equal(storage.size,0);
});
test('double-click submission sends one command and preserves it until confirmed',async()=>{
 const ack=deferred();let submits=0;
 const app=await setup(url=>{if(url==='/session-status')return session();if(url.endsWith('/draft'))return draft();submits++;return ack.promise;});
 app.edit('12','34');await app.submit();await app.submit();assert.equal(submits,1);assert.equal(app.storage.get('record').pendingSubmission.revision,1);
 ack.resolve(response({submitted:true,revision:1,receipt:{id:7}}));await tick();assert.equal(app.redirect(),'/store/sessions/7');
});
test('blank values are preserved explicitly and incomplete totals remain blank',async()=>{
 const app=await setup(url=>url==='/session-status'?session():draft());app.edit('','0');await app.retry();assert.equal(app.total.textContent,'—');assert.equal(app.calls.find(c=>c.payload).payload.changes.V1.front_qty,'');
});
test('idle and hidden polling never renew authentication or rewrite recovery storage',async()=>{
 const app=await setup(()=>session());app.document.hidden=true;await app.poll();assert.equal(app.calls.length,0);app.document.hidden=false;await app.poll();assert.equal(app.calls.length,1);assert.equal(app.calls[0].url,'/session-status');assert.equal(app.storage.size,0);
});
test('another counter submitting a different revision never clears this tab’s unfinished work',async()=>{
 const app=await setup(()=>session(),{state:{revision:2,submitted:true,receipt:{id:7}},saved:{revision:1,generation:2,acknowledged:1,pendingSubmission:{revision:1},values:{V1:{front_qty:'12',back_qty:'34'}}}});
 assert.equal(app.redirect(),null);assert.equal(app.storage.size,1);assert.equal(app.front.disabled,true);assert.match(app.status.textContent,/additional protected entries/);
});
test('a pending revision conflict remains a review step after refresh',async()=>{
 const app=await setup(()=>{throw Error('must not retry automatically');},{state:{revision:2,submitted:false},saved:{revision:1,generation:2,acknowledged:1,needsReview:true,pending:{generation:2,payload:{revision:1}},values:{V1:{front_qty:'12',back_qty:'34'}}}});
 assert.equal(app.calls.length,0);assert.equal(app.recover.hidden,false);assert.equal(app.storage.size,1);
});
test('quota failure during authentication expiry hides values and retains the last edit privately for retry',async()=>{
 let expired=false;const app=await setup(url=>url==='/session-status'?(expired?response({},401):session()):draft());
 app.options.storageError=true;app.edit('12','34');await tick();expired=true;await app.retry();
 assert.equal(app.front.value,'');assert.equal(app.front.disabled,true);assert.match(app.status.textContent,/keep this tab open/);
 expired=false;app.options.storageError=false;await app.retry();assert.equal(app.front.value,'12');assert.equal(app.calls.find(c=>c.payload).payload.changes.V1.back_qty,'34');
});
test('a change between HTML rendering and key fetch cannot silently overwrite newer server entries',async()=>{
 const app=await setup(()=>{throw Error('must reopen first');},{state:{revision:1,submitted:false}});
 assert.equal(app.front.disabled,true);assert.equal(app.calls.length,0);assert.match(app.status.textContent,/changed while opening/);
});

test('reused numeric account ID with a different immutable identity hides work and sends no write',async()=>{
 const app=await setup(()=>session({principal_uuid:'account-recreated'}));app.edit('12','34');await app.retry();
 assert.equal(app.front.value,'');assert.equal(app.calls.filter(c=>c.payload).length,0);assert.equal(app.storage.get('record').values.V1.front_qty,'12');
});
test('every protected command carries original immutable count, account and store identities',async()=>{
 const app=await setup(url=>url==='/session-status'?session():draft());app.edit('12','34');await app.retry();
 const headers=app.calls.find(c=>c.payload).headers;assert.equal(headers['X-Inventory-Identity'],'count-a');assert.equal(headers['X-Inventory-Principal'],'account-a');assert.equal(headers['X-Inventory-Store'],'store-a');
});
