import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as adminHelpers from '../app/static/v2/flavor-explorer/shared-admin.mjs';
import {
 classification,changedFields,normalizedNote,esc,diffMarkup,previewMarkup,
 cleanName,FieldEditor,fieldValue,
} from '../app/static/v2/flavor-explorer/shared-admin.mjs';

const taxonomy={
 notes:[{slug:'green-apple',label:'Green Apple'},{slug:'strawberry-kiwi',label:'Strawberry / Kiwi'}],
 categories:[{id:12,slug:'fruit',name:'Fruit'}],
 sensoryAxes:[{slug:'cooling',label:'Cooling'},{slug:'tartness',label:'Tartness'}],
};

test('canonical editor state preserves explicit zero and keeps unknown sensory axes sparse',()=>{
 const original={
  description:'',notes:['green-apple','green-apple'],categories:[12,12],
  sensory:{cooling:0,tartness:null,creaminess:undefined,richness:false,brightness:'0.8'},
 };
 const before=JSON.stringify(original);
 assert.deepEqual(classification(original),{
  description:'',notes:['green-apple'],categories:[12],sensory:{cooling:0},similarities:[],
 });
 assert.equal(JSON.stringify(original),before);
 assert.deepEqual(classification(),{description:'',notes:[],categories:[],sensory:{},similarities:[]});
 assert.equal(fieldValue('sensory',{cooling:0},taxonomy),'Cooling: None · 0%');
 assert.equal(fieldValue('sensory',{tartness:.37},taxonomy),'Tartness: Light · 37%');
 assert.equal(fieldValue('sensory',{tartness:null},taxonomy),'Tartness: Unclassified');
 const intensityDiff=diffMarkup({sensory:{tartness:.8}},{sensory:{tartness:1}},taxonomy);
 assert.match(intensityDiff,/Intense · 80%/);
 assert.match(intensityDiff,/Intense · 100%/);
 assert.equal(fieldValue('sensory',{},taxonomy),'Unclassified');
});

test('employee patches contain only changed axes, with null for an explicit clear',()=>{
 const before={description:'Published description',notes:['green-apple'],categories:[12],
  sensory:{cooling:0,tartness:.4,creaminess:.6},similarities:[]};
 const after={...before,sensory:{cooling:0,tartness:.8,brightness:0}};
 assert.deepEqual(changedFields(before,after),{
  sensory:{tartness:.8,creaminess:null,brightness:0},
 });
 assert.deepEqual(changedFields(before,{...before,sensory:{creaminess:.6,tartness:.4,cooling:0}}),{});
 assert.deepEqual(before.sensory,{cooling:0,tartness:.4,creaminess:.6});
});

test('an untouched reviewed sensory value is not rounded by the five-step editor display',()=>{
 const slider={dataset:{originalValue:'.37'},value:'2'};
 const checkbox={checked:true,dataset:{axisKnown:'tartness'}};
 const host={
  querySelectorAll(selector){
   if(selector==='[data-axis-known]')return [checkbox];
   if(selector==='[data-field="notes"]:checked')return [{value:'green-apple'}];
   if(selector==='[data-field="categories"]:checked')return [{value:'12'}];
   return [];
  },
  querySelector(selector){
   if(selector==='[data-description]')return {value:'Existing description'};
   if(selector.startsWith('[data-axis='))return slider;
   throw new Error('Unexpected editor control '+selector);
  },
 };
 const previous=globalThis.CSS;
 globalThis.CSS={escape:value=>value};
 try{
  const editor={element:host,options:{},value:{similarities:[]},captureRelations(){}};
  assert.equal(FieldEditor.prototype.read.call(editor).sensory.tartness,.37);
  slider.dataset.changed='true';slider.value='4';
  assert.equal(FieldEditor.prototype.read.call(editor).sensory.tartness,.8);
  checkbox.checked=false;
  assert.deepEqual(FieldEditor.prototype.read.call(editor).sensory,{});
 }finally{if(previous===undefined)delete globalThis.CSS;else globalThis.CSS=previous}
});

