// Normal CI discovers CommonJS entrypoints; preserve each ESM suite unchanged.
const test=require('node:test');
const assert=require('node:assert/strict');
const {readdirSync}=require('node:fs');
const {spawnSync}=require('node:child_process');
const files=readdirSync(__dirname).filter(name=>/^test_flavor.*\.mjs$/.test(name)).sort();
assert.ok(files.length>=8,'Missing Product Explorer frontend suites');
for(let index=0;index<files.length;index+=2){
 const batch=files.slice(index,index+2).map(name=>'tests/'+name);
 test('Product Explorer frontend: '+batch.join(', '),()=>{
  const result=spawnSync(process.execPath,['--max-old-space-size=512','--max-semi-space-size=16','--test-concurrency=2','--test',...batch],
   {cwd:require('node:path').resolve(__dirname,'..'),encoding:'utf8',timeout:120000,maxBuffer:4*1024*1024});
  assert.equal(result.status,0,(result.stdout||'')+(result.stderr||'')+(result.error||''));
 });
}
