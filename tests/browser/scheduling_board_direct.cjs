/* Run against the disposable Schedule Board demo, using its synthetic test week. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.ERUPTED_PLAYWRIGHT_MODULE);
const config = JSON.parse(fs.readFileSync(process.env.ERUPTED_BOARD_DEMO_CONFIG));
const origin = process.env.ERUPTED_BOARD_DEMO_ORIGIN || 'http://127.0.0.1:8776';
assert.equal(new URL(origin).hostname, '127.0.0.1');
const boardUrl = week => origin + '/v2/scheduling/week?start=' + week;
const date = (week, offset) => new Date(Date.parse(week) + offset * 86400000).toISOString().slice(0, 10);
const cell = (page, employee, day) => page.locator(`#schedule-cell-${employee}-${day}`);
const card = locator => locator.locator('[data-shift-card]');
const tile = (page, name) => page.getByRole('group', {name:'Assignment palette'}).getByRole('button', {name, exact:true});
const verify = async (page, name) => {
  await page.locator('[data-placement-feedback]').getByText(name).waitFor();
  assert.equal(await page.locator('dialog[open]').count(), 0);
};
const drag = async (page, from, to) => {
  await from.scrollIntoViewIfNeeded();
  const source = await from.boundingBox(), dest = await to.boundingBox();
  await page.mouse.move(source.x + source.width/2, source.y + source.height/2);
  await page.mouse.down();
  await page.mouse.move(dest.x + dest.width/2, dest.y + dest.height/2, {steps:12});
  await page.mouse.up();
};
const capture = async (page, name) => {
  if (process.env.ERUPTED_BOARD_SCREENSHOTS) await page.screenshot({path:path.join(process.env.ERUPTED_BOARD_SCREENSHOTS,name)});
};

(async () => {
  const browser = await chromium.launch({executablePath:process.env.ERUPTED_CHROMIUM, headless:true});
  try {
    const desktop = await browser.newPage({viewport:{width:1600,height:1000}});
    desktop.setDefaultTimeout(12000);
    const errors = [];
    desktop.on('pageerror', e => errors.push(e.message));
    desktop.on('dialog', dialog => { errors.push(dialog.message()); dialog.dismiss(); });
    const [avery, riley] = config.staff;
    const week = config.test_week;
    await desktop.goto(boardUrl(week));
    await desktop.locator('[data-shift-details-open]').first().waitFor();
    assert.deepEqual(await desktop.locator('[data-assignment-palette] button').allTextContents(),
      ['Andresen','HWY 99','Longview','SR 503','Training']);
    assert.equal(await desktop.locator('.shift-card-quick').count(), 0);
    assert.equal(await desktop.locator('[data-shift-details-open]').filter({hasText:/Full Shift|[0-9]:[0-9]/}).count(), 0);
    const mondayRiley = cell(desktop,riley,date(week,1));
    const tuesdayRiley = cell(desktop,riley,date(week,2));
    const tuesdayAvery = cell(desktop,avery,date(week,2));
    await drag(desktop,tile(desktop,'Andresen'),tuesdayRiley);
    await verify(desktop,'Assignment added.');
    assert.match(await card(tuesdayRiley).innerText(), /Andresen\s+Coverage/);
    await capture(desktop,'01-created.png');
    await desktop.reload();
    await card(tuesdayRiley).waitFor();

    await drag(desktop,card(tuesdayRiley).locator('.shift-card-main'),tuesdayAvery);
    await verify(desktop,'Assignment moved.');
    assert.equal(await card(tuesdayRiley).count(),0);
    await card(tuesdayAvery).waitFor();
    await capture(desktop,'02-moved.png');
    await drag(desktop,card(tuesdayAvery).locator('.shift-card-main'),mondayRiley);
    await verify(desktop,'Assignment moved.');
    assert.equal(await card(tuesdayAvery).count(),0);
    await card(mondayRiley).waitFor();

    const occupied = cell(desktop,avery,date(week,1));
    await drag(desktop,card(mondayRiley).locator('.shift-card-main'),occupied);
    await desktop.locator('[data-board-error]').getByText('That employee already has an assignment on this date.').waitFor();
    assert.equal(await card(mondayRiley).count(),1);
    assert.equal(await card(occupied).count(),1);

    const beforeTraining = Number(await desktop.locator('[data-summary="assigned_hours"]').innerText());
    const beforeStoreCoverage = await desktop.locator('[data-store-shift-summary]').allTextContents();
    // Keyboard alternative uses the same action as dragging.
    await tile(desktop,'Training').focus();
    await desktop.keyboard.press('Enter');
    const trainingCell = cell(desktop,config.trainee_id,date(week,3));
    await trainingCell.focus();
    await desktop.keyboard.press('Enter');
    await verify(desktop,'Assignment added.');
    assert.equal((await card(trainingCell).locator('.shift-card-main').innerText()).trim(),'Training');
    assert.equal(Number(await desktop.locator('[data-summary="assigned_hours"]').innerText()),beforeTraining+3.5);
    assert.deepEqual(await desktop.locator('[data-store-shift-summary]').allTextContents(),beforeStoreCoverage);
    await capture(desktop,'03-training.png');

    // Keyboard move from the details action never opens the legacy move form.
    await card(trainingCell).locator('[data-shift-details-open]').press('Enter');
    await desktop.locator('.shift-inspector').getByRole('button',{name:'Move',exact:true}).press('Enter');
    const nextTraining = cell(desktop,config.trainee_id,date(week,4));
    assert.equal(await desktop.locator(':focus').getAttribute('id'), await trainingCell.getAttribute('id'));
    await desktop.keyboard.press('ArrowRight');
    await desktop.keyboard.press('Enter');
    await verify(desktop,'Assignment moved.');
    await card(nextTraining).waitFor();
    await card(nextTraining).getByRole('button',{name:/Remove Training/}).press('Enter');
    await verify(desktop,'Assignment removed.');
    assert.equal(await card(nextTraining).count(),0);

    const removable = card(mondayRiley);
    await removable.hover();
    await removable.getByRole('button',{name:/Remove Andresen/}).click();
    await verify(desktop,'Assignment removed.');
    assert.equal(await removable.count(),0);
    assert.equal(Number(await desktop.locator('[data-summary="assigned_hours"]').innerText()),beforeTraining-13.25);
    await capture(desktop,'04-removed.png');
    await desktop.getByRole('button',{name:'Undo',exact:true}).click();
    await verify(desktop,'Assignment restored.');
    await removable.waitFor();
    await removable.hover();
    await removable.getByRole('button',{name:/Remove Andresen/}).click();
    await verify(desktop,'Assignment removed.');
    await desktop.reload();
    assert.equal(await removable.count(),0);

    const published = config.published_week;
    await desktop.goto(boardUrl(published));
    const publishedId = await desktop.locator('[data-schedule-board]').getAttribute('data-period-id');
    const fromPublished = cell(desktop,avery,date(published,1));
    const toPublished = cell(desktop,riley,date(published,1));
    await drag(desktop,card(fromPublished).locator('.shift-card-main'),toPublished);
    await desktop.getByRole('button',{name:'Publish Schedule'}).waitFor();
    assert.notEqual(await desktop.locator('[data-schedule-board]').getAttribute('data-period-id'),publishedId);
    await card(toPublished).waitFor();
    await desktop.goto(boardUrl(published)+'&period_id='+publishedId);
    await card(fromPublished).waitFor();
    assert.equal(await card(toPublished).count(),0);

    const touch = await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
    touch.setDefaultTimeout(12000);
    touch.on('pageerror', e => errors.push(e.message));
    await touch.goto(boardUrl(week));
    const touchTarget = cell(touch,riley,date(week,3));
    await tile(touch,'HWY 99').tap();
    await touchTarget.tap();
    await verify(touch,'Assignment added.');
    assert.match(await card(touchTarget).innerText(), /HWY 99/);

    const cdp = await touch.context().newCDPSession(touch);
    const target = cell(touch,riley,date(week,4));
    await touchTarget.evaluate(node => {
      const grid = node.closest('[data-board-scroll]');
      // Place the two adjacent dates after the sticky employee column.
      grid.scrollLeft += node.getBoundingClientRect().left - grid.getBoundingClientRect().left - 130;
    });
    const from = await card(touchTarget).locator('.shift-card-main').boundingBox();
    const to = await target.boundingBox();
    const grid = await touch.locator('[data-board-scroll]').boundingBox();
    const dropX = (to.x + Math.min(to.x + to.width, grid.x + grid.width - 5)) / 2;
    const touchPoint = (x,y) => [{x:Math.floor(x),y:Math.floor(y)}];
    const x = from.x+from.width/2, y = from.y+from.height/2;
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:touchPoint(x,y)});
    for (let step=1;step<=10;step++) await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',
      touchPoints:touchPoint(x+(dropX-x)*step/10,y+(to.y+to.height/2-y)*step/10)});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await verify(touch,'Assignment moved.');
    await card(target).waitFor();
    await card(target).getByRole('button',{name:/Remove HWY 99/}).tap();
    await verify(touch,'Assignment removed.');
    assert.equal(await card(target).count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS desktop drag/create/move/remove, keyboard placement and move, touch tap and drag, Training totals, occupied drop, persistence, and published draft isolation.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