test('custom flavor notes reuse recovered names and slugs without mutating the dictionary',()=>{
 const before=JSON.stringify(taxonomy);
 assert.deepEqual(normalizedNote('GREEN APPLE',taxonomy),{slug:'green-apple',label:'Green Apple'});
 assert.deepEqual(normalizedNote('green-apple',taxonomy),{slug:'green-apple',label:'Green Apple'});
 assert.deepEqual(normalizedNote('Green_apple',taxonomy),{slug:'green-apple',label:'Green Apple'});
 assert.deepEqual(normalizedNote('Strawberry / Kiwi',taxonomy),{slug:'strawberry-kiwi',label:'Strawberry / Kiwi'});
 assert.deepEqual(normalizedNote('Crème Brûlée',taxonomy),{slug:'creme-brulee',label:'Crème Brûlée'});
 assert.throws(()=>normalizedNote('!!!',taxonomy),/letters or numbers/);
 assert.throws(()=>normalizedNote('x'.repeat(81),taxonomy),/1 to 80/);
 assert.equal(JSON.stringify(taxonomy),before);
});

test('classification diffs and preview details escape hostile product and proposal text',()=>{
 const hostile='</textarea><script>alert("x")</script><img src=x onerror=alert(1)>';
 assert.equal(esc('<>&"\''),'&lt;&gt;&amp;&quot;&#39;');
 const diff=diffMarkup({}, {description:hostile,notes:['green-apple']},taxonomy);
 assert.doesNotMatch(diff,/<script|<img|<\/textarea>/i);
 assert.match(diff,/&lt;script&gt;/);
 const preview=previewMarkup({
  changedCount:1,unchangedCount:0,products:[{id:'square-x',name:hostile,before:{},after:{description:hostile}}],
  warnings:[hostile],
 },taxonomy);
 assert.doesNotMatch(preview,/<script|<img|<\/textarea>/i);
 assert.match(preview,/&lt;script&gt;/);
});

test('catalog name cleanup uses literal asterisks and whitespace rather than double-escaped patterns',()=>{
 assert.equal(cleanName('** Killa Fruits · *Blue Raspberry'),'Killa Fruits · Blue Raspberry');
 assert.equal(cleanName('Coastal Clouds · **Strawberry'),'Coastal Clouds · Strawberry');
 assert.equal(cleanName('Green Apple'),'Green Apple');
});


// Exercise the real controller with minimal DOM surfaces, without a browser or network.
function queueController(request){
 const nodes=new Map();
 const element=selector=>{
  if(!nodes.has(selector))nodes.set(selector,{
   innerHTML:'',textContent:'',value:'',hidden:false,
   addEventListener(){},
   querySelectorAll(){return []},
   querySelector(child){return element(selector+' '+child)},
  });
  return nodes.get(selector);
 };
 const document={querySelector:element,querySelectorAll(){return []}};
 const source=readFileSync(new URL('../app/static/v2/flavor-explorer/admin.mjs',import.meta.url),'utf8');
 const entry=source.match(/^import\{([^}]+)\}from[^;]+;\n/);
 assert.ok(entry,'admin controller has the expected shared-helper import');
 const names=entry[1].split(',').map(name=>name.trim());
 const body=source.slice(entry[0].length).replace(/\nstart\(\);\s*$/,'');
 const create=new Function('document',...names,body+'\nreturn {state,start,loadActivity,loadGroups,openGroup,renderReviewSelection,showHistoryVersion,setBoot:value=>{boot=value}};');
 const controller=create(document,...names.map(name=>name==='api'?request:adminHelpers[name]));
 controller.setBoot({taxonomy});
 return {controller,element};
}
const queuedGroup=(id,status,name)=>({
 id,status,baseVersionId:1,productId:'square-'+id,product:{name},
 current:{description:'Current published classification'},
 proposed:{description:'Original employee proposal'},
 submissions:[{id:id*10,employee:'Preview employee',store:'Preview store',comment:'Review the submitted proposal'}],
});

