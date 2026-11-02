const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const code=source.slice(source.indexOf('  const publishDialog ='),source.indexOf('  const historyDialog ='))+'\nthis.openPublication=openPublication;';
function node(){return {value:'',listeners:{},hidden:false,disabled:false,addEventListener(k,fn){this.listeners[k]=fn},setAttribute(k,v){this[k]=v},showModal(){this.open=true},close(){this.open=false}}}
function setup(){
 const nodes={},requests=[],renders=[],announcements=[]; const $=key=>nodes[key]??=node();
 const context={$,refreshRequired:false,board:{actions:{publish:true},period:{id:29,version:4}},window:{SchedulePresentation:{weekLabel:()=> 'December 20–26, 2026'}},clearError:n=>{if(n)n.hidden=true},showError:(e,n)=>{n.hidden=false;n.textContent=e.message},render:b=>renders.push(b),announce:s=>announcements.push(s),api:(path,method,payload)=>new Promise((resolve,reject)=>requests.push({path,method,payload,resolve,reject}))};
 vm.createContext(context);vm.runInContext(code,context);
 context.openPublication();
 return {context,$,requests,renders,announcements,submit:()=>$('[data-publish-form]').listeners.submit({preventDefault(){}})};
}
const success={message:'Schedule approved and published.',board:{period:{id:29,version:5,status:'PUBLISHED'}}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 let t=setup();assert.match(t.$('[data-publish-description]').textContent,/December 20–26, 2026/);
 let p=t.submit();await t.submit();assert.equal(t.requests.length,1);
 assert.equal(t.$('[data-publish-submit]').disabled,true);
 let prevented=false;t.$('[data-publish-dialog]').listeners.cancel({preventDefault(){prevented=true}});assert.equal(prevented,true);
 t.requests[0].resolve(success);await p;await t.submit();assert.equal(t.requests.length,1);
 assert.equal(t.$('[data-publish-dialog]').open,false);assert.equal(t.renders[0].period.status,'PUBLISHED');
 assert.equal(t.$('[data-publication-success]').textContent,success.message);assert.equal(t.announcements.at(-1),success.message);
 // Warning + approved PTO choices survive the transport unchanged.
 t=setup();t.$('[data-publish-override]').checked=true;t.$('[data-publish-pto]').checked=true;
 await t.submit();assert.equal(t.requests.length,0);assert.match(t.$('[data-publish-error]').textContent,/reason/);
 t.$('[data-publish-override-reason]').value='Owner accepts this PTO exception.';
 p=t.submit();assert.equal(t.requests[0].payload.override_pto,true);assert.equal(t.requests[0].payload.confirm_serious_warnings,true);
 t.requests[0].resolve(success);await p;assert.equal(t.$('[data-publish-dialog]').open,false);
 // Abort, broken JSON, server error and duplicate/stale response all reconcile.
 for(const failure of [new DOMException('Aborted','AbortError'),new SyntaxError('Broken JSON'),Object.assign(new Error('Server failed'),{status:500}),Object.assign(new Error('Version conflict'),{status:409})]){
   t=setup();p=t.submit();t.requests[0].reject(failure);await tick();
   assert.equal(t.requests[1].path,'/v2/scheduling/api/periods/29/publication-state');assert.equal(t.requests[1].method,'GET');
   t.requests[1].resolve({...success,published:true});await p;
   assert.equal(t.$('[data-publish-dialog]').open,false);assert.equal(t.renders.length,1);assert.equal(t.context.refreshRequired,false);
 }
 // Unavailable state never reports failure or success as established fact.
 t=setup();p=t.submit();t.requests[0].reject(new Error('offline'));await tick();t.requests[1].reject(new Error('offline'));await p;
 assert.equal(t.$('[data-publish-dialog]').open,true);assert.match(t.$('[data-publish-error]').textContent,/status could not be confirmed/);assert.equal(t.renders.length,0);assert.equal(t.context.refreshRequired,true);assert.equal(t.$('[data-publish-submit]').textContent,'Check publication status');
 t=setup();p=t.submit();t.requests[0].reject(new Error('offline'));await tick();t.requests[1].resolve({published:false});await p;
 assert.match(t.$('[data-publish-error]').textContent,/not published/);assert.equal(t.context.refreshRequired,true);
 // Definitive validation rejection is not masked by reconciliation.
 t=setup();p=t.submit();t.requests[0].reject(Object.assign(new Error('PTO confirmation required'),{status:422}));await p;
 assert.equal(t.requests.length,1);assert.match(t.$('[data-publish-error]').textContent,/PTO/);
 console.log('Publication dialog, immediate status, overrides, duplicate guard and uncertain outcome recovery passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
