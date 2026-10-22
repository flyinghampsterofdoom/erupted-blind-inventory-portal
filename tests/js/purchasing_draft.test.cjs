const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
const source=fs.readFileSync('app/static/v2/purchasing.js','utf8');
const calls=[];let fail=true;
const context={crypto,AbortController,setTimeout,clearTimeout,
  root:{dataset:{csrf:'synthetic'},querySelector:()=>null,querySelectorAll:()=>[]},
  saveButton:{},editControls:{},status:{},
  persist:()=>true,rowState:()=>{},syncGroups:()=>{},clearError:()=>{},showError:()=>{},pendingSummary:()=>{},reviewConflict:async()=>{},
  refreshMeta:async()=>{},refreshOpen:async()=>{},showFinalReview:async()=>{},
  fetch:async(url,options)=>{calls.push(JSON.parse(options.body));return {ok:!fail,status:fail?503:200,headers:{get:()=> 'application/json'},json:async()=>fail?{detail:'Synthetic network failure'}:{version:2}};},
};
vm.createContext(context);
vm.runInContext(`let queue=[],busy=false,paused=false,conflict=false,pendingBatch=null,version=1,finalReviewSupplier=null;const base='/sessions/1';\n`+
  source.slice(source.indexOf('  function draftStatus('),source.indexOf('  async function flush('))+
  source.slice(source.indexOf('  function overlayItem('),source.indexOf('  function pendingLines('))+
  '\nthis.enqueue=enqueue;this.pump=pump;this.overlayItem=overlayItem;this.state=()=>({queue,pendingBatch,version,busy});',context);
const item=id=>({id,name:`Synthetic ${id}`,supplier_id:1,quantity:5,allocations:{1:5},cost:'3',decisions:{}});
(async()=>{
  for(let id=1;id<=30;id++)context.enqueue({kind:'line',line_id:id,preview:item(id),changes:{quantity:id,allocations:{1:id}}});
  assert.equal(calls.length,0,'Editing never invokes server persistence');
  assert.equal(context.state().queue.length,30);
  context.enqueue({kind:'line',line_id:1,preview:item(1),changes:{supplier_id:2}});
  assert.equal(context.state().queue.length,30,'Supplier movement merges into the same pending product decision');
  assert.equal(context.overlayItem(item(1)).supplier_id,2);
  assert.equal(context.overlayItem(item(1)).quantity,1);
  assert.equal(context.overlayItem(item(1)).allocations[1],1);
  assert.equal(item(1).quantity,5,'Saved baselines are not mutated');
  await context.pump();
  assert.equal(calls.length,1);assert.equal(calls[0].operations.length,30);
  assert.equal(context.state().queue.length,30,'A failed save retains every edit');
  fail=false;await context.pump();
  assert.deepEqual(calls[0],calls[1],'Retry sends the exact request identity, version and operations');
  assert.equal(context.state().queue.length,0);assert.equal(context.state().pendingBatch,null);assert.equal(context.state().version,2);
  console.log('Purchasing draft: 30 local edits, merged transfer, retained failed save and exact-identity retry passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Confirmation in another tab must clear the retained request as well as its queue.
const storageContext={
  window:{addEventListener:(name,handler)=>storageContext.handler=handler},
  get:async()=>({version:2,lines:{1:{quantity:1,allocations:{1:1}}}}),
  persist:()=>true,clearError:()=>{},draftStatus:()=>{},refreshMeta:async()=>{},refreshOpen:async()=>{},
};
vm.createContext(storageContext);
vm.runInContext(`const storageKey='synthetic',base='/sessions/1';let queue=[{kind:'line',line_id:1,changes:{quantity:1,allocations:{1:1}}}],pendingBatch={request_key:'committed'},paused=true,busy=false,conflict=false,version=1;\n`+
  source.slice(source.indexOf("  window.addEventListener('storage',"),source.indexOf('  const catalogSearch='))+
  '\nthis.state=()=>({queue,pendingBatch,paused,version});',storageContext);
storageContext.handler({key:'synthetic',newValue:null}).then(()=>{
  assert.equal(storageContext.state().queue.length,0);
  assert.equal(storageContext.state().pendingBatch,null,'A confirmed cross-tab save must not leave a stale request to replay on the next edit');
  assert.equal(storageContext.state().version,2);
  console.log('Purchasing draft: cross-tab confirmation clears the saved request receipt.');
}).catch(error=>{console.error(error);process.exitCode=1;});

const unloadContext={window:{addEventListener:(name,handler)=>unloadContext.handler=handler},persist:()=>true};
vm.createContext(unloadContext);
vm.runInContext('let queue=[{id:1}],busy=false;const timers=new Map();\n'+source.slice(source.indexOf("  window.addEventListener('beforeunload',"),source.indexOf("  root.addEventListener('focusout',"))+'\nthis.clear=()=>queue=[];',unloadContext);
let prevented=false;const departure={preventDefault:()=>prevented=true};unloadContext.handler(departure);
assert.equal(prevented,true);assert.equal(departure.returnValue,'');
unloadContext.clear();prevented=false;unloadContext.handler({preventDefault:()=>prevented=true});assert.equal(prevented,false);
console.log('Purchasing draft: navigation warning applies only while work is unsaved.');