test('approved Activity drilldown includes modified approvals and displays each actual decision',async()=>{
 const requests=[],groups=[
  queuedGroup(1,'APPROVED','Directly approved product'),
  queuedGroup(2,'MODIFIED_APPROVED','Modified approval product'),
 ];
 const {controller,element}=queueController(async path=>{
  requests.push(path);
  if(path==='/analytics')return {counts:{approvedSubmissions:2,decidedSubmissions:2,approvalRate:1}};
  assert.equal(path,'/suggestions?status=approved');
  return {groups};
 });
 controller.state.tab='activity';
 await controller.loadActivity();
 const activity=element('#fi-activity'),match=activity.innerHTML.match(/data-drill-tab="suggestions" data-drill-filter="([^"]+)"><span>Approved suggestions/);
 assert.ok(match,'approved count is a drillable figure');
 assert.equal(match[1],'approved');
 activity.onclick({target:{closest:selector=>selector==='[data-drill-tab]'?{
  dataset:{drillTab:'suggestions',drillFilter:match[1]},
 }:null}});
 await new Promise(setImmediate);
 assert.equal(controller.state.queueStatus,'approved');
 assert.equal(element('#fi-queue-status').value,'approved');
 assert.match(element('#fi-panel').innerHTML,/<option value="approved">Approved \(including modified\)<\/option>/);
 assert.match(element('#fi-panel').innerHTML,/<option value="APPROVED">Approved without modification<\/option>/);
 assert.match(element('#fi-groups').innerHTML,/Directly approved product/);
 assert.match(element('#fi-groups').innerHTML,/Modified approval product/);
 assert.match(element('#fi-groups').innerHTML,/Modified & approved/);
 assert.equal(requests.at(-1),'/suggestions?status=approved');
 controller.openGroup(2);
 assert.match(element('#fi-group-detail').innerHTML,/Modified &amp; approved/);
 assert.match(element('#fi-group-detail').innerHTML,/submitted proposal against the current published classification/);
 assert.doesNotMatch(element('#fi-group-detail').innerHTML,/data-review-decision/);
});

test('a late queue response cannot replace a newer status or refresh result',async()=>{
 const requests=[];
 const {controller,element}=queueController(path=>new Promise(resolve=>requests.push({path,resolve})));
 controller.state.tab='suggestions';
 controller.state.queueStatus='PENDING';
 const pending=controller.loadGroups();
 controller.state.queueStatus='approved';
 const olderApproval=controller.loadGroups();
 const latestApproval=controller.loadGroups();
 requests[2].resolve({groups:[queuedGroup(3,'MODIFIED_APPROVED','Newest approved result')]});
 await latestApproval;
 assert.match(element('#fi-groups').innerHTML,/Newest approved result/);
 requests[1].resolve({groups:[queuedGroup(2,'APPROVED','Stale approved result')]});
 await olderApproval;
 requests[0].resolve({groups:[queuedGroup(1,'PENDING','Stale pending result')]});
 await pending;
 assert.equal(controller.state.queueStatus,'approved');
 assert.deepEqual(controller.state.groups.map(group=>group.id),[3]);
 assert.match(element('#fi-groups').innerHTML,/Newest approved result/);
 assert.doesNotMatch(element('#fi-groups').innerHTML,/Stale approved result|Stale pending result/);
 assert.deepEqual(requests.map(row=>row.path),[
  '/suggestions?status=PENDING','/suggestions?status=approved','/suggestions?status=approved',
 ]);
});


