import test from 'node:test';
import assert from 'node:assert/strict';
import {PRODUCTS} from '../app/static/v2/flavor-explorer/catalog.mjs';
import {discover} from '../app/static/v2/flavor-explorer/engine.mjs';

function product(id,overrides={}){
 return {...PRODUCTS[0],id,brand:'Fixture Brand',name:'Edition '+id,description:'A catalog description.',
  type:'freebase',notes:['raspberry'],categories:['fruit'],
  sensory:{sweetness:0,tartness:0,creaminess:0,richness:0,brightness:0,cooling:0,crispness:0},
  classificationSource:'published',classificationStatus:'approved',classificationConfidence:'established',
  similarities:[],strengths:[3],variations:[{id:id+'-3',strengthMg:3,availability:{}}],
  ...overrides};
}
const options={type:'freebase',strength:3,notes:['raspberry'],mode:'explore'};
const edge=(productId,weight=.8,reason='Bright red-fruit alternative')=>({productId,weight,reason});
function pair(anchorOverrides={},targetOverrides={}){
 return [product('anchor',{similarities:[edge('target')],...anchorOverrides}),
         product('target',{notes:['cherry'],...targetOverrides})];
}

test('approved directional relationships qualify only in Explore after exact matches',()=>{
 const products=pair();
 const result=discover(products,options);
 assert.deepEqual(result.map(row=>row.product.id),['anchor','target']);
 const match=result[1].match;
 assert.equal(match.kind,'related');
 assert.equal(match.reason,'Team recommendation: Bright red-fruit alternative');
 assert.equal(match.basis,'established');assert.equal(match.inferred,false);
 assert.equal(match.evidence.approvedRelationship,true);
 assert.equal(match.evidence.sourceProductId,'anchor');
 assert.equal(match.matchedNoteCount,0);
 assert.deepEqual(discover(products,{...options,mode:'exact'}).map(row=>row.product.id),['anchor']);
});
test('inferred, pending and unattested source classifications cannot supply team recommendations',()=>{
 for(const source of [
  {classificationSource:'catalog-inferred',classificationStatus:'provisional',classificationConfidence:'provisional'},
  {classificationSource:'published',classificationStatus:'pending',classificationConfidence:'established'},
  {classificationSource:'published',classificationStatus:'approved',classificationConfidence:undefined},
 ]){
  const result=discover(pair(source),options);
  assert.equal(result.some(row=>row.product.id==='target'),false);
 }
 for(const extra of [
  {inferred:true},{pending:true},{approved:false},{status:'PENDING'},
  {classificationSource:'catalog-inferred'},{source:'inferred'},
  {status:'approved',classificationConfidence:'provisional'},
 ]){
  const result=discover(pair({similarities:[{...edge('target'),...extra}]}),options);
  assert.equal(result.some(row=>row.product.id==='target'),false);
 }
});
test('team recommendations cannot bypass target type, exact strength, category or search filters',()=>{
 const anchor=product('anchor',{similarities:[edge('good'),edge('salt'),edge('six'),edge('tobacco'),edge('other')]});
 const candidates=[
  product('good',{name:'Good citrus alternative',notes:['cherry']}),
  product('salt',{name:'Good salt',notes:['cherry'],type:'salt'}),
  product('six',{name:'Good six',notes:['cherry'],strengths:[6],variations:[{id:'six-6',strengthMg:6}]}),
  product('tobacco',{name:'Good tobacco',notes:['tobacco'],categories:['tobacco']}),
  product('other',{name:'Different name',notes:['cherry']}),
 ];
 const result=discover([anchor,...candidates],{...options,categories:['fruit'],query:'good'});
 assert.deepEqual(result.map(row=>row.product.id),['good']);
 assert.deepEqual(result[0].strengths,[3]);
 assert.equal(result[0].eligibleVariations[0].strengthMg,3);
});
test('an anchor with an ineligible type or strength cannot attest a recommendation',()=>{
 for(const override of [
  {type:'salt'},
  {strengths:[6],variations:[{id:'source-6',strengthMg:6}]},
 ]){
  const products=pair(override);
  assert.equal(discover(products,{...options,anchorProductId:'anchor'}).some(row=>row.product.id==='target'),false);
 }
});
test('ordinary browse never activates dormant links; explicit no-note anchors return linked recommendations',()=>{
 const products=[...pair(),product('unrelated',{notes:['cola']})];
 const browse=discover(products,{type:'freebase',strength:3});
 assert.equal(browse.length,3);
 assert.ok(browse.every(row=>row.match.kind==='browse'));
 const anchored=discover(products,{type:'freebase',strength:3,anchorProductId:'anchor'});
 assert.deepEqual(anchored.map(row=>row.product.id),['target']);
 assert.equal(anchored[0].match.kind,'related');
 const exact=discover(products,{type:'freebase',strength:3,mode:'exact',anchorProductId:'anchor'});
 assert.equal(exact.length,3);assert.ok(exact.every(row=>row.match.kind==='browse'));
});
test('relationships remain directional and exact target matches keep their exact classification',()=>{
 const source=product('source',{notes:['custom-a'],similarities:[edge('target')]});
 const target=product('target',{notes:['custom-b']});
 assert.deepEqual(discover([source,target],{...options,notes:['custom-a']}).map(row=>row.product.id),['source','target']);
 assert.deepEqual(discover([source,target],{...options,notes:['custom-b']}).map(row=>row.product.id),['target']);
 const exactTarget=product('target');
 const result=discover([product('anchor',{similarities:[edge('target')]}),exactTarget],options);
 assert.ok(result.every(row=>row.match.kind==='exact'));
 assert.ok(result.every(row=>!row.match.reason.startsWith('Team recommendation')));
});
test('highest-weight reviewed anchor supplies deterministic provenance and verified confidence',()=>{
 const anchors=[
  product('a',{similarities:[edge('target',.5,'First link')]}),
  product('z',{classificationSource:'legacy_verified',classificationConfidence:'existing_verified',
      similarities:[edge('target',.9,'Reviewed berry alternative')]}),
 ];
 const target=product('target',{notes:['cherry']});
 const result=discover([...anchors,target],options).find(row=>row.product.id==='target');
 assert.equal(result.match.basis,'verified');assert.equal(result.match.evidence.sourceProductId,'z');
 assert.equal(result.match.evidence.relationshipWeight,.9);
 const reverse=discover([target,...anchors.reverse()],options).find(row=>row.product.id==='target');
 assert.deepEqual(reverse.match,result.match);
});
test('custom approved note identities match exactly and search through public dictionary labels',()=>{
 const custom=product('custom',{notes:['yuzu-blend']});
 for(const definitions of [
  [{id:'yuzu-blend',label:'Yuzu Citrus'}],
  new Map([['yuzu-blend',{label:'Yuzu Citrus'}]]),
  {'yuzu-blend':'Yuzu Citrus'},
 ]){
  const result=discover([custom],{...options,notes:['yuzu-blend'],mode:'exact',query:'citrus',noteDefinitions:definitions});
  assert.equal(result.length,1);assert.equal(result[0].match.kind,'exact');
  assert.equal(result[0].match.reason,'Classified with yuzu citrus.');
 }
 assert.equal(discover([custom],{...options,notes:['yuzu-blend'],mode:'exact'}).length,1);
});
test('unknown note semantics never manufacture family, partial or sensory recommendations',()=>{
 const custom=product('custom',{notes:['yuzu-blend'],sensory:PRODUCTS[0].sensory});
 const other=product('other',{notes:['raspberry'],sensory:PRODUCTS[0].sensory});
 const definition={id:'yuzu-blend',label:'Yuzu Blend',family:'berry',categories:['fruit'],sensory:PRODUCTS[0].sensory};
 const result=discover([custom,other],{...options,notes:['yuzu-blend'],noteDefinitions:[definition]});
 assert.deepEqual(result.map(row=>row.product.id),['custom']);
 const mixed=discover([product('both',{notes:['raspberry','yuzu-blend']}),other],
  {...options,notes:['raspberry','yuzu-blend'],noteDefinitions:[definition]});
 assert.deepEqual(mixed.map(row=>row.product.id),['both']);
 assert.deepEqual(discover([other],{...options,notes:['never-classified']}),[]);
});
test('approved relationships can bridge a new custom identity without inventing its sensory profile',()=>{
 const [anchor,target]=pair({notes:['yuzu-blend']},{notes:['cola']});
 const result=discover([anchor,target],{...options,notes:['yuzu-blend']});
 assert.deepEqual(result.map(row=>row.product.id),['anchor','target']);
 assert.equal(result[1].match.reason,'Team recommendation: Bright red-fruit alternative');
 assert.equal(result[1].match.inferred,false);
});
test('invalid weights, self links and absent anchors cannot create unrelated recommendations',()=>{
 const target=product('target',{notes:['cherry']});
 for(const weight of [0,-1,2,NaN,Infinity,'0.8']){
  const anchor=product('anchor',{similarities:[edge('target',weight)]});
  assert.equal(discover([anchor,target],options).some(row=>row.product.id==='target'),false);
 }
 assert.deepEqual(discover(pair(),{...options,notes:[],anchorProductId:'missing'}),[]);
 const self=product('anchor',{similarities:[edge('anchor')]});
 assert.equal(discover([self],{type:'freebase',strength:3,anchorProductId:'anchor'}).length,0);
});
test('no approved edges preserve canonical ranking and discovery leaves source data unchanged',()=>{
 const before=JSON.stringify(PRODUCTS);
 const baseline=discover(PRODUCTS,options);
 const clean=PRODUCTS.map(item=>({...item,similarities:[]}));
 assert.deepEqual(discover(clean,options).map(row=>[row.product.id,row.match.kind,row.match.score]),
  baseline.map(row=>[row.product.id,row.match.kind,row.match.score]));
 assert.equal(JSON.stringify(PRODUCTS),before);
 const products=pair();const snapshot=JSON.stringify(products);discover(products,options);
 assert.equal(JSON.stringify(products),snapshot);
});
