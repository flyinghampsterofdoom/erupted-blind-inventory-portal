const {test} = require('node:test');
const assert = require('node:assert/strict');
const {iso, parse, sunday, monthDate, monthGrid, weekURL} = require('../../app/static/v2/scheduling-calendar.js');

test('Christmas, Sunday/Saturday boundaries and year crossover use local calendar dates', () => {
  for (let day = 20; day <= 26; day++) assert.equal(iso(sunday(parse(`2026-12-${day}`))), '2026-12-20');
  assert.equal(iso(sunday(parse('2026-12-27'))), '2026-12-27');
  assert.equal(iso(sunday(parse('2027-01-01'))), '2026-12-27');
  assert.equal(iso(sunday(parse('2026-03-08'))), '2026-03-08'); // DST Sunday
  assert.equal(iso(sunday(parse('2026-11-01'))), '2026-11-01');
});
test('monthly grids contain complete Sunday through Saturday weeks including leap years', () => {
  for (const year of [2024, 2026, 2027, 2032]) for (let month = 0; month < 12; month++) {
    const dates = monthGrid(monthDate(year, month));
    assert.equal(dates[0].getUTCDay(), 0); assert.equal(dates.at(-1).getUTCDay(), 6);
    assert.equal(dates.length % 7, 0); assert.ok(dates.length <= 42);
    assert.equal(dates.filter(d => d.getUTCMonth() === month).length, monthDate(year, month + 1, 0).getUTCDate());
  }
});
test('selection preserves store filters and removes stale revision and editing parameters', () => {
  const url = new URL(weekURL('https://example.test/v2/scheduling/week?store_id=2&store_id=4&period_id=29&history=edit&start=2026-12-13&message=old', '2026-12-20'));
  assert.equal(url.searchParams.get('start'), '2026-12-20');
  assert.deepEqual(url.searchParams.getAll('store_id'), ['2', '4']);
  for (const key of ['period_id', 'history', 'message']) assert.equal(url.searchParams.has(key), false);
});