test('an unavailable catalog group exposes retained identities and read-only history while keeping rejection',async()=>{
 const group=queuedGroup(9,'PENDING','Previously catalogued product');
 group.productId='unavailable-flavor-91';group.catalogUnavailable=true;
 group.product={id:group.productId,name:'Previously catalogued product',brand:'Catalog unavailable',
  catalogUnavailable:true,historyReadOnly:true,squareItemIds:['ITEM-STABLE-91'],
  squareVariationIds:['VAR-<script>alert(1)</script>']};
 const requests=[];
 const {controller,element}=queueController(async path=>{
  requests.push(path);
  if(path==='/suggestions?status=PENDING')return {groups:[group]};
  assert.equal(path,'/products/unavailable-flavor-91/history');
  return {product:group.product,versions:[{id:501,version:1,snapshot:{description:'Retained approved description'}}],
   operations:[{id:'op-archived',kind:'bulk'}]};
 });
 controller.state.tab='suggestions';controller.state.queueSearch='ITEM-STABLE-91';
 await controller.loadGroups();
 assert.match(element('#fi-groups').innerHTML,/Catalog unavailable/);
 assert.match(element('#fi-groups').innerHTML,/ITEM-STABLE-91/);
 controller.openGroup(9);
 const detail=element('#fi-group-detail').innerHTML;
 assert.match(detail,/Square item IDs/);assert.match(detail,/ITEM-STABLE-91/);
 assert.match(detail,/Square variation IDs/);assert.match(detail,/VAR-&lt;script&gt;/);
 assert.doesNotMatch(detail,/<script>/);
 assert.match(detail,/data-review-decision="REJECTED"/);
 assert.doesNotMatch(detail,/data-review-decision="APPROVED"|id="fi-modify-review"/);
 controller.state.reviewSelected.add('9');controller.renderReviewSelection();
 assert.match(element('#fi-queue-selection').innerHTML,/data-batch-review="APPROVED" disabled/);
 assert.match(element('#fi-queue-selection').innerHTML,/data-batch-review="REJECTED">Reject selected/);
 element('#fi-group-history').onclick();
 await new Promise(setImmediate);
 assert.equal(controller.state.tab,'history');
 assert.equal(controller.state.historyId,'unavailable-flavor-91');
 assert.equal(element('#fi-history-product').value,'unavailable-flavor-91');
 assert.match(element('#fi-panel').innerHTML,/<option value="unavailable-flavor-91">/);
 assert.equal(controller.state.all.length,0,'retained records never enter the selectable catalog');
 assert.equal(requests.at(-1),'/products/unavailable-flavor-91/history');
 assert.doesNotMatch(element('#fi-history-list').innerHTML,/data-reverse-operation/);
 controller.showHistoryVersion(501);
 assert.match(element('#fi-history-detail').innerHTML,/Retained approved description/);
 assert.match(element('#fi-history-detail').innerHTML,/Read-only history/);
 assert.doesNotMatch(element('#fi-history-detail').innerHTML,/fi-preview-restore/);
});


function staffController(request){
 const nodes=new Map();
 const element=selector=>{
  if(!nodes.has(selector))nodes.set(selector,{
   innerHTML:'',textContent:'',value:'',hidden:false,
   focus(){this.focused=true},scrollIntoView(){},
   querySelector(child){return element(selector+' '+child)},
   querySelectorAll(){return []},
  });
  return nodes.get(selector);
 };
 const document={querySelector:element,querySelectorAll(){return []}};
 class FieldEditorStub{
  constructor(_element,_taxonomy,_products,value){this.set(value)}
  read(){return classification(this.value)}
  set(value){this.value=classification(value)}
 }
 const source=readFileSync(new URL('../app/static/v2/flavor-explorer/staff.mjs',import.meta.url),'utf8');
 const entry=source.match(/^import\{([^}]+)\}from[^;]+;\n/);
 assert.ok(entry,'staff controller has the expected shared-helper import');
 const names=entry[1].split(',').map(name=>name.trim());
 const body=source.slice(entry[0].length).replace(/\nstart\(\);\s*$/,'');
 const create=new Function('document','CSS',...names,body+'\nreturn {choose,search,backToProducts,submit,drafts,get selected(){return selected},get editor(){return editor},setBoot:value=>{boot=value}};');
 const controller=create(document,{escape:value=>value},...names.map(name=>name==='api'?request:name==='FieldEditor'?FieldEditorStub:adminHelpers[name]));
 controller.setBoot({taxonomy});
 return {controller,element};
}
const staffProduct=(id,version)=>({
 id,name:'Product '+id,brand:'Preview brand',flavorId:id==='A'?1:2,
 publishedVersionId:version,classificationFingerprint:id+'-fingerprint-'+version,
 classification:classification({description:'Published '+id+' version '+version}),
});

