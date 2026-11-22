import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { Script } from 'node:vm';
import { productArt } from '../app/static/v2/flavor-explorer/art.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { PRODUCTS, NOTES, STORES, CATALOG_META } from '../app/static/v2/flavor-explorer/catalog.mjs';
import { discover, availabilityFor, availableStrengths } from '../app/static/v2/flavor-explorer/engine.mjs';
const byId = id => PRODUCTS.find(product => product.id === id);
const relatedBerry = {...byId('blueberry-velvet'),id:'related-berry',notes:['blackberry','blueberry'],categories:['fruit'],sensory:{sweetness:3,tartness:3,creaminess:0,richness:1,brightness:4,cooling:0,crispness:3}};
const raspberry = {type:'freebase', strength:3, notes:['raspberry'], storeId:'hwy99', mode:'explore'};
test('freebase + 3mg + raspberry preserves hard gates at every recommendation tier', () => {
  const results = discover([...PRODUCTS,relatedBerry],raspberry);
  assert.ok(results.length >= 5);
  assert.ok(results.every(result => result.product.type === 'freebase'));
  assert.ok(results.every(result => result.eligibleVariations.every(v => v.strengthMg === 3)));
  assert.ok(results.every(result => result.strengths.length === 1 && result.strengths[0] === 3));
  assert.equal(results.some(result => ['raspberry-ripple-salt','raspberry-ice'].includes(result.product.id)),false);
  assert.equal(results.find(result => result.product.id === 'raspberry-ripple').match.kind,'exact');
  assert.equal(results.find(result => result.product.id === 'related-berry').match.kind,'related');
  assert.equal(results.find(result => result.product.id === 'pineapple-press').match.kind,'sensory');
  assert.equal(results.find(result => result.product.id === 'green-apple-snap').match.kind,'sensory');
  assert.equal(results.some(result => result.product.id === 'classic-tobacco'),false);
});
test('exact mode requires every selected flavor; explore labels partial matches', () => {
  const exact = discover(PRODUCTS,{...raspberry,notes:['raspberry','vanilla'],mode:'exact'});
  assert.deepEqual(exact.map(result => result.product.id),['raspberry-ripple']);
  const broad = discover(PRODUCTS,{...raspberry,notes:['raspberry','vanilla']});
  assert.equal(broad[0].match.kind,'exact');
  const partial = broad.find(result => result.product.id === 'blueberry-velvet');
  assert.equal(partial.match.kind,'related');
  assert.equal(partial.match.label,'Some selected notes');
  assert.match(partial.match.reason,/partial/i);
});
test('stock groups preserve deterministic exact, related and sensory ranking', () => {
  const results = discover(PRODUCTS,raspberry);
  const tier = {exact:0,related:1,sensory:2,exploratory:3,browse:4};
  for (let index=1; index<results.length; index++) {
    const before=results[index-1],after=results[index];
    assert.ok(before.availability.state!=='out_of_stock'||after.availability.state==='out_of_stock');
    if((before.availability.state==='out_of_stock')===(after.availability.state==='out_of_stock'))
      assert.ok(tier[before.match.kind] <= tier[after.match.kind]);
  }
  assert.deepEqual(results.map(result => result.product.id),
    discover([...PRODUCTS].reverse(),raspberry).map(result => result.product.id));
});
test('0mg remains a hard strength filter and impossible type/strength combinations are empty', () => {
  const zero = discover(PRODUCTS,{type:'freebase',strength:0});
  assert.ok(zero.length);
  assert.ok(zero.every(result => result.eligibleVariations.every(v => v.strengthMg === 0)));
  assert.deepEqual(discover(PRODUCTS,{type:'salt',strength:3,notes:['raspberry']}),[]);
  assert.deepEqual(discover(PRODUCTS,{type:'disposable',strength:3}),[]);
  assert.deepEqual(discover(PRODUCTS,{strength:'nonsense'}),[]);
  assert.deepEqual(discover(PRODUCTS,{strength:-1}),[]);
  assert.deepEqual(discover(PRODUCTS,{type:'unexpected'}),[]);
});
test('category selections are a hard OR filter, including broader exploration', () => {
  const results = discover(PRODUCTS,{...raspberry,categories:['dessert','beverage']});
  assert.ok(results.length);
  assert.ok(results.every(result => result.product.categories.some(id => ['dessert','beverage'].includes(id))));
  assert.equal(results.some(result => result.product.id === 'pineapple-press'),false);
  assert.deepEqual(discover(PRODUCTS,{categories:['unknown-category']}),[]);
  assert.deepEqual(discover(PRODUCTS,{notes:['unknown-note']}),[]);
});
test('search matches brands and normalized flavor labels using all query words', () => {
  const result = discover(PRODUCTS,{query:'NORTHLINE raspberry',type:'freebase'});
  assert.ok(result.length);
  assert.ok(result.every(row => row.product.brand === 'Northline'));
  assert.equal(discover(PRODUCTS,{query:'green apple',type:'freebase'})[0].product.id,'green-apple-snap');
  assert.deepEqual(discover(PRODUCTS,{query:'northline chocolate'}),[]);
});
test('availability is computed only from eligible nicotine variations and selected store', () => {
  const product = byId('raspberry-ripple');
  assert.equal(availabilityFor(product,'hwy99',0).state,'out_of_stock');
  assert.equal(availabilityFor(product,'hwy99',3).state,'available');
  assert.equal(availabilityFor(product,'longview',3).state,'out_of_stock');
  assert.equal(availabilityFor(product,'andresen',3).state,'stale');
  assert.equal(availabilityFor(product,'sr503',3).state,'unknown');
  assert.equal(discover(PRODUCTS,{...raspberry,storeId:'longview'})
    .find(result => result.product.id === product.id).availability.state,'out_of_stock');
  assert.equal(availabilityFor(product,'hwy99',18).state,'unknown');
  assert.equal(availabilityFor(product,'missing-store',3).state,'unknown');
  assert.deepEqual(availableStrengths(product),[0,3,6]);
});
test('confirmed zero requires every eligible variation confirmed; stale and missing are never zero', () => {
  const checkedAt='2026-11-19T20:00:00Z';
  const fixture = states => ({
    variations: states.map((state,index) => ({
      strengthMg:index,availability:state === null ? {} : {hwy99:{state,checkedAt}},
    })),
  });
  assert.equal(availabilityFor(fixture(['out_of_stock','out_of_stock']),'hwy99').state,'out_of_stock');
  assert.equal(availabilityFor(fixture(['out_of_stock','stale']),'hwy99').state,'stale');
  assert.equal(availabilityFor(fixture(['out_of_stock',null]),'hwy99').state,'unknown');
  assert.equal(availabilityFor(fixture(['stale','unknown']),'hwy99').state,'unknown');
  assert.equal(availabilityFor(fixture(['stale','unknown','available']),'hwy99').state,'available');
  assert.equal(availabilityFor({variations:[]},'hwy99').state,'unknown');
  assert.equal(availabilityFor({variations:[{strengthMg:3,availability:{hwy99:{state:'out_of_stock'}}}]},'hwy99').state,'unknown');
});
test('unavailable products stay discoverable with honest availability labels', () => {
  const product = byId('raspberry-ripple');
  const results = discover([product],{...raspberry,storeId:'sr503'});
  assert.equal(results.length,1);
  assert.equal(results[0].availability.state,'unknown');
  assert.match(results[0].availability.label,/unconfirmed/);
});
test('cards preserve branded flavors and group strengths without mutating source data', () => {
  const snapshot=JSON.stringify(PRODUCTS);
  const results=discover(PRODUCTS,{type:'freebase',notes:['raspberry'],mode:'exact'});
  assert.ok(results.some(result => result.product.id === 'raspberry-ripple'));
  assert.ok(results.some(result => result.product.id === 'raspberry-preserve'));
  assert.equal(results.filter(result => result.product.id === 'raspberry-ripple').length,1);
  assert.deepEqual(results.find(result => result.product.id === 'raspberry-ripple').strengths,[0,3,6]);
  assert.equal(JSON.stringify(PRODUCTS),snapshot);
});
test('representative fixtures are complete, attributable demo data with no exact inventory quantities', () => {
  assert.equal(CATALOG_META.live,false);
  assert.equal(STORES.length,4);
  assert.ok(PRODUCTS.length>=18);
  const noteIds=new Set(NOTES.map(note => note.id));
  const ids=new Set();
  const cards=new Set();
  for (const product of PRODUCTS) {
    assert.equal(ids.has(product.id),false);ids.add(product.id);
    const identity=product.brand+'|'+product.name+'|'+product.type;
    assert.equal(cards.has(identity),false);cards.add(identity);
    assert.match(product.squareItemId,/^demo-/);
    assert.ok(product.notes.every(id=>noteIds.has(id)));
    assert.deepEqual(availableStrengths(product),product.strengths);
    assert.equal(Object.keys(product.sensory).length,7);
    assert.ok(Object.values(product.sensory).every(value=>Number.isInteger(value)&&value>=0&&value<=5));
    for (const variation of product.variations) {
      assert.match(variation.id,/^demo-/);assert.match(variation.sku,/^DEMO-/);
      for (const store of STORES) {
        assert.ok(variation.availability[store.id]);
        assert.equal('quantity' in variation.availability[store.id],false);
      }
    }
  }
});

