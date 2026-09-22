// Browser-controller behavior with deterministic DOM, network, and timers.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync('app/static/v2/blind-count.js','utf8');
const tick = async () => { for(let i=0;i<100;i++) await Promise.resolve(); };
const deferred = () => {let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject};};
const response = (data,status=200) => ({ok:status<300,status,redirected:false,json:async()=>data});
function element(props={}) {
  const handlers={};return Object.assign({handlers,value:'',textContent:'',hidden:true,disabled:false,validity:{valid:true},
    addEventListener(name,fn){handlers[name]=fn;},
    fire(name){return handlers[name]?.({preventDefault(){},target:this});}},props);
}
function setup(fetcher, saved=null) {
  const front=element({dataset:{field:'front_qty'}}),back=element({dataset:{field:'back_qty'}}),total=element();
  const row=element({dataset:{variationId:'V1'},querySelectorAll:()=>[front,back],querySelector:()=>total});
  const form=element({dataset:{sessionId:'7',storeId:'1',principalId:'4',revision:'0'},querySelectorAll:s=>s==='[data-field]'?[front,back]:[row]});
  let loggedOut=0,redirect=null,uid=0;
  const logout=element({submit(){loggedOut++;}}),status=element(),login=element(),recover=element(),save=element();
  const els={'blind-count-form':form,'count-save-status':status,'count-login':login,'count-recover':recover,'count-save':save};
  const storage=new Map(saved?[[ 'blind-count:4:1:7',JSON.stringify(saved) ]]:[]);
  const calls=[];const timers=new Map();let timerId=0;let interval;
  const document=element({cookie:'csrf_token=abc',getElementById:id=>els[id],querySelector:()=>logout});
  const window=element({confirm:()=>true,location:{assign:url=>{redirect=url;}}});
  vm.runInNewContext(source,{document,window,Date,Math,Number,String,Object,JSON,decodeURIComponent,
    crypto:{randomUUID:()=>`operation-${++uid}`},
    sessionStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k),removeItem:k=>storage.delete(k)},
    setTimeout:(fn,ms)=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),setInterval:fn=>{interval=fn;},
    fetch:async(url,options)=>{calls.push({url,...options,payload:options?.body?JSON.parse(options.body):null});return fetcher(url,options);}});
  const edit=(f,b)=>{front.value=f;back.value=b;front.fire('input');};
  return {front,back,total,form,logout,status,login,recover,save,storage,calls,timers,document,edit,
    poll:()=>interval(),loggedOut:()=>loggedOut,redirect:()=>redirect};
}
const session=()=>response({principal_id:4,store_id:1,expires_at:new Date(Date.now()+300000).toISOString()});

