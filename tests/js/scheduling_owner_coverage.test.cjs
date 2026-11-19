const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const fields=Object.fromEntries(['commitment_employee_id','coverage_start_time','coverage_end_time','coverage_break_minutes','lead_designation'].map(k=>[k,{value:''}]));
fields.commitment_employee_id.selectedOptions=[{textContent:'Owner · Current owner'}];
fields.commitment_employee_id.add=function(option){this.innerHTML+=`<option value="${option.value}">${option.text}</option>`;};
const nodes=new Map();
const context={board:{owners:[{id:44,name:'Current owner'}]},attendanceForm:{elements:fields},
  $:key=>{if(!nodes.has(key))nodes.set(key,{});return nodes.get(key);},
  api:async()=>({candidates:[{id:22,name:'Employee',warnings:[]}],owners:[{id:44,name:'Current owner'}]}),
  escapeHtml:value=>String(value??'').replaceAll('<','&lt;'),URLSearchParams,
  Option:function(text,value){this.text=text;this.value=value;},showError:error=>{throw error;}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  let exceptionShift ='),source.indexOf('  async function openAttendance('))+
  '\nexceptionShift={id:10,employee_id:11};this.refresh=refreshCoverageCandidates;this.update=updateAttendanceFields;',context);
(async()=>{
 fields.commitment_employee_id.value='owner:44';fields.lead_designation.value='ASSIGN';
 fields.coverage_start_time.value='11:00';fields.coverage_end_time.value='15:00';fields.coverage_break_minutes.value='0';
 await context.refresh();
 assert.equal(fields.commitment_employee_id.value,'owner:44');
 assert.match(fields.commitment_employee_id.innerHTML,/value="owner:44"/);
 assert.match(fields.commitment_employee_id.innerHTML,/value="22"/);
 assert.equal(fields.lead_designation.disabled,true);assert.equal(fields.lead_designation.value,'KEEP');
 assert.equal(nodes.get('[data-coverage-hours]').hidden,false);
 context.api=async()=>({candidates:[],owners:[]});
 await context.refresh();
 assert.equal(fields.commitment_employee_id.value,'owner:44','Removing an Owner from current choices cannot silently cancel coverage');
 assert.match(fields.commitment_employee_id.innerHTML,/Current owner/);
 fields.commitment_employee_id.value='22';context.update();assert.equal(fields.lead_designation.disabled,false);
 const card=source.slice(source.indexOf('  function cardMarkup('),source.indexOf('  function mergeMoveDelta('));
 vm.runInContext(card+'\nthis.card=cardMarkup;',context);
 const markup=context.card({id:-9,owner_name:'Current <owner>',is_coverage:true,store_name:'Longview',
   exception:{status:'Replacement committed',original:{employee_name:'Original'},commitment:{owner_name:'Current <owner>'},actual_worker_names:[]}});
 assert.match(markup,/Owner · Current &lt;owner>/);
 assert.match(markup,/Committed: Owner · Current &lt;owner>/);
 console.log('Owner candidate refresh preserves selection, employee options and hours; Lead and card identity remain correct.');
})().catch(error=>{console.error(error);process.exitCode=1});