test('a submitted product resolving after Back cannot erase another product draft or comment',async()=>{
 let finishSubmission,submitted;
 const {controller,element}=staffController(async(path,options)=>{
  if(path==='/products/A')return staffProduct('A',1);
  if(path==='/products/B')return staffProduct('B',1);
  if(path==='/products/A/suggest'){
   submitted=options.body;
   return new Promise(resolve=>{finishSubmission=resolve});
  }
  assert.equal(path,'/my-suggestions');return {items:[]};
 });
 await controller.choose('A');
 controller.editor.set({description:'Employee correction for A'});
 element('#fi-staff-comment').value='A review comment';
 const submission=controller.submit();
 controller.backToProducts();
 await controller.choose('B');
 controller.editor.set({description:'Unsubmitted correction for B',sensory:{tartness:0}});
 element('#fi-staff-comment').value='Keep the B comment';
 controller.backToProducts();
 await controller.choose('B');
 const expected=controller.editor.read();
 finishSubmission({submissionId:77});
 await submission;
 assert.equal(submitted.baseFingerprint,'A-fingerprint-1');
 assert.equal(submitted.comment,'A review comment');
 assert.equal(controller.selected.id,'B');
 assert.deepEqual(controller.editor.read(),expected);
 assert.equal(element('#fi-staff-comment').value,'Keep the B comment');
 assert.deepEqual(controller.drafts.get('B').value,expected);
 assert.equal(controller.drafts.get('B').comment,'Keep the B comment');
 assert.equal(controller.drafts.has('A'),false,'only the successfully submitted A snapshot is retired');
 assert.equal(element('#fi-staff-editor').hidden,false);
});

test('a late search cannot reopen results over an editor and restored drafts keep their original baseline',async()=>{
 let finishSearch,current=staffProduct('B',1);
 const {controller,element}=staffController(async path=>{
  if(path.startsWith('/products?'))return new Promise(resolve=>{finishSearch=resolve});
  assert.equal(path,'/products/B');return current;
 });
 element('#fi-staff-search').value='berry';
 element('#fi-staff-results').innerHTML='Preserved matching product list';
 const search=controller.search();
 await controller.choose('B');
 controller.editor.set({description:'Saved employee correction',sensory:{tartness:.37}});
 element('#fi-staff-comment').value='Preserve this original-baseline draft';
 finishSearch({items:[staffProduct('A',1)]});
 await search;
 assert.equal(element('#fi-staff-results').hidden,true);
 assert.equal(element('#fi-staff-result-count').hidden,true);
 assert.equal(element('#fi-staff-editor').hidden,false);
 assert.equal(element('#fi-staff-results').innerHTML,'Preserved matching product list');
 controller.backToProducts();
 assert.equal(element('#fi-staff-search').value,'berry');
 assert.equal(element('#fi-staff-results').hidden,false);
 current=staffProduct('B',2);
 await controller.choose('B');
 assert.equal(controller.selected.publishedVersionId,1);
 assert.equal(controller.selected.classificationFingerprint,'B-fingerprint-1');
 assert.equal(controller.selected.classification.description,'Published B version 1');
 assert.equal(controller.editor.read().description,'Saved employee correction');
 assert.equal(controller.editor.read().sensory.tartness,.37);
 assert.equal(element('#fi-staff-comment').value,'Preserve this original-baseline draft');
 assert.match(element('#fi-staff-editor').innerHTML,/Guide used for this draft/);
 assert.match(element('#fi-staff-message').textContent,/original guide/);
});

test('Explorer setup stays hidden until the admin data and tab handlers are ready',async()=>{
 let release;
 const {controller,element}=queueController(async path=>{
  if(path==='/bootstrap')return {capabilities:{admin:true,manageExplorerVisibility:true},principal:{username:'Preview administrator'},taxonomy};
  if(path==='/products')return new Promise(resolve=>{release=resolve});
  return {items:[]};
 });
 const tab=element('#fi-discovery-tab');tab.hidden=true;
 const starting=controller.start();
 await Promise.resolve();await Promise.resolve();
 assert.equal(tab.hidden,true);
 assert.equal(typeof release,'function');
 release({items:[]});await starting;
 assert.equal(tab.hidden,false);
});
