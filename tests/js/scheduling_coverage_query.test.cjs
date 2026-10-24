// Exercise the actual request function with coverage intervals and scoped board URLs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/v2/scheduling.js', 'utf8');
const apiSource = source.slice(source.indexOf('  async function api('), source.indexOf('  const publishDialog'));
let scope = '?start=2026-10-18&store_id=7&store_id=9&schedule_period_id=12';
let requested;
const context = {
  board: {period: {id: 12}}, query: () => scope, csrf: () => '',
  decodeURIComponent, setTimeout, clearTimeout, AbortController,
  fetch: async (path, options) => {
    requested = new URL(path, 'http://localhost');
    assert.equal(options.method, 'GET');
    // Model the server's typed query validation that rejected `0?start=...`.
    assert.equal(Number.isNaN(Number(requested.searchParams.get('coverage_break_minutes') || 0)), false);
    return {ok: true, json: async () => ({candidates: [{id: 22, name: 'Cameron', warnings: [{message: 'Already scheduled at Andresen'}]}]})};
  },
};
vm.createContext(context);
vm.runInContext('let writeInFlight=false,refreshRequired=false,pendingMove=null;' + apiSource, context);
(async () => {
  const result = await context.api('/v2/scheduling/api/shifts/13/coverage-candidates?coverage_start_time=08%3A45&coverage_end_time=22%3A15&coverage_break_minutes=0', 'GET');
  assert.equal(requested.searchParams.get('coverage_start_time'), '08:45');
  assert.equal(requested.searchParams.get('coverage_end_time'), '22:15');
  assert.equal(requested.searchParams.get('coverage_break_minutes'), '0');
  assert.equal(requested.searchParams.get('start'), '2026-10-18');
  assert.deepEqual(requested.searchParams.getAll('store_id'), ['7', '9']);
  assert.equal(requested.searchParams.get('schedule_period_id'), '12');
  assert.equal(result.candidates[0].warnings[0].message, 'Already scheduled at Andresen');
  await context.api('/v2/scheduling/api/shifts/13/coverage-candidates', 'GET');
  assert.equal(requested.searchParams.get('start'), '2026-10-18');
  scope = '';
  await context.api('/v2/scheduling/api/shifts/13/coverage-candidates?coverage_break_minutes=0', 'GET');
  assert.equal(requested.search, '?coverage_break_minutes=0');
  console.log('Coverage interval queries preserve board week, repeated store scope, period, and warning responses.');
})().catch(error => { console.error(error); process.exitCode = 1; });
