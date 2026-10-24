// The actual dialog opener must distinguish coverage intent from historical work.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
const opener = source.slice(source.indexOf('  async function openAttendance('), source.indexOf("  $('[data-commitment-history]')?.addEventListener"));
const names = ['shift_id', 'event_type', 'commitment_employee_id', 'coverage_start_time', 'coverage_end_time', 'coverage_break_minutes', 'note'];
const elements = Object.fromEntries(names.map(name => [name, {value: '', innerHTML: ''}]));
const form = {elements, reset() { for (const field of Object.values(elements)) field.value = ''; }};
const nodes = new Map();
const sourceShift = {id: 10, employee_id: 11, employee_name: 'Micah', shift_date: '2030-05-05', start_time: '09:00', end_time: '17:00', time_label: '9 AM–5 PM', store_name: 'Longview', unpaid_break_minutes: 30,
  attendance_events: [{event_type: 'CALLED_OUT', note: 'Original optional note', voided: false}],
  exception: {commitment: {id: 90, employee_id: 22, start_time: '11:00:00', end_time: '15:00:00', unpaid_break_minutes: 10}, commitment_history: []}};
const actualCard = {id: -1000000010, source_shift_id: 10, is_coverage: true, employee_id: 33, start_time: '09:00', end_time: '17:00'};
let opened = 0;
const context = {board: {shifts: [sourceShift, actualCard], attendance_replacements: [{id: 11, name: 'Micah'}, {id: 22, name: 'Cameron'}, {id: 33, name: 'Lexi'}]},
  attendanceForm: form, attendanceDialog: {showModal() { opened++; }},
  $: selector => { if (!nodes.has(selector)) nodes.set(selector, {}); return nodes.get(selector); },
  clearError() {}, renderAttendanceHistory() {}, updateAttendanceFields() {}, refreshCoverageCandidates: async () => {}, escapeHtml: text => text};
vm.createContext(context);
const serializer = source.slice(source.indexOf('  function attendancePayload('), source.indexOf('  function updateAttendanceFields('));
vm.runInContext(serializer, context);
const values = new Map([['event_type', ''], ['commitment_employee_id', '22'],
  ['coverage_start_time', '08:45'], ['coverage_end_time', '22:15'], ['coverage_break_minutes', '0']]);
assert.equal(context.attendancePayload(values).event_type, null);
assert.equal(context.attendancePayload(values).commitment_employee_id, 22);
assert.equal(context.attendancePayload(values).coverage_break_minutes, 0);
values.set('event_type', 'CALLED_OUT');
assert.equal(context.attendancePayload(values).event_type, 'CALLED_OUT');
values.set('commitment_employee_id', '');
assert.equal(context.attendancePayload(values).cancel_coverage, true);
assert.equal(context.attendancePayload(values).coverage_start_time, null);
vm.runInContext('let exceptionShift=null,commitmentCandidates=[];' + opener, context);
(async () => {
  await context.openAttendance(actualCard);
  assert.equal(elements.shift_id.value, 10);
  assert.equal(elements.commitment_employee_id.value, 22, 'Cameron remains committed even when Lexi actually worked');
  assert.equal(elements.coverage_start_time.value, '11:00');
  assert.equal(elements.coverage_end_time.value, '15:00');
  assert.equal(elements.coverage_break_minutes.value, 10);
  assert.equal(elements.note.value, 'Original optional note');
  assert.equal(elements.event_type.value, 'CALLED_OUT', 'A genuine absence remains selected independently of relief');
  assert.equal(nodes.get('[data-coverage-hours]').open, true);
  assert.doesNotMatch(elements.commitment_employee_id.innerHTML, /value="11"/);
  sourceShift.exception.commitment = null;
  sourceShift.attendance_events = [];
  await context.openAttendance(sourceShift);
  assert.equal(elements.commitment_employee_id.value, '', 'An actual-only worker must never become a new commitment by opening the editor');
  assert.equal(elements.coverage_start_time.value, '09:00');
  assert.equal(elements.coverage_end_time.value, '17:00');
  assert.equal(elements.coverage_break_minutes.value, 30);
  assert.equal(elements.note.value, '');
  assert.equal(elements.event_type.value, '', 'An unmarked shift defaults to Covered / Reassigned without inventing an absence');
  assert.equal(opened, 2);
  console.log('Coverage editor preserves authoritative commitment, interval, and optional note without inferring actual worker commitments.');
})().catch(error => { console.error(error); process.exitCode = 1; });
