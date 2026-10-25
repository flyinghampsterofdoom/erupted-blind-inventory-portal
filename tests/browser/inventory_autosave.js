/* Real browser + disposable local PostgreSQL qualification. See docs/development/autosave-interaction.txt. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const output=process.env.AUTOSAVE_EVIDENCE_DIR;
if(!output)throw Error('AUTOSAVE_EVIDENCE_DIR is required');
const local=JSON.parse(fs.readFileSync(path.join(output,'local.json')));
if(new URL(local.url).hostname!=='127.0.0.1')throw Error('Local fixture only');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const result={conditions:[],integrity:[],errors:[]};
let browser;
const saved=page=>page.waitForFunction(()=>{
 const form=document.querySelector('form[data-durable-inventory]');
 return form&&!form.querySelector('[name=employee_name]').disabled&&document.getElementById(form.dataset.autosaveStatusId).textContent==='Saved to server';
});
async function scope(page){return page.locator('form[data-durable-inventory]').evaluate(form=>({kind:form.dataset.kind,id:form.dataset.recordId}));}
async function fields(page){return page.locator('form[data-durable-inventory]').evaluate(form=>{
 const values={};for(const c of form.querySelectorAll('input,select,textarea')){
  if(!c.name||['csrf_token','working_context'].includes(c.name)||c.disabled||(['checkbox','radio'].includes(c.type)&&!c.checked))continue;
  (values[c.name]??=[]).push(c.value);
 }return values;
});}
async function persisted(context,record){return (await context.request.get(`${local.url}/__autosave_test__/state?kind=${record.kind}&id=${record.id}`)).json();}
function compare(actual,expected){
 for(const [key,values]of Object.entries(expected)){
  const canonical=xs=>key.startsWith('qty')?xs.map(Number):key==='completed_task_ids'?[...xs].sort():xs;
  assert.deepEqual(canonical(actual.fields[key]),canonical(values),`persisted ${key}`);
 }
 assert.deepEqual(actual.fields.completed_task_ids||[],expected.completed_task_ids||[]);
}
async function instrument(context){
 await context.addInitScript(()=>{
  window.qualification={inputs:[],disabled:[],longTasks:[],fetches:[],blur:[]};
  new PerformanceObserver(list=>window.qualification.longTasks.push(...list.getEntries().map(e=>({start:e.startTime,ms:e.duration})))).observe({type:'longtask',buffered:true});
  document.addEventListener('focusout',e=>{if(e.target.closest('form[data-durable-inventory]'))window.qualification.blur.push({name:e.target.name,time:performance.now()});},true);
  document.addEventListener('input',e=>{
   if(!e.target.closest('form[data-durable-inventory]'))return;
   const sample={start:performance.now(),name:e.target.name,value:e.target.value};window.qualification.inputs.push(sample);
   queueMicrotask(()=>{sample.handlerMs=performance.now()-sample.start;});
   requestAnimationFrame(()=>{sample.frameMs=performance.now()-sample.start;});
  },true);
  new MutationObserver(entries=>{for(const e of entries)if(e.target.closest('form[data-durable-inventory]')&&e.target.disabled)window.qualification.disabled.push({name:e.target.name,start:performance.now()});}).observe(document,{attributes:true,subtree:true,attributeFilter:['disabled']});
  const original=window.fetch;window.fetch=async(...args)=>{
   const sample={url:String(args[0]),method:args[1]?.method||'GET',start:performance.now()};window.qualification.fetches.push(sample);
   try{const response=await original(...args);sample.ms=performance.now()-sample.start;sample.status=response.status;return response;}catch(e){sample.ms=performance.now()-sample.start;sample.error=String(e);throw e;}
  };
 });
}
async function session(cookies){
 const context=await browser.newContext();if(cookies)await context.addCookies(cookies);await instrument(context);
 const control={delay:0,fail:null,posts:[]};
 await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());if(url.hostname!=='127.0.0.1')return route.abort();
  if(request.method()!=='POST'||!url.pathname.match(/\/store\/[^/]+\/\d+\/(save|submit)$/))return route.continue();
  const entry={url:url.pathname,started:Date.now(),operation:request.headers()['x-draft-operation'],revision:request.headers()['x-draft-version'],body:JSON.stringify([...request.postData().matchAll(/name="([^"]+)"\r\n\r\n([\s\S]*?)\r\n--/g)].map(m=>[m[1],m[2]]).filter(([k])=>!['csrf_token','working_context'].includes(k)).sort())};control.posts.push(entry);
  const fail=url.pathname.endsWith('/submit')&&control.failFinalAfter?'after':control.fail;
  if(url.pathname.endsWith('/submit'))control.failFinalAfter=false;control.fail=null;
  if(fail==='before'){entry.failed='before';return route.abort('failed');}
  const response=await route.fetch();entry.status=response.status();
  if(control.delay)await sleep(control.delay);
  if(fail==='after'){entry.failed='after';return route.abort('failed');}
  await route.fulfill({response});entry.finished=Date.now();
 });
 const page=await context.newPage();page.on('pageerror',e=>result.errors.push(String(e)));
 page.on('dialog',d=>d.type()==='beforeunload'?d.accept():d.dismiss());
 if(!cookies){await page.goto(local.url+'/login');await page.locator('[name=username]').fill(local.username);await page.locator('[name=password]').fill(local.password);await page.locator('button[type=submit]').click();}
 return {context,page,control};
}
async function open(page,kind){
 await page.goto(local.url+'/store/'+kind);await page.waitForFunction(()=>{const f=document.querySelector('form[data-durable-inventory] [name=employee_name]');return f&&!f.disabled;});
 await page.evaluate(()=>{window.qualification={inputs:[],disabled:[],longTasks:[],fetches:[],blur:[]};});
}
async function waitPost(control,n){for(let i=0;i<300&&control.posts.length<n;i++)await sleep(10);assert.ok(control.posts.length>=n,'save initiated');}
async function run(){
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const login=await session();const cookies=await login.context.cookies();await login.context.close();
 for(const kind of ['change-box-count','daily-chore-sheet','non-sellable-stock-take']){
  let app=await session(cookies),{page,context,control}=app;await open(page,kind);const record=await scope(page);
  for(const delay of [0,250,1000]){
   control.delay=delay;const first=control.posts.length;await page.evaluate(()=>{window.qualification={inputs:[],disabled:[],longTasks:[],fetches:[],blur:[]};});
   const name=page.locator('form[data-durable-inventory] [name=employee_name]');
   await name.fill(`Synthetic ${kind} ${delay}`);await waitPost(control,first+1);
   await name.pressSequentially(' ABCDEFGHIJ',{delay:20});await saved(page);
   assert.equal(await name.evaluate(e=>document.activeElement===e),true,'focus stable');
   const expected=await fields(page);compare(await persisted(context,record),expected);
   const metrics=await page.evaluate(()=>window.qualification);assert.equal(metrics.disabled.length,0,'no background disabled transition');assert.equal(metrics.blur.length,0,'no background focus loss');
   assert.equal(metrics.inputs.length,12,'all accepted typing events recorded');
   assert.equal(control.posts.length-first,2,'one initial save and one coalesced follow-up');
   result.conditions.push({kind,delay,metrics,posts:control.posts.slice(first),persisted:true,focusStable:true});
  }
  control.delay=250;
  // Rapid typing does not produce per-keystroke POSTs; sequential fields are included.
  const before=control.posts.length,name=page.locator('form[data-durable-inventory] [name=employee_name]');
  await name.fill('');await name.pressSequentially('Rapid continuous sequential entry',{delay:15});assert.equal(control.posts.length,before);
  const quantities=page.locator('form[data-durable-inventory] input[type=number]:enabled');
  if(kind==='daily-chore-sheet'){
   await page.locator('[name=completed_task_ids]').nth(0).check();await sleep(300);await page.locator('[name=completed_task_ids]').nth(1).check();
  }else{await quantities.nth(0).fill('2');await sleep(300);await quantities.nth(1).fill('3');}
  await saved(page);compare(await persisted(context,record),await fields(page));
  // Failed required save must not submit; retry old immutable command then latest values.
  control.delay=1000;control.fail='before';const failedStart=control.posts.length;
  await name.fill('Required save failure');await page.locator('form[data-durable-inventory] button[formaction$="/submit"]').click();
  await page.waitForFunction(()=>[...document.querySelectorAll('.autosave-status')].some(e=>e.textContent.includes('Submission stopped')));
  assert.equal(control.posts.slice(failedStart).some(x=>x.url.endsWith('/submit')),false);assert.equal(await name.isEnabled(),true);
  await name.fill('Recovered after failure');await page.getByRole('button',{name:'Save / Retry protected work',exact:true}).click();await saved(page);
  assert.equal(control.posts[failedStart].operation,control.posts[failedStart+1].operation);assert.equal(control.posts[failedStart].body,control.posts[failedStart+1].body);
  compare(await persisted(context,record),await fields(page));
  // Lost response after commit, newer edit, refresh: recover old command and newer fields.
  control.delay=0;control.fail='after';const lostStart=control.posts.length;
  await name.fill('Old pending snapshot');await waitPost(control,lostStart+1);
  await page.waitForFunction(()=>[...document.querySelectorAll('.autosave-status')].some(e=>e.textContent.includes('Failed to fetch')));
  await name.fill('Newer protected before refresh');await sleep(100);await page.reload();
  await saved(page);assert.equal(await page.locator('[name=employee_name]').inputValue(),'Newer protected before refresh');
  assert.equal(control.posts[lostStart].operation,control.posts[lostStart+1].operation);compare(await persisted(context,record),await fields(page));
  // Real second-tab revision conflict: authoritative server winner remains intact.
  const tab=await context.newPage();tab.on('dialog',d=>d.accept());await open(tab,kind);
  await tab.locator('[name=employee_name]').fill('Second tab authoritative winner');await saved(tab);
  await page.locator('[name=employee_name]').fill('First tab retained conflict');
  await page.waitForFunction(()=>[...document.querySelectorAll('.autosave-status')].some(e=>e.textContent.includes('changed elsewhere')));
  assert.equal((await persisted(context,record)).fields.employee_name[0],'Second tab authoritative winner');assert.equal(await page.locator('[name=employee_name]').isDisabled(),true);
  const retained=await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('inventory-draft:v2:')).length);assert.ok(retained>0);
  await tab.close();await context.close();
  // Separate clean device sees server winner; Submit during autosave drains newer edits once.
  app=await session(cookies);({page,context,control}=app);await open(page,kind);control.delay=1000;
  await page.locator('[name=employee_name]').fill('Draft before final');await waitPost(control,1);
  await page.locator('[name=employee_name]').pressSequentially(' with newer edits',{delay:15});
  const expected=await fields(page);control.failFinalAfter=true;await page.locator('form[data-durable-inventory] button[formaction$="/submit"]').click();
  // A duplicate click dispatched while controls are final-locked must not enqueue another operation.
  await page.locator('form[data-durable-inventory] button[formaction$="/submit"]').evaluate(e=>e.click());
  await page.waitForFunction(()=>[...document.querySelectorAll('.autosave-status')].some(e=>e.textContent.includes('Submission unconfirmed')));
  assert.equal(await page.locator('[name=employee_name]').isDisabled(),true);await page.getByRole('button',{name:'Save / Retry protected work',exact:true}).click();
  await page.waitForURL(/\/resume$/);await page.getByText('Work successfully submitted',{exact:false}).waitFor();
  const actual=await persisted(context,record);compare(actual,expected);assert.equal(actual.status,'SUBMITTED');
  const submitPosts=control.posts.filter(x=>x.url.endsWith('/submit'));assert.equal(submitPosts.length,1);
  const submitAudits=Object.entries(actual.audit_counts).filter(([key])=>key.endsWith('_SUBMITTED'));assert.equal(submitAudits.length,1);assert.equal(submitAudits[0][1],1);
  await page.reload();await page.getByText('Work successfully submitted',{exact:false}).waitFor();assert.equal(control.posts.filter(x=>x.url.endsWith('/submit')).length,1);
  result.integrity.push({kind,continuousAndSequential:true,failedRequiredSaveBlockedSubmit:true,stableRetry:true,refreshWithPendingAndNewerEdits:true,conflictPreservedWinner:true,protectedConflictRecords:retained,queuedSubmit:true,oneSubmitAudit:true,lostFinalResponseRecoveredByReceipt:true,receiptRefresh:true,final:actual,posts:control.posts});
  await context.close();console.log('qualified '+kind);
 }
 assert.deepEqual(result.errors,[]);await browser.close();
}
run().then(()=>{fs.writeFileSync(path.join(output,'browser.json'),JSON.stringify(result,null,2));console.log('all browser scenarios passed');}).catch(async e=>{
 result.failure=String(e.stack);fs.writeFileSync(path.join(output,'browser-failure.json'),JSON.stringify(result,null,2));console.error(e);await browser?.close();process.exitCode=1;
});
