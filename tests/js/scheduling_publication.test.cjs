const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const code=source.slice(source.indexOf('  const publishDialog ='),source.indexOf('  const historyDialog ='))+'\nthis.openPublication=openPublication;';
function node(){return {listeners:{},hidden:false,disabled:false,addEventListener(k,fn){this.listeners[k]=fn},setAttribute(k,v){this[k]=v},showModal(){this.open=true},close(){this.open=false}}}
const nodes={};const $=key=>nodes[key]??=node();
let calls=0,resolve,reject,reloads=0; const payloads=[];
const context={$,board:{actions:{publish:true},period:{id:29,version:4}},window:{SchedulePresentation:{weekLabel:()=> 'December 20–26, 2026'}},clearError:n=>{n.hidden=true},showError:(e,n)=>{n.hidden=false;n.textContent=e.message},location:{reload(){reloads++}},api:(path,method,payload)=>{calls++;payloads.push(payload);assert.equal(path,'/v2/scheduling/api/periods/29/approve-and-publish');assert.equal(payload.expected_version,4);return new Promise((yes,no)=>{resolve=yes;reject=no})}};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
 context.openPublication();assert.match($('[data-publish-description]').textContent,/December 20–26, 2026/);
 const submit=$('[data-publish-form]').listeners.submit;
 const event={preventDefault(){}};
 const first=submit(event);await submit(event);assert.equal(calls,1);assert.equal($('[data-publish-submit]').disabled,true);
 let prevented=false;$('[data-publish-dialog]').listeners.cancel({preventDefault(){prevented=true}});assert.equal(prevented,true);
 reject(Object.assign(new Error('Schedule changed. Refresh and review.'),{status:409}));await first;
 assert.equal($('[data-publish-refresh]').hidden,false);assert.equal($('[data-publish-cancel]').disabled,false);assert.equal(reloads,0);
 assert.equal(payloads[0].confirm_serious_warnings,false);
 $('[data-publish-override]').checked=true; $('[data-publish-override-reason]').value='';
 await submit(event); assert.equal(calls,1);assert.match($('[data-publish-error]').textContent,/reason/);
 $('[data-publish-override-reason]').value='Owner accepts the holiday staffing exception.';
 const second=submit(event);assert.equal(payloads[1].confirm_serious_warnings,true);assert.equal(payloads[1].override_reason,'Owner accepts the holiday staffing exception.');resolve({});await second;await submit(event);assert.equal(calls,2);assert.equal(reloads,1);
 console.log('Confirmation date, duplicate submissions, pending cancel guard, stale-version error and successful reload passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
