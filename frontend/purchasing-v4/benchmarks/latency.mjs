import {chromium} from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import {createHash} from 'node:crypto';
const fixtureRows=Number(process.env.V4_BENCH_ROWS??2000),fixtureCatalog=Number(process.env.V4_BENCH_CATALOG??20000),fixtureVendors=Number(process.env.V4_BENCH_VENDORS??30),baseUrl=process.env.V4_BENCH_URL??'http://127.0.0.1:58441';
const out=process.env.V4_BENCH_OUTPUT;if(!out)throw Error('Explicit V4_BENCH_OUTPUT required');
const browser=await chromium.launch({headless:process.env.V4_HEADED!=='1'}),context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1}),page=await context.newPage();
if(process.env.V4_WORKSHEET_ID)await context.addInitScript(id=>localStorage.setItem('v4-current:1',id),process.env.V4_WORKSHEET_ID);await page.goto(baseUrl+'/');await page.getByRole('textbox',{name:'Username',exact:true}).fill('v4-fixture');await page.getByLabel('Password',{exact:true}).fill('FixturePhase4!2026');await page.getByRole('button',{name:'Sign in',exact:true}).click();await page.locator('#matching-count').filter({hasText:fixtureRows.toLocaleString('en-US')}).waitFor();
await page.locator('#inventory-settings').click();await page.locator('#inventory-age').fill('86400');const inventoryReady=page.waitForResponse(r=>r.url().includes('/inventory?max_age_seconds=86400'));await page.locator('#dialog-apply').click();await inventoryReady;await page.waitForFunction(()=>document.querySelector('#inventory-settings')?.getAttribute('title')?.includes('maximum observation age'));
await page.locator('#vendors').click();await page.locator('#all-vendors').check();await page.locator('#dialog-apply').click();await page.locator('#search').fill('');
await page.evaluate(()=>{window.__longTasks=[];try{new PerformanceObserver(list=>window.__longTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:true});}catch{}});
await page.waitForFunction(()=>performance.getEntriesByName('v4-first-grid-frame').length>0);
const assets=JSON.parse(await fs.readFile(new URL('../../../app/purchasing_v4/worksheet_static/assets.json',import.meta.url),'utf8'));const assetHashes={};for(const file of Object.values(assets))assetHashes[file]=createHash('sha256').update(await fs.readFile(new URL('../../../app/purchasing_v4/worksheet_static/'+file,import.meta.url))).digest('hex');
const navigation=await page.evaluate(()=>({browser:navigator.userAgent,initial_dom_ready_ms:performance.now(),client_to_painted_grid_ms:performance.getEntriesByName('v4-first-grid-frame').at(-1).startTime-performance.getEntriesByName('v4-client-start')[0].startTime,state_hydration_to_painted_grid_ms:performance.getEntriesByName('v4-first-grid-frame').at(-1).startTime-performance.getEntriesByName('v4-state-start').at(-1).startTime,mounted_rows:document.querySelectorAll('.product-row').length,nodes:document.querySelectorAll('*').length,heap:performance.memory?.usedJSHeapSize,resources:performance.getEntriesByType('resource').filter(e=>e.name.includes('/worksheet-assets/')).map(e=>({url:e.name,bytes:e.transferSize,duration:e.duration}))}));
// Two animation frames cover the frame containing the changed DOM. No state-store test hooks are used.
async function measure(operation,index){return await page.evaluate(async({operation,index})=>{const start=performance.now();const input=document.querySelector('[data-col="0"]');const click=id=>document.querySelector(id).click();
 if(operation==='quantity_edit'||operation==='row_total_update'){input.focus();input.value=String(1+index%30);input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:input.value}));}
 else if(operation==='keyboard_navigation'){document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}));}
 else if(operation==='select_100'){click('#clear-selection');click('#select-100');}
 else if(operation==='search_filter'){const field=document.querySelector('#search');field.value=['Geek Pulse','Mint','RAZ','Blue Razz',''][index%5];field.dispatchEvent(new Event('input',{bubbles:true}));}
 else if(operation==='supplier_switch'){const all=document.querySelector('#all-vendors');all.checked=false;const boxes=[...document.querySelectorAll('[data-visible-vendor]')];boxes.forEach((box,i)=>box.checked=i===index%2);click('#dialog-apply');}
 else if(operation==='bulk_vendor'){click('#dialog-apply');}
 else if(operation==='undo'){click('#undo');}
 else if(operation==='redo'){click('#redo');}
 else if(operation==='paste_100x4'){const text=Array(100).fill(`${1+index%3}\t2\t3\t4`).join('\n');const event=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(event,'clipboardData',{value:{getData:()=>text}});input.dispatchEvent(event);}
 else if(operation==='scroll'){const v=document.querySelector('#viewport');v.scrollTop=(index*340)%5000;v.dispatchEvent(new Event('scroll'));}
 else if(operation==='copy'){const event=new Event('copy',{bubbles:true,cancelable:true});Object.defineProperty(event,'clipboardData',{value:{setData:(type,text)=>{window.__copied=text;}}});input.dispatchEvent(event);}
 return await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({milliseconds:performance.now()-start,row_total:document.querySelector('.row-total')?.textContent,selected:document.querySelector('#selection-count').textContent,save:document.querySelector('#save-status').textContent,mounted_rows:document.querySelectorAll('.product-row').length}))));},{operation,index});}
