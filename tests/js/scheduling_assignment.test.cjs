// Exercise the actual assignment-dialog handlers without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
const handlers = source.slice(source.indexOf("  let swapKind = 'REPLACE';"), source.lastIndexOf('\n})();'));
function element() {
  return {value: '', hidden: false, disabled: false, required: false,
    options: [{textContent: ''}], listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; }};
}
const fields = Object.fromEntries(['kind', 'shift_id', 'other_shift_id', 'replacement_employee_id', 'reason', 'lead_designation', 'other_lead_designation', 'reassign_lead_shift_id'].map(name => [name, element()]));
const form = {...element(), elements: fields, querySelector() { return null; }, reset() { Object.values(fields).forEach(field => { field.value = ''; }); }};
const dialog = {showModal() { this.open = true; }, close() { this.open = false; }};
const open = {...element(), dataset: {swapOpen: 'REPLACE'}};
const nodes = {'[data-swap-dialog]': dialog, '[data-swap-form]': form};
for (const name of ['other-shift-label', 'replacement-label', 'other-shift-title', 'swap-description', 'swap-title', 'swap-error', 'swap-close']) nodes[`[data-${name}]`] = element();
let submitted;
const gapHelper = source.slice(source.indexOf('  function updateLeadGap('), source.indexOf('  for (const form of [shiftForm, moveForm])'));
vm.runInNewContext(gapHelper + handlers, {
  $: selector => nodes[selector], document: {querySelectorAll: () => [open]},
  board: {period: {id: 9, version: 4}, shifts: [{id: 11, employee_id: 1, employee_name: 'Alex', shift_date: '2026-10-15', store_name: 'Andresen', time_label: '8:45 AM–10 PM'}]},
  escapeHtml: value => value, URL, location: {href: 'http://localhost/v2/scheduling/week?start=2026-10-11', assign() {}},
  api: async (path, method, payload) => { submitted = payload; return {period_ids: [9]}; },
});

(async () => {
  open.listeners.click();
  assert.equal(fields.kind.value, 'REPLACE');
  for (const kind of ['REPLACE', 'SHIFT', 'STORE', 'REPLACE']) {
    fields.other_shift_id.value = 'stale other';
    fields.replacement_employee_id.value = 'stale employee';
    fields.kind.value = kind;
    fields.kind.listeners.change();
    const replace = kind === 'REPLACE';
    assert.equal(nodes['[data-other-shift-label]'].hidden, replace);
    assert.equal(fields.other_shift_id.disabled, replace);
    assert.equal(fields.other_shift_id.required, !replace);
    assert.equal(nodes['[data-replacement-label]'].hidden, !replace);
    assert.equal(fields.replacement_employee_id.disabled, !replace);
    assert.equal(fields.replacement_employee_id.required, replace);
    assert.equal(replace ? fields.other_shift_id.value : fields.replacement_employee_id.value, '');
    assert.ok(!/validat|revision|approv/i.test(nodes['[data-swap-description]'].textContent));
    // Even stale browser values in hidden controls must never reach the backend.
    fields.shift_id.value = '11'; fields.other_shift_id.value = '12';
    fields.replacement_employee_id.value = '3'; fields.reason.value = 'Coverage change';
    fields.lead_designation.value = 'ASSIGN'; fields.other_lead_designation.value = 'REMOVE'; fields.reassign_lead_shift_id.value = '13';
    await form.listeners.submit({preventDefault() {}});
    assert.equal(submitted.kind, kind === 'STORE' ? 'STORE' : 'SHIFT');
    assert.equal(submitted.other_shift_id, replace ? null : 12);
    assert.equal(submitted.replacement_employee_id, replace ? 3 : null);
    assert.equal(submitted.expected_versions[9], 4);
    assert.equal(submitted.reason, 'Coverage change');
    assert.equal(submitted.lead_designation, 'ASSIGN');
    assert.equal(submitted.other_lead_designation, replace ? 'KEEP' : 'REMOVE');
    assert.equal(submitted.reassign_lead_shift_id, 13);
  }
  console.log('Assignment choices, field visibility, mode transitions and backend payloads passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
