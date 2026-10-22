// Exercise delayed acknowledgement, rejection, and ambiguous transport failure.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
function extract(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }
function setup() {
  const original = {insertBefore(card) { card.parentNode = original; }};
  const destination = {append(card) { card.parentNode = destination; }};
  const card = {parentNode: original, nextSibling: null, dataset:{}, setAttribute(){}, removeAttribute(){},querySelector:()=>null};
  const shift = {id:42,employee_id:7,shift_date:'2027-01-03',store_id:1};
  let finish, response, calls = 0, rendered = 0;
  const context = {performance:{mark(){},measure(){},clearMeasures(){}},requestAnimationFrame:fn=>fn(),
    board:{period:{id:5,version:8},actions:{},shifts:[shift],stores:[]},
    document:{getElementById:()=>card},decodeURIComponent,JSON,Error,AbortController,setTimeout,clearTimeout,
    clearError(){},employeeStoreConflict:()=>null,
    $$:()=>[{dataset:{employeeId:'8',shiftDate:'2027-01-03'},querySelector:sel=>sel==='.schedule-cell__shifts'?destination:null}],
    csrf:()=>'',query:()=>'',moveDialog:{close(){}},announce(){},showError:e=>{context.error=e;},
    render(){rendered++;},fetch:()=>{calls++;return new Promise((resolve,reject)=>{finish={resolve,reject};});}};
  vm.createContext(context);
  vm.runInContext('let pendingMove=null,writeInFlight=false,refreshRequired=false;'+
    extract('  async function api(', '  const publishDialog')+
    extract('  function shiftPayload(', '  function cellFor(')+
    extract('  async function saveMove(', '  async function placeStoreShift('),context);
  return {context,card,original,destination,shift,get calls(){return calls;},get rendered(){return rendered;},
    resolve(status=200) { finish.resolve({ok:status===200,status,json:async()=>({board:context.board,message:'Saved',save_outcome:'local_saved'})}); },
    malformed() { finish.resolve({ok:true,status:200,json:async()=>{throw new SyntaxError('Malformed');}}); },
    reject() { finish.reject(new TypeError('Connection lost')); }};
}
(async()=>{
  let h=setup(), c=h.context;
  let save=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
  assert.equal(h.card.parentNode,h.destination,'move must occur before acknowledgement');
  assert.equal(h.rendered,0);
  await c.saveMove(h.shift,{employee_id:9,shift_date:h.shift.shift_date,store_id:1});
  assert.equal(h.calls,1,'second move must not send a stale version');
  h.resolve(); await save;
  assert.equal(h.rendered,1);
  for (const status of [403,409,422,500]) {
    h=setup();c=h.context;
    save=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
    h.resolve(status);await save;
    assert.equal(h.card.parentNode,h.original,`restore on HTTP ${status}`);
    assert.equal(h.rendered,0);
    if(status===500) assert.match(c.error.message,/could not be confirmed/);
    if (status===409 || status===500) {
      await c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
      assert.equal(h.calls,1,'uncertain or stale state must require refresh');
    }
  }
  h=setup();c=h.context;
  save=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
  h.reject();await save;
  assert.equal(h.card.parentNode,h.original);
  assert.match(c.error.message,/could not be confirmed/);
  await c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
  assert.equal(h.calls,1,'lost response must never be retried blindly');
  h=setup();c=h.context;
  save=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
  h.malformed();await save;
  assert.equal(h.card.parentNode,h.original);
  assert.match(c.error.message,/could not be confirmed/);
  await c.saveMove(h.shift,{employee_id:9,shift_date:h.shift.shift_date,store_id:1});assert.equal(h.calls,1);
  h=setup();c=h.context;c.render=()=>{throw new Error('Render failed');};
  save=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});h.resolve();await save;
  assert.equal(h.card.parentNode,h.destination,'acknowledged save must not claim a rollback');
  assert.match(c.error.message,/was saved/);
  await c.saveMove(h.shift,{employee_id:9,shift_date:h.shift.shift_date,store_id:1});assert.equal(h.calls,1);
  h=setup();c=h.context;c.board.period.generated_origin=true;c.isManualAssignment=()=>true;
  let acknowledge;c.ensureManualEditing=()=>new Promise(resolve=>{acknowledge=resolve;});
  // Both moves await the same first-session confirmation.
  const ack=new Promise(resolve=>{acknowledge=resolve;});c.ensureManualEditing=()=>ack;
  const first=c.saveMove(h.shift,{employee_id:8,shift_date:h.shift.shift_date,store_id:1});
  const second=c.saveMove(h.shift,{employee_id:9,shift_date:h.shift.shift_date,store_id:1});
  acknowledge();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.calls,1,'confirmation must not release two stale writes');h.resolve();await Promise.all([first,second]);
  h=setup();c=h.context;
  vm.runInContext(extract('  function render(', '  function renderWarnings('),c);
  c.render({period:{id:5,version:7}});assert.equal(c.board.period.version,8,'older snapshots never replace a newer board');
  vm.runInContext('pendingMove={};',c);c.render({period:{id:5,version:9}});
  assert.equal(c.board.period.version,8,'GET must not erase pending visual movement');
  assert.equal(vm.runInContext('pendingMove.deferredBoard.period.version',c),9);
  console.log('Optimistic move delayed success, rejection, concurrency, and transport tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Session acknowledgement cannot replace a per-action Owner/PTO decision.
{
  let answers=[], prompts=0, messages=[];
  const c={board:{actions:{override_operational:true}},confirm:message=>{messages.push(message);return answers.shift();},prompt:()=>{prompts++;return 'Explicit emergency coverage';},showError(){},Error};
  vm.createContext(c);vm.runInContext(extract('  function confirmOperationalException(', '  async function api('),c);
  answers=[true,false];assert.equal(c.confirmOperationalException(['APPROVED_TIME_OFF'],'Conflict'),null);
  assert.equal(prompts,0);assert.match(messages[1],/Separately confirm/);
  answers=[true,true];const result=c.confirmOperationalException(['APPROVED_TIME_OFF'],'Conflict');
  assert.equal(result.override_pto,true);assert.equal(result.override_reason,'Explicit emergency coverage');
  c.board.actions.override_operational=false;assert.equal(c.confirmOperationalException([], 'Conflict'),null);
}

{
  const nodes={};const c={errorBox:{},$:s=>(nodes[s]??={}),announce(){},escapeHtml:s=>s};
  vm.createContext(c);vm.runInContext(extract('  function showError(', '  function clearError('),c);
  c.showError({requiresRefresh:true,message:'Unconfirmed'});
  assert.equal(nodes['[data-board-error-title]'].textContent,'Refresh required to confirm schedule');
  c.showError({status:422,message:'Rejected'});
  assert.equal(nodes['[data-board-error-title]'].textContent,'Schedule not updated');
}
