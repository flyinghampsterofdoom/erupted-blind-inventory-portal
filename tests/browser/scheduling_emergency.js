/* Desktop/mobile checks using the isolated scheduling_emergency_server.py fixture. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const out=process.env.SCHEDULING_EVIDENCE_DIR;
const fixture=JSON.parse(fs.readFileSync(path.join(out,'local.json')));
assert.equal(new URL(fixture.url).hostname,'127.0.0.1');
(async()=>{
 const browser=await chromium.launch({headless:true});const checks=[];const errors=[];
 try{
  for(const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]){
   const context=await browser.newContext({viewport:{width,height}});const page=await context.newPage();
   page.on('pageerror',e=>errors.push(e.message));
   await page.goto(fixture.url+'/login');await page.locator('[name=username]').fill(fixture.username);await page.locator('[name=password]').fill(fixture.password);
   await Promise.all([page.waitForURL(url=>!url.pathname.startsWith('/login')),page.locator('button[type=submit]').click()]);
   await page.goto(fixture.url+'/v2/scheduling/week?start='+fixture.week);
   await page.locator('.schedule-shift.is-compact').first().waitFor();
   assert.equal(await page.locator('.shift-card-callout').count(),0);
   assert.equal(await page.locator('.has-owner-coverage').count(),1);
   const header=page.locator('#schedule-day-'+fixture.day);assert.equal(await header.evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(255, 245, 194)');
   assert.equal(await page.locator('.schedule-cell.has-owner-coverage').count(),0);
   await header.locator('summary').click();assert.equal(await header.locator('details[open] li').count(),2);
   assert.match(await header.innerText(),/Existing Owner/);assert.match(await header.innerText(),/North/);assert.match(await header.innerText(),/South/);
   await header.locator('summary').click();
   const quick=page.locator('.shift-card-quick').first();await quick.click();await page.locator('.shift-inspector[open]').waitFor();
   assert.match(await page.locator('.shift-inspector').innerText(),/Edit/);await page.locator('[data-inspector-close]').click();
   await page.screenshot({path:path.join(out,name+'-board.png'),fullPage:true});
   await page.goto(fixture.url+'/v2/scheduling/emergency-coverage');
   await page.locator('[name=employee_id]').selectOption(String(fixture.employee));await page.locator('[name=final_day]').fill('2030-05-13');
   await page.getByRole('button',{name:'Preview affected shifts'}).click();
   await page.getByRole('button',{name:'Record departure and prepare coverage revisions'}).waitFor();
   await page.screenshot({path:path.join(out,name+'-departure.png'),fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   checks.push({viewport:name,headerOnly:true,multipleOwnerDetails:true,menuPreserved:true,noCardCalloutLinks:true,departurePreview:true});
   await context.close();
  }
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify({status:'PASS',checks,errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
