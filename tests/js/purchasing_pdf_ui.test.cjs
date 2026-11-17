const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const node=process.execPath,frontend=path.resolve('frontend/purchasing-v4');
const pinnedPython='/Users/justinrawlinson/.pyenv/versions/3.10.13/bin/python';
const python=process.env.PYTHON??(fs.existsSync(pinnedPython)?pinnedPython:'python3');
test('strict TypeScript check and deployed frontend bundle',()=>{
 for(const args of [['node_modules/typescript/bin/tsc','--noEmit'],['build.mjs']]){
  const r=cp.spawnSync(node,args,{cwd:frontend,encoding:'utf8'});assert.equal(r.status,0,r.stdout+r.stderr);
 }
});
test('V4 chooser, saved revisions, browser printing and responsive preview',async()=>{
 const {chromium}=require('../../frontend/purchasing-v4/node_modules/playwright');
 const esbuild=require('../../frontend/purchasing-v4/node_modules/esbuild');
 const entry=[
 "import {openPurchaseDocuments} from './src/purchase-documents.ts';",
 "const rows=[{supplier_id:'vendor-A',offers:{'vendor-A':{name:'Atlas Supply'}},packs:[1,0,0,0],pack_size:5,removed:false},{supplier_id:'vendor-B',offers:{'vendor-B':{name:'Boreal Imports'}},packs:[null,0,0,0],pack_size:5,removed:false},{supplier_id:'vendor-A',offers:{'vendor-A':{name:'Atlas Supply'}},packs:[1,0,0,0],pack_size:5,removed:false,sealed:{purchase_order_id:901}}];",
 "window.readyCount=0;window.pendingResolve=null;document.body.innerHTML='<button id=\"open\">Print / PDF</button>';",
 "document.querySelector('#open').onclick=()=>openPurchaseDocuments({rows:()=>rows,session:()=>({id:'worksheet-1',version:7}),ready:()=>{window.readyCount++;return new Promise(resolve=>window.pendingResolve=resolve);}});"
 ].join('\n');
 const bundle=await esbuild.build({stdin:{contents:entry,resolveDir:frontend,loader:'ts'},bundle:true,write:false,format:'iife'});
 const browser=await chromium.launch({headless:true,args:['--single-process','--no-zygote','--disable-gpu','--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.context().route('**/*',r=>r.fulfill({contentType:'text/html',body:'<!doctype html><body><script>'+bundle.outputFiles[0].text+'</script>'}));
  await page.goto('https://v4-pdf.test/');await page.locator('#open').click();
  assert.equal(await page.locator('[data-document-supplier]').count(),2);
  assert(await page.locator('[data-document-supplier=\"vendor-B\"]').isDisabled());
  assert.equal(await page.getByRole('link',{name:/PO #901/}).getAttribute('href'),'/api/purchasing-v4/orders/901/documents/preview');
  const popupEvent=page.waitForEvent('popup');await page.locator('[data-document-supplier="vendor-A"]').click();const popup=await popupEvent;
  assert.equal(await page.evaluate(()=>window.readyCount),1);assert.equal(popup.url(),'about:blank');
  await page.evaluate(()=>window.pendingResolve());await popup.waitForURL('**/preview?version=7');
  assert.match(popup.url(),/worksheet-1\/purchase-documents\/vendor-A\/preview\?version=7$/);await popup.close();
  const preview=cp.spawnSync(python,['-B','-c',"import sys;sys.path.insert(0,'tests');from test_purchasing_v4_purchase_documents import example;from app.purchasing_v4 import purchase_documents as d;print(d.html_preview(d.finish(example(85)),'/api/purchasing-v4/orders/901/documents/pdf'))"],{encoding:'utf8'});
  assert.equal(preview.status,0,preview.stdout+preview.stderr);
  const html=preview.stdout;
  await page.context().unroute('**/*');let calls=[];await page.route('**/*',r=>{calls.push({method:r.request().method(),url:r.request().url()});return r.fulfill({contentType:'text/html',body:html});});
  await page.goto('https://v4-pdf.test/preview');
  await page.evaluate(()=>{window.printCalls=0;window.print=()=>{window.printCalls++;};});
  await page.getByRole('button',{name:'Print',exact:true}).click();assert.equal(await page.evaluate(()=>window.printCalls),1);
  assert(calls.every(c=>c.method==='GET'));
  assert.match(await page.getByRole('link',{name:'Download PDF'}).getAttribute('href'),/download=true$/);
  assert.equal(await page.getByRole('link',{name:'Preview PDF'}).getAttribute('target'),'_blank');
  assert.equal(await page.locator('tbody tr').count(),85);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('nav').isVisible(),false);
  assert.equal(await page.locator('tbody tr').first().evaluate(n=>getComputedStyle(n).breakInside),'avoid');
  await page.pdf({path:path.join(process.env.TMPDIR,'v4-native-print.pdf'),preferCSSPageSize:true,printBackground:true});
  const parsed=cp.spawnSync(python,['-B','-c',"import sys,re;sys.path.insert(0,'tests');from pdf_test_reader import PdfReader;pages=[p.extract_text() for p in PdfReader(sys.argv[1]).pages];assert len(pages)>1;[None if any(re.search(r'SKU-'+str(i)+r'(?![0-9])',p) and re.search(r'Variation '+str(i)+r'(?![0-9])',p) for p in pages) else (_ for _ in ()).throw(AssertionError(i)) for i in range(85)]",path.join(process.env.TMPDIR,'v4-native-print.pdf')],{encoding:'utf8'});
  assert.equal(parsed.status,0,parsed.stdout+parsed.stderr);
  await page.emulateMedia({media:'screen'});await page.screenshot({path:'docs/development/v4-pdf-preview-desktop.png',fullPage:false});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.getByRole('button',{name:'Print',exact:true}).isVisible(),true);
  await page.screenshot({path:'docs/development/v4-pdf-preview-mobile.png',fullPage:false});assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