test('an older acknowledgment cannot mark a newer edit saved', async()=>{
  const first=deferred(),second=deferred();let posts=0;
  const app=setup(url=>url==='/session-status'?session():(++posts===1?first.promise:second.promise));
  app.edit('2','3');app.save.fire('click');await tick();
  app.edit('4','3');first.resolve(response({revision:1,errors:{}}));await tick();
  assert.equal(posts,2);assert.equal(app.status.textContent,'Saving…');assert.equal(app.storage.size,1);
  const requests=app.calls.filter(c=>c.payload);assert.equal(requests[1].payload.revision,1);assert.equal(requests[1].payload.changes.V1.front_qty,'4');
  second.resolve(response({revision:2,errors:{}}));await tick();assert.equal(app.status.textContent,'Saved');assert.equal(app.storage.size,0);assert.equal(app.total.textContent,'7');
});
test('a lost save response retries the exact operation and then newer edits',async()=>{
  let attempts=0;
  const app=setup((url,options)=>{if(url==='/session-status')return session();if(++attempts===1)throw Error('offline');return response({revision:attempts-1,errors:{}});});
  app.edit('1','0');app.save.fire('click');await tick();
  app.edit('2','0');app.save.fire('click');await tick();
  const posts=app.calls.filter(c=>c.payload);assert.equal(posts.length,3);
  assert.deepEqual(posts[1].payload,posts[0].payload);assert.equal(posts[2].payload.changes.V1.front_qty,'2');assert.equal(app.status.textContent,'Saved');
});
test('logout waits for the newest server acknowledgment',async()=>{
  const saved=deferred();const app=setup(url=>url==='/session-status'?session():saved.promise);
  app.edit('0','0');const finish=app.logout.fire('submit');await tick();assert.equal(app.loggedOut(),0);assert.equal(app.front.disabled,true);
  saved.resolve(response({revision:1,errors:{}}));await finish;assert.equal(app.loggedOut(),1);
});
test('failed persistence pauses logout and preserves pending values',async()=>{
  const app=setup(url=>{if(url==='/session-status')return session();throw Error('offline');});
  app.edit('3','4');await app.logout.fire('submit');assert.equal(app.loggedOut(),0);assert.equal(app.storage.size,1);assert.equal(app.front.value,'3');assert.match(app.status.textContent,/Logout paused/);
});
test('expired or changed accounts cannot save; same-account reauthentication resumes',async()=>{
  let auth='expired';const app=setup(url=>url==='/session-status'?(auth==='expired'?response({},401):auth==='other'?response({principal_id:9}):session()):response({revision:1,errors:{}}));
  app.edit('4','5');app.save.fire('click');await tick();assert.equal(app.calls.filter(c=>c.payload).length,0);assert.equal(app.login.hidden,false);assert.equal(app.storage.size,1);
  auth='other';app.save.fire('click');await tick();assert.equal(app.calls.filter(c=>c.payload).length,0);
  auth='same';app.document.cookie='csrf_token=new-session';app.save.fire('click');await tick();
  assert.equal(app.calls.filter(c=>c.payload)[0].headers['X-CSRF-Token'],'new-session');assert.equal(app.status.textContent,'Saved');
});
test('incomplete rows stay blank and a clear is transmitted explicitly',async()=>{
  const app=setup(url=>url==='/session-status'?session():response({revision:1,errors:{}}));
  app.edit('','0');assert.equal(app.total.textContent,'—');app.save.fire('click');await tick();assert.equal(app.calls.find(c=>c.payload).payload.changes.V1.front_qty,'');
});
test('recovery is account/store/round scoped and requires explicit restoration',async()=>{
  const app=setup(url=>url==='/session-status'?session():response({revision:1,errors:{}}),{savedAt:Date.now(),values:{V1:{front_qty:'6',back_qty:'7'}}});
  assert.equal(app.front.value,'');assert.equal(app.recover.hidden,false);app.recover.fire('click');assert.equal(app.front.value,'6');assert.equal(app.total.textContent,'13');
});
test('polling uses only the nonrenewing session endpoint while inactive',async()=>{
  const app=setup(()=>session());await app.poll();assert.equal(app.calls.length,1);assert.equal(app.calls[0].url,'/session-status');assert.equal(app.calls[0].method,undefined);
});
test('a revision conflict retains the buffer and prevents further writes',async()=>{
  const app=setup(url=>url==='/session-status'?session():response({error:'Changed'},409));app.edit('1','2');app.save.fire('click');await tick();app.save.fire('click');await tick();assert.equal(app.calls.filter(c=>c.payload).length,1);assert.equal(app.storage.size,1);assert.match(app.status.textContent,/changed elsewhere/);
});
test('lost final-submit response retries submission without another draft save',async()=>{
  let submissions=0;
  const app=setup(url=>{
    if(url==='/session-status')return session();
    if(url.endsWith('/draft'))return response({revision:1,errors:{}});
    if(++submissions===1)throw Error('response lost');
    return response({submitted:true,redirect:'/store/daily-count'});
  });
  app.edit('3','4');await app.form.fire('submit');
  assert.equal(app.front.disabled,true);assert.equal(app.redirect(),null);assert.match(app.status.textContent,/Submission unconfirmed/);
  await app.form.fire('submit');assert.equal(app.redirect(),'/store/daily-count');
  assert.equal(app.calls.filter(c=>c.url.endsWith('/draft')).length,1);assert.equal(submissions,2);
});
