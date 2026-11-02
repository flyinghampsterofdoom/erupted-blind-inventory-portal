const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('app/static/v2/scheduling.js','utf8');
const code=source.slice(source.indexOf('  async function api('),source.indexOf('  const publishDialog ='))+'\nthis.api=api;';
(async()=>{
 for(const failure of [new DOMException('The operation was aborted.','AbortError'),Object.freeze(new Error('Frozen transport error'))]){
   const ctx={board:{period:{}},refreshRequired:false,writeInFlight:false,pendingMove:null,query:()=>'',csrf:()=>'',fetch:async()=>{throw failure},AbortController,setTimeout,clearTimeout};
   vm.createContext(ctx);vm.runInContext(code,ctx);
   await assert.rejects(ctx.api('/publish'), e=>{
     assert.equal(e.cause,failure);assert.equal(e.requiresRefresh,true);assert.match(e.message,/could not be confirmed/);return true;
   });
   assert.equal(ctx.refreshRequired,true);assert.equal(ctx.writeInFlight,false);
 }
 console.log('Read-only DOMException and frozen Error preserve cause without message assignment.');
})().catch(e=>{console.error(e);process.exitCode=1});
