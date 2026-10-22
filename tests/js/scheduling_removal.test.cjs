// Exercise the removal save path and both permission-controlled card states.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
function extract(name, next) {
  return source.slice(source.indexOf(`  ${name}`), source.indexOf(`  ${next}`, source.indexOf(`  ${name}`)));
}
const board = {period:{id:5,version:8},editable:true,actions:{remove_assignments:true},shifts:[]};
const shift = {id:42,employee_id:7,shift_date:'2027-01-03',store_id:1,time_label:'9–10',store_name:'Synthetic'};
let sent, rendered, announced;
const context = {board,pendingMove:null,writeInFlight:false,refreshRequired:false,performance:{mark(){},measure(){},clearMeasures(){}},document:{getElementById:()=>null},escapeHtml:value=>value||'',clearError(){},employeeStoreConflict:()=>null,
  $$:()=>[],api:async(path,method,payload)=>{sent={path,method,payload};return {board,message:'Assignment removed'};},
  render:snapshot=>{rendered=snapshot;},moveDialog:{close(){}},announce:message=>{announced=message;},
  showError:error=>{throw error;}};
vm.createContext(context);
vm.runInContext(extract('function shiftPayload(', 'function cellFor(')+extract('function cardMarkup(', 'function render(')+extract('async function saveMove(', 'async function placeStoreShift('),context);
(async()=>{
  assert.match(context.cardMarkup(shift), /Remove assignment/);
  assert.doesNotMatch(context.cardMarkup({...shift,manually_locked:true}), /Remove assignment/);
  assert.match(context.cardMarkup({...shift,manually_locked:true}), /Unlock for regeneration/);
  board.actions.remove_assignments=false;
  assert.doesNotMatch(context.cardMarkup(shift), /Remove assignment/);
  board.actions.remove_assignments=true;
  await context.saveMove(shift,{employee_id:null,shift_date:shift.shift_date,store_id:shift.store_id});
  assert.equal(sent.method,'PATCH');
  assert.equal(sent.payload.employee_id,null);
  assert.equal(sent.payload.expected_version,8);
  assert.equal(sent.payload.override_hard_unavailability,false);
  assert.equal(rendered,board);assert.equal(announced,'Assignment removed');
  assert.match(context.cardMarkup({...shift,is_open:true,intentionally_unfilled:true}),/manual removal preserved/);
  assert.match(context.cardMarkup({...shift,is_open:true,business_closed:true}),/no coverage required/);
  assert.match(context.cardMarkup({...shift,is_open:true}),/requires coverage/);
  console.log('Scheduling removal UI tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
