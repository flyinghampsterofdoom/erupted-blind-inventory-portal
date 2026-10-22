const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.env.PURCHASING_SOURCE||'app/static/v2/purchasing.js','utf8');

// Match the DOM contract: Element.append mutates children and returns undefined.
class Element {
  constructor(tag,attrs={},text=''){this.tag=tag;this.attrs=attrs;this.textContent=text;this.children=[];this.hidden=false;}
  append(...nodes){this.children.push(...nodes);}
  prepend(...nodes){this.children.unshift(...nodes);}
  after(){}
  replaceChildren(...nodes){this.children=[...nodes];}
  querySelectorAll(selector){
    return this.children.flatMap(child=>[
      ...(selector===child.tag||selector===`.${child.attrs.class}`||
        (selector.startsWith('[')&&selector.endsWith(']')&&Object.hasOwn(child.attrs,selector.slice(1,-1)))?[child]:[]),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  setAttribute(key,value){this.attrs[key]=value;}
}
const el=(...args)=>new Element(...args);
const stores=[{id:'1',name:'Store A'},{id:'2',name:'Store B'}];
let reply,fail=false,inventoryLoads=0;
const context={
  el,base:'/sessions/1',meta:{status:'ACTIVE'},number:String,
  tableStores:()=>stores,params:()=>new URLSearchParams(),
  get:async()=>{if(fail)throw Error('Network unavailable');return reply;},
  button:(text,action)=>{const node=el('button',{},text);node.click=action;return node;},
  rowPair:item=>[el('tr',{'data-line-id':item.id}),el('tr',{'data-detail-id':item.id})],
  workingData:data=>data,
  appendAccount:()=>{},appendPdf:()=>{},appendCategories:()=>{},
  appendCategoryRows:(tbody,data)=>{for(const item of data.rows)tbody.append(...context.rowPair(item));},
  actionForm:()=>{const form=el('form');form.append(el('button'));return form;},
  loadInventory:()=>{inventoryLoads++;},flush:async()=>true,
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  async function loadGroup('),source.indexOf('  function appendPdf('))+'\nthis.loadGroup=loadGroup;',context);
const ctrl={body:el('div'),group:{id:1,name:'Supplier',status:'OPEN',product_count:106,units:0},page:1};
const data=rows=>({rows,page:1,page_size:50,total:rows.length,pages:1});

(async()=>{
  reply=data([]);
  await context.loadGroup(ctrl);
  assert.equal(ctrl.loaded,true,'A valid empty result must render instead of falling into Retry table');
  const cell=ctrl.body.querySelector('.v2-table__empty');
  assert.equal(cell.textContent,'No products match these filters.');
  assert.equal(cell.attrs.colspan,stores.length+5);
  assert.equal(ctrl.body.querySelector('tbody').children[0].children[0],cell);
  assert.equal(ctrl.body.querySelector('.purchasing-pagination').children[0].textContent,'0–0 of 0 matching products · page 1 / 1');
  assert.equal(ctrl.body.querySelector('.purchasing-pagination').querySelectorAll('button').every(node=>node.disabled),true);
  assert.equal(ctrl.body.querySelector('.purchasing-group-actions').querySelector('button').disabled,true,'Zero-unit groups cannot create a PO');

  reply=data([{id:7}]);ctrl.group.units=3;
  await context.loadGroup(ctrl);
  assert.equal(ctrl.loaded,true);
  assert.equal(ctrl.body.querySelector('tbody').children.length,2,'Populated results retain product/detail rows');
  assert.equal(ctrl.body.querySelector('.v2-table__empty'),null);
  assert.equal(inventoryLoads,2);

  fail=true;await context.loadGroup(ctrl);
  assert.equal(ctrl.loaded,false);
  assert.equal(ctrl.body.querySelector('p').textContent,'Network unavailable');
  const retry=ctrl.body.querySelector('button');assert.equal(retry.textContent,'Retry table');
  fail=false;reply=data([]);await retry.click();
  assert.equal(ctrl.loaded,true,'Retry can recover to a valid empty table');
  console.log('Purchasing table: empty, populated, pagination, zero-unit PO guard and retry checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
