const assert=require('node:assert/strict');
const {status,labor,hoursSummary,weekLabel}=require('../../app/static/v2/scheduling-workspace.js');
const board={period:null,shifts:[],week:{start:'2026-12-20',end:'2026-12-26'}};
assert.equal(status(board).tone,'neutral');
board.period={status:'DRAFT'};assert.equal(status(board).tone,'neutral');
board.period.generated_at='2026-10-20';assert.equal(status(board).tone,'draft');
board.current_published_period_id=1;assert.equal(status(board).tone,'mixed');
board.period.status='PUBLISHED';assert.equal(status(board).tone,'published');
board.period.status='ARCHIVED';board.mode_label='Archived';assert.equal(status(board).label,'Archived');
assert.deepEqual(labor({missing_rate_shift_count:24,costed_paid_hours:0,estimated_cost:0}),{value:'Not available',note:'24 shifts missing rates'});
assert.deepEqual(labor({missing_rate_shift_count:2,costed_paid_hours:10,estimated_cost:200}),{value:'$200.00',note:'2 shifts missing rates · partial estimate'});
assert.equal(hoursSummary(Array.from({length:4},()=>({closed:true,event_name:'Christmas'}))),'Christmas · 4 closures');
assert.match(weekLabel(board),/December 20.*26, 2026/);
console.log('Status states, truthful labor estimates, Christmas closure summary and selected-week dates passed.');

// A swap-created revision must expose its own publish action without a reload.
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {classList:{toggle(){}},replaceChildren(){},querySelector:()=>null,insertAdjacentHTML(){}});
  return nodes.get(selector);
}
const root = {querySelector: node};
const workspace = require('../../app/static/v2/scheduling-workspace.js');
const snapshot = {period:{id:1,status:'PUBLISHED'},mode_label:'Published',shifts:[{}],
  editable:false,actions:{clone_published:true},warnings:[],summary:{effective_shift_count:1,open_shift_count:0,serious_warning_count:0}};
workspace.update(root,snapshot);
assert.match(node('.schedule-completion-actions').innerHTML,/data-clone-published/);
snapshot.period={id:2,status:'DRAFT'};
snapshot.current_published_period_id=1;
snapshot.editable=true;
const secondaryButtons=new Map();
node('.schedule-secondary-actions').querySelector=selector=>secondaryButtons.get(selector);
node('.schedule-secondary-actions').insertAdjacentHTML=(_where,html)=>{const attr=html.match(/type="button" (data-[^>]*)>/)[1];secondaryButtons.set(`[${attr}]`,{});};
snapshot.actions={approve:true,publish:true,direct_edit:true,generate:true};
workspace.update(root,snapshot);
assert.match(node('.schedule-completion-actions').innerHTML,/data-publish-schedule/);
assert.doesNotMatch(node('.schedule-completion-actions').innerHTML,/data-clone-published/);
assert.doesNotMatch(node('.schedule-completion-actions').innerHTML,/[ ]disabled/);
assert.equal(secondaryButtons.get('[data-add-shift]').textContent,'Add custom shift');
assert.equal(secondaryButtons.get('[data-generate-schedule]').hidden,false);
assert.equal(node('[data-board-mode]').textContent,'Published + unpublished revision');
snapshot.actions.approve=false;
workspace.update(root,snapshot);
assert.match(node('.schedule-completion-actions').innerHTML,/[ ]disabled/);
snapshot.period.approval={id:7};
workspace.update(root,snapshot);
assert.doesNotMatch(node('.schedule-completion-actions').innerHTML,/[ ]disabled/);
