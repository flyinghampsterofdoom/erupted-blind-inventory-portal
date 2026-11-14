const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {dailyLeadAction,shiftLabel}=require('../../app/static/v2/scheduling-grid.js');
test('two-click action respects administrator permission and existing qualification',()=>{
 const board={actions:{assign_lead_of_day:true}};
 const shift={employee_id:2,employee_lead_capable:true,can_change_lead_designation:true};
 assert.equal(dailyLeadAction(board,shift),'Make Lead');
 assert.equal(dailyLeadAction(board,{...shift,is_lead_of_day:true}),'Remove Lead');
 for(const s of [{...shift,employee_lead_capable:false},{...shift,is_training:true},{...shift,is_coverage:true},{...shift,can_change_lead_designation:false}])
   assert.equal(dailyLeadAction(board,s),null);
 assert.equal(dailyLeadAction({actions:{}},shift),null);
 assert.equal(shiftLabel({...shift,is_lead_of_day:true}),'Lead');
});
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const context={};vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function applyDailyLead('),source.indexOf('  async function saveDailyLead(')),context);
test('compact receipt transfers company-wide Lead across stores and preserves assignments',()=>{
 const first={id:1,store_id:1,employee_id:11,is_lead_of_day:true};
 const second={id:2,store_id:2,employee_id:22,is_lead_of_day:false};
 const original={period:{id:3,version:4},shifts:[first,second,{id:9,store_id:1}],
   warnings:[{type:'OTHER',date:'2030-05-14'}],week:{days:[{iso:'2030-05-14'}]},groups:[{store_id:1},{store_id:2}]};
 const receipt={period_id:3,base_version:4,version:5,date:'2030-05-14',shift_id:2,lead_gap:false,
   changes:[{id:1,is_lead_of_day:false},{id:2,is_lead_of_day:true}]};
 const updated=context.applyDailyLead(original,receipt);
 assert.equal(updated.period.version,5);
 assert.equal(updated.shifts[0].is_lead_of_day,false);assert.equal(updated.shifts[1].is_lead_of_day,true);
 assert.equal(updated.shifts[1].employee_id,22);assert.equal(updated.shifts[1].store_id,2);
 assert.equal(original.shifts[0].is_lead_of_day,true);
 assert.deepEqual({...updated.shifts[2]},original.shifts[2]);
 const removed=context.applyDailyLead(updated,{...receipt,base_version:5,version:6,lead_gap:true,changes:[{id:2,is_lead_of_day:false}]});
 assert.equal(removed.shifts[1].employee_id,22);
 assert.equal(removed.warnings.filter(w=>w.type==='NO_LEAD_OF_DAY').length,1);
 assert.equal(removed.week.days[0].warning_count,2);
 assert.throws(()=>context.applyDailyLead(updated,receipt),/Refresh/);
});
test('Lead action uses compact endpoint without manual approval, full board fetch or confirmation',()=>{
 const handler=source.slice(source.indexOf('  async function saveDailyLead('),source.indexOf('  function shiftPayload('));
 assert.match(handler,/daily-lead/);
 assert.doesNotMatch(handler,/confirm\(|prompt\(|ensureManualEditing|\/api\/board/);
 assert.match(handler,/applyDailyLead/);
});

test('daily Lead bypasses first-session manual assignment acknowledgement',()=>{
 const ctx={};vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('  function isManualAssignment('),source.indexOf('  async function ensureManualEditing(')),ctx);
 assert.equal(ctx.isManualAssignment('/v2/scheduling/api/periods/3/shifts/2/daily-lead'),false);
 assert.equal(ctx.isManualAssignment('/v2/scheduling/api/periods/3/shifts/2'),true);
});
test('a filtered board can accept clearing the authorized Lead at an offscreen store',()=>{
 const snapshot={period:{id:3,version:1},shifts:[{id:2,store_id:2}],warnings:[],week:{days:[]},groups:[]};
 const result=context.applyDailyLead(snapshot,{period_id:3,base_version:1,version:2,date:'2030-05-14',shift_id:2,
  lead_gap:false,changes:[{id:1,is_lead_of_day:false},{id:2,is_lead_of_day:true}]});
 assert.equal(result.shifts.length,1);assert.equal(result.shifts[0].is_lead_of_day,true);
});