if(!navigation.resources.some(r=>r.url.endsWith('/'+assets.js)))throw Error('Loaded browser bundle did not match the frozen build');
const scenarios={};
for(const operation of ['quantity_edit','row_total_update','keyboard_navigation','select_100','search_filter','supplier_switch','bulk_vendor','undo','redo','paste_100x4','copy','scroll']){
 const samples=[];
 if(operation==='keyboard_navigation')await page.locator('[data-col="0"]').first().focus();
 if(['bulk_vendor','paste_100x4','copy'].includes(operation)){await page.locator('#search').fill('');await page.locator('#vendors').click();await page.locator('#all-vendors').check();await page.locator('#dialog-apply').click();await page.locator('#viewport').evaluate(v=>v.scrollTop=0);}
 if(operation==='bulk_vendor'){await page.locator('#select-100').click();await page.locator('#bulk-action').selectOption('vendor');}
 const iterations=['bulk_vendor','supplier_switch','quantity_edit','row_total_update','select_100','search_filter'].includes(operation)?210:100;
 for(let i=0;i<iterations;i++){
  if(operation==='supplier_switch')await page.locator('#vendors').click();
  if(operation==='bulk_vendor'){await page.locator('#apply-bulk').click();await page.locator('#new-vendor').selectOption({label:i%2?'Atlas Trade Supply':'Zephyr Imports'});}
  const result=await measure(operation,i);if(operation==='select_100'&&!result.selected.startsWith('100 selected'))throw Error('Select100 benchmark did not select exactly 100 rows');if(i>=10)samples.push(result);
 }
 scenarios[operation]=samples;await fs.writeFile(out+'/latency-progress.json',JSON.stringify({completed:Object.keys(scenarios)}));
}
await page.locator('[data-col="0"]').first().press('Tab');await page.waitForFunction(()=>document.querySelector('#save-status').textContent==='Saved',{timeout:60000});
const long=await page.evaluate(()=>({long_tasks:window.__longTasks,heap:performance.memory?.usedJSHeapSize,mounted_rows:document.querySelectorAll('.product-row').length,nodes:document.querySelectorAll('*').length}));
const targets={quantity_edit:50,row_total_update:50,select_100:100,bulk_vendor:150,search_filter:100,supplier_switch:150};const summary={};
for(const [name,samples] of Object.entries(scenarios)){const sorted=samples.map(s=>s.milliseconds).sort((a,b)=>a-b);summary[name]={samples:sorted.length,median_ms:sorted[Math.ceil(sorted.length*.5)-1],p95_ms:sorted[Math.ceil(sorted.length*.95)-1],p99_ms:sorted[Math.ceil(sorted.length*.99)-1],max_ms:sorted.at(-1),target_ms:targets[name]??null,absolute_pass:targets[name]?sorted.at(-1)<targets[name]:null,p95_pass:targets[name]?sorted[Math.ceil(sorted.length*.95)-1]<targets[name]:null};}
await page.locator('#search').fill('Geek Pulse');await page.screenshot({path:out+'/desktop-grid-search.png'});await page.locator('#search').fill('');await page.locator('#viewport').evaluate(v=>v.scrollTop=0);await page.locator('#vendors').click();await page.locator('#all-vendors').check();await page.locator('#dialog-apply').click();await page.screenshot({path:out+'/desktop-grid.png'});
await fs.writeFile(out+'/browser-latency.json',JSON.stringify({build:{assets,sha256:assetHashes,browser_version:browser.version()},fixture:{rows:fixtureRows,catalog:fixtureCatalog,vendors:fixtureVendors,stores:4},hardware:{model:os.cpus()[0].model,logical_cpus:os.cpus().length,ram_bytes:os.totalmem(),os:os.release()},headless:process.env.V4_HEADED!=='1',viewport:{width:1440,height:900},protocol:'10 warmup actions excluded per scenario; event handler through second animation frame; raw maxima gate all six exact Addendum A targets, with p95/p99 separately reported. Browser launch/login/asset loading are separate from interaction timings.',navigation,summary,raw:scenarios,after:long},null,2));
await browser.close();console.log(JSON.stringify(summary,null,2));