test('production Explorer loads the live staff catalog and omits the offline owner snapshot', () => {
  const index=readFileSync(new URL('../app/static/v2/flavor-explorer/index.html',import.meta.url),'utf8');
  const source=readFileSync(new URL('../app/static/v2/flavor-explorer/data-source.mjs',import.meta.url),'utf8');
  assert.match(index,/type="module"/);
  assert.match(source,/fetch\('\.\/api\/catalog'/);
  assert.match(source,/credentials:'same-origin'/);
  assert.doesNotMatch(index,/EMBEDDED_CATALOG|owner-preview/);
});
test('product artwork safely escapes catalog text and isolates every rendering instance', () => {
  const hostile={...byId('raspberry-ripple'),id:'same-product',name:'B<&"\'',brand:'A<&"\''};
  const illustrations=[productArt(hostile),productArt(hostile,{hero:true}),productArt({...hostile,type:'disposable'})];
  const idsAcrossIllustrations=new Set();
  for(const svg of illustrations){
    assert.match(svg,/<svg\b/);
    assert.ok(svg.includes('A&lt;&amp;&quot;&#39;'),'brand text must preserve complete XML entities');
    assert.ok(svg.includes('B&lt;&amp;&quot;&#39;') || svg.includes('FLAVOR SERIES'),
      'flavor text must be escaped when the product label presents it');
    assert.doesNotMatch(svg,/&(?!amp;|lt;|gt;|quot;|#39;)/,'escaping must not be truncated inside an XML entity');
    const ids=[...svg.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
    assert.ok(ids.length>0);
    assert.equal(new Set(ids).size,ids.length);
    const localIds=new Set(ids);
    for(const id of ids){
      assert.equal(idsAcrossIllustrations.has(id),false,'repeated card/detail artwork must not share gradient IDs');
      idsAcrossIllustrations.add(id);
    }
    const references=[...svg.matchAll(/url\(#([^)]+)\)/g)].map(match=>match[1]);
    assert.ok(references.length>0);
    assert.ok(references.every(reference=>localIds.has(reference)),
      'every paint reference must resolve within this illustration');
  }
});

test('confirmed stock sorts across relevance tiers, locations and eligible strengths',()=>{
 const checkedAt=new Date().toISOString(),validUntil=new Date(Date.now()+300000).toISOString();
 const make=(id,notes,states)=>({...byId('raspberry-ripple'),id,name:id,notes,
  variations:states.map(([strengthMg,hwy99,longview])=>({id:id+'-'+strengthMg,strengthMg,
   availability:Object.fromEntries([['hwy99',hwy99],['longview',longview]].filter(([,state])=>state).map(([store,state])=>[store,{state,checkedAt,validUntil}]))}))});
 const exact=make('exact',['raspberry'],[[3,'out_of_stock','available'],[6,'available','out_of_stock']]);
 const related=make('related',['blackberry','blueberry'],[[3,'available','out_of_stock']]);
 const unknown=make('unknown',['raspberry'],[[3,null,null]]);
 const stale=make('stale',['raspberry'],[[3,'stale','stale']]);
 const expired=make('expired',['raspberry'],[[3,'out_of_stock','out_of_stock']]);
 expired.variations[0].availability.hwy99.validUntil='2000-01-01T00:00:00Z';
 const products=[exact,related,unknown,stale,expired],snapshot=JSON.stringify(products);
 const here=discover(products,{...raspberry,strength:3});
 assert.equal(here.at(-1).product.id,'exact','an unavailable exact match follows an available related match');
 assert.equal(here.length,5,'uncertain and stale stock remain discoverable');
 assert.equal(here.find(r=>r.product.id==='expired').availability.state,'stale');
 assert.equal(discover(products,{...raspberry,storeId:'longview'}).at(-1).product.id,'related');
 assert.equal(discover([exact],{storeId:'hwy99'} )[0].availability.state,'available','any eligible stocked strength keeps the group available');
 assert.equal(discover([exact],{storeId:'hwy99',strength:3})[0].availability.state,'out_of_stock');
 assert.deepEqual(discover(products,{...raspberry,mode:'exact'}).map(r=>r.product.id).includes('related'),false);
 assert.equal(JSON.stringify(products),snapshot);
});
