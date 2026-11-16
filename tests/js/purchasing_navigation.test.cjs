const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const {stripTypeScriptTypes}=require("node:module");
const navContext={};vm.createContext(navContext);
vm.runInContext(stripTypeScriptTypes(fs.readFileSync(require.resolve("../../frontend/purchasing-v4/src/navigation.ts"),"utf8")).replace("export function","function"),navContext);
const nav=navContext.navigationTarget;
const ids=["vendor-first","before","editing","after","vendor-next"];
test("Tab and Shift Tab retain normal four-store navigation",()=>{
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,ids,"editing",1,0,1))),{id:"editing",col:2});
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,ids,"editing",3,0,1))),{id:"after",col:0});
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,ids,"editing",0,0,-1))),{id:"before",col:3});
});
test("filtering edited zero row never resets Tab to vendor top",()=>{
 const shown=ids.filter(id=>id!=="editing");
 for(let col=0;col<4;col++){
  assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,shown,"editing",col,0,1))),{id:"after",col:0});
  assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,shown,"editing",col,0,-1))),{id:"before",col:3});
 }
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,shown,"editing",1,1,0))),{id:"after",col:1});
});
test("filtered neighbors, vendor boundaries and empty results",()=>{
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,["before","vendor-next"],"editing",3,0,1))),{id:"vendor-next",col:0});
 assert.deepEqual(JSON.parse(JSON.stringify(nav(ids,["before"],"vendor-next",3,0,1))),{id:"before",col:0});
 assert.equal(nav(ids,[],"editing",1,0,1),undefined);
 assert.equal(nav(ids,ids,"missing",1,0,1),undefined);
});
test("real worksheet key handler captures order before commit rebuild",()=>{
 const app=fs.readFileSync(require.resolve("../../frontend/purchasing-v4/src/app.ts"),"utf8");
 const code=app.slice(app.indexOf("function key("),app.indexOf("function copyText("));
 for(const key of ["Tab","Enter","ArrowDown"]){
  let args;
  const context={visibleIds:[...ids],editor:{editing:false},finishEditor(){context.visibleIds=["before","after"];},move(...a){args=a;}};
  vm.createContext(context);vm.runInContext(stripTypeScriptTypes(code),context);
  context.key({key,preventDefault(){},shiftKey:false},"editing",3);
  assert.deepEqual(Array.from(args[5]),ids);
 }
});
