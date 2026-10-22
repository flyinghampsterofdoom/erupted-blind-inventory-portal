const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const c={};vm.createContext(c);vm.runInContext(source.slice(source.indexOf('  function mergeMoveDelta('),source.indexOf('  function render(')),c);
const date='2027-06-06',shift={id:10,employee_id:1,shift_date:date,store_id:2,warning_ids:[1],has_warning:true};
const people=[1,2].map(id=>({id,name:'Person'+id,days:[{iso:date,cell_id:'cell'+id,indicators:['Preserved'],shifts:id===1?[shift]:[]}]}));
const previous={period:{id:5,version:3},week:{start:date,days:[{iso:date}]},employees:people,shifts:[shift],groups:[{store_id:2,employees:people,open_days:[]}],actions:{edit_shifts:true}};
const delta={period:{id:5,version:4},base_version:3,layout_employee_ids:[1,2],
 shifts:[{...shift,employee_id:2}],shift_warning_states:[{id:10,warning_ids:[500],has_warning:true}],
 employees:people.map(p=>({id:p.id,assigned_shift_count:p.id===2?1:0,warning_count:0})),
 days:[{iso:date,warning_count:1}],warnings:[],summary:{assigned_hours:8},actions:{edit_shifts:true}};
const result=c.mergeMoveDelta(previous,delta);
assert.equal(previous.shifts[0].employee_id,1,'canonical input remains untouched');
assert.equal(result.snapshot.shifts[0].employee_id,2);
assert.equal(result.snapshot.employees[0].days[0].shifts.length,0);
assert.equal(result.snapshot.employees[1].days[0].shifts[0].id,10);
assert.equal(result.snapshot.employees[1].days[0].indicators[0],'Preserved');
assert.equal(result.snapshot.groups[0].employees[1].assigned_shift_count,1);
assert.equal(result.changedIds.size,1);
assert.throws(()=>c.mergeMoveDelta(previous,{...delta,base_version:2}),/full refresh/);
assert.throws(()=>c.mergeMoveDelta(previous,{...delta,layout_employee_ids:[1,2,3]}),/full refresh/);
assert.throws(()=>c.mergeMoveDelta(previous,{...delta,shift_warning_states:[]}),/full refresh/);
assert.equal(c.mergeMoveDelta(previous,{...delta,actions:{edit_shifts:false}}).changedIds,null,'permission presentation changes rebuild affected controls');
console.log('Move delta: canonical assignment, counters, group/day references, static indicators and version/layout recovery passed');
