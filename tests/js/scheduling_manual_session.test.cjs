const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const helper=source.slice(source.indexOf('  let manualAcknowledged'),source.indexOf('  function confirmOperationalException'));
function setup(already=false,otherTab=false){
 let shown=0,calls=[],fail=false;const submit={disabled:false},error={hidden:true};
 const form={querySelector:()=>submit},dialog={showModal(){shown++;},close(){this.onclose?.();}},cancel={};
 const c={root:{dataset:{manualAcknowledged:String(already)}},board:{period:{generated_origin:true}},
  $:sel=>({'[data-manual-edit-dialog]':dialog,'[data-manual-edit-form]':form,'[data-manual-edit-error]':error,'[data-manual-edit-cancel]':cancel}[sel]),
  csrf:()=>'',decodeURIComponent,Promise,Error,
  fetch:async(path,options)=>{calls.push(options?.method||'GET');return {ok:!fail,json:async()=>({acknowledged:options?.method==='POST'||otherTab})};}};
 vm.createContext(c);vm.runInContext(helper,c);
 return {c,form,dialog,cancel,error,submit,calls,get shown(){return shown;},fail(){fail=true;}};
}
(async()=>{
 let h=setup();assert.equal(h.shown,0,'viewing never displays the notice');
 const first=h.c.ensureManualEditing(),also=h.c.ensureManualEditing();
 await new Promise(setImmediate);assert.equal(h.shown,1);assert.deepEqual(h.calls,['GET']);
 await h.form.onsubmit({preventDefault(){}});await Promise.all([first,also]);
 await h.c.ensureManualEditing();assert.equal(h.shown,1);assert.deepEqual(h.calls,['GET','POST']);
 h=setup(true);await h.c.ensureManualEditing();assert.equal(h.calls.length,0,'new page in acknowledged session skips notice');
 h=setup(false,true);await h.c.ensureManualEditing();assert.equal(h.shown,0,'another tab already acknowledged this login');
 h=setup();let pending=h.c.ensureManualEditing();pending.catch(()=>{});await new Promise(setImmediate);
 h.cancel.onclick();await assert.rejects(pending,/cancelled/);assert.deepEqual(h.calls,['GET'],'cancelling sends no acknowledgement or mutation');
 h=setup();pending=h.c.ensureManualEditing();pending.catch(()=>{});await new Promise(setImmediate);h.fail();
 await h.form.onsubmit({preventDefault(){}});assert.equal(h.error.hidden,false);assert.equal(h.submit.disabled,false);
 h.cancel.onclick();await assert.rejects(pending,/cancelled/);
 console.log('Manual notice: view, first edit, shared prompt, same-login reload, another tab, cancellation and failure passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
