const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/employee-schedule.js','utf8');
const code=source.slice(source.indexOf('  function applyResult('),source.indexOf('  let syncing='))+'\nthis.applyResult=applyResult;';
let requestNode=null,rendered='',name='',own=false,hidden=true;
const card={dataset:{assignmentVersion:'3'},querySelector(k){return k==='[data-shift-employee]'?{set textContent(v){name=v}}:{set hidden(v){hidden=v}}},classList:{toggle(k,v){own=v}}};
const list={querySelector(k){return k==='[data-no-requests]'?null:requestNode},insertAdjacentHTML(_,s){rendered=s}};
const root={querySelector(k){return k==='[data-request-list]'?list:card}};
const ctx={root,Date};vm.createContext(ctx);vm.runInContext(code,ctx);
function result(version,status='COMPLETED',at='2030-05-01T10:00:00Z'){return {request_id:1,status,updated_at:at,request_html:'rendered '+status,assignments:[{id:7,period_version:version,employee_name:'New employee',own:true}]}}
ctx.applyResult(result(2));assert.equal(name,'');assert.equal(card.dataset.assignmentVersion,'3');
ctx.applyResult(result(4));assert.equal(name,'New employee');assert.equal(own,true);assert.equal(hidden,false);
requestNode={dataset:{requestState:'COMPLETED:2030-05-01T10:00:00Z',updatedAt:'2030-05-01T10:00:00Z'},set outerHTML(v){rendered=v}};
rendered='final';ctx.applyResult({...result(4,'PENDING_MANAGER','2030-05-01T09:59:59Z'),assignments:[]});assert.equal(rendered,'final');
ctx.applyResult({...result(4,'PENDING_RECIPIENT','2030-05-01T10:00:01Z'),assignments:[]});assert.equal(rendered,'final');
assert.match(source,/if\(pending\)return;pending=true/);assert.match(source,/If the connection failed, refresh before retrying/);
console.log('Out-of-order request and assignment replies cannot regress the UI; ownership and duplicate guards pass.');
