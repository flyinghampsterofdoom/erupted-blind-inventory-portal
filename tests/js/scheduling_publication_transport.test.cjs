// Run the real API helper and publication handler together, including retry guards.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const code=source.slice(source.indexOf('  async function api('),source.indexOf('  const historyDialog ='))+'\nthis.openPublication=openPublication;';
const nodes={},requests=[],renders=[];
const $=key=>nodes[key]??={value:'',hidden:false,listeners:{},addEventListener(k,f){this.listeners[k]=f},setAttribute(){},showModal(){this.open=true},close(){this.open=false}};
let phase=0;
const context={$,board:{actions:{publish:true},period:{id:3,version:4}},refreshRequired:false,writeInFlight:false,pendingMove:null,query:()=>'?start=2030-05-12',csrf:()=>'',AbortController,setTimeout,clearTimeout,
 window:{SchedulePresentation:{weekLabel:()=> 'May 12–18, 2030'}},clearError:()=>{},showError:(e,n)=>{n.textContent=e.message},announce:()=>{},render:b=>renders.push(b),
 fetch:async(path,options)=>{
   requests.push({path,method:options.method});
   if(phase++===0)throw new DOMException('Aborted','AbortError');
   if(phase===2)throw new TypeError('Offline during state check');
   return {ok:true,json:async()=>({published:true,message:'Schedule published successfully.',board:{period:{id:3,version:5,status:'PUBLISHED'}}})};
 }};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
 context.openPublication();const submit=$('[data-publish-form]').listeners.submit,event={preventDefault(){}};
 await submit(event);assert.equal(requests.length,2);assert.equal(context.refreshRequired,true);
 assert.match($('[data-publish-error]').textContent,/status could not be confirmed/);
 assert.equal($('[data-publish-submit]').textContent,'Check publication status');
 await submit(event);assert.equal(requests.length,3);assert.equal(requests[2].method,'GET');
 assert.equal(requests.filter(r=>r.method==='POST').length,1);
 assert.equal($('[data-publish-dialog]').open,false);assert.equal(renders[0].period.status,'PUBLISHED');assert.equal(context.refreshRequired,false);
 console.log('Real API + dialog: lost response, offline state check, retry checks state without another POST, immediate published board.');
})().catch(e=>{console.error(e);process.exitCode=1});
