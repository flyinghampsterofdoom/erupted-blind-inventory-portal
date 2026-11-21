import test from 'node:test';
import assert from 'node:assert/strict';
import {PRODUCTS} from '../app/static/v2/flavor-explorer/catalog.mjs';
import {discover,availabilityFor} from '../app/static/v2/flavor-explorer/engine.mjs';
import {safeSquareImage} from '../app/static/v2/flavor-explorer/data-source.mjs';
test('raspberry exploration preserves defining tartness instead of weak generic brightness',()=>{
 const results=discover(PRODUCTS,{type:'freebase',strength:3,notes:['raspberry']});
 const ids=new Set(results.map(result=>result.product.id));
 assert.ok(ids.has('pineapple-press'));assert.ok(ids.has('green-apple-snap'));
 assert.equal(ids.has('watermelon-breeze'),false);assert.equal(ids.has('peach-tea'),false);
 assert.equal(ids.has('classic-tobacco'),false);assert.equal(ids.has('mint-glacier'),false);
});
test('unknown nicotine strength is browseable but never becomes 0mg or another selected strength',()=>{
 const unknown={...PRODUCTS[0],id:'square-unknown',type:'disposable',strengths:[],variations:[{...PRODUCTS[0].variations[0],id:'square-v-unknown',strengthMg:null}]};
 assert.equal(discover([unknown],{type:'disposable'}).length,1);
 assert.deepEqual(discover([unknown],{strength:0}),[]);
 assert.deepEqual(discover([unknown],{strength:50}),[]);
 assert.deepEqual(discover([unknown])[0].strengths,[]);
});
test('browser-side expiry prevents a retained Square snapshot from claiming current stock',()=>{
 const p={...PRODUCTS[0],variations:[{id:'v3',strengthMg:3,availability:{hwy99:{state:'available',checkedAt:'2026-01-01T00:00:00Z',validUntil:'2026-01-01T00:15:00Z'}}}]};
 assert.equal(availabilityFor(p,'hwy99',3).state,'stale');
 assert.equal(availabilityFor(p,'longview',3).state,'unknown');
});
test('negative or inconsistent source evidence retains the staff-check state',()=>{
 const p={...PRODUCTS[0],variations:[{id:'v3',strengthMg:3,availability:{hwy99:{state:'check_staff',checkedAt:'2026-11-19T00:00:00Z'}}}]};
 assert.equal(availabilityFor(p,'hwy99',3).state,'check_staff');
});
test('public image URLs accept known Square image origins only without credentials',()=>{
 assert.ok(safeSquareImage('https://items-images-production.s3.us-west-2.amazonaws.com/a.jpg'));
 for(const url of ['http://images.squareup.com/a.jpg','https://evil.example/a.jpg','https://secret@images.squareup.com/a.jpg','https://images.squareup.com:8443/a.jpg','https://images.squareup.com/a.jpg?token=secret','javascript:alert(1)']){
  assert.equal(safeSquareImage(url),null);
 }
});

test('confirmed available products lead equally relevant exact matches without hiding sold-out matches',()=>{
 const base={...PRODUCTS[0],notes:['raspberry'],variations:[{id:'v',strengthMg:3,availability:{hwy99:{state:'available',checkedAt:new Date().toISOString()}}}]};
 const sold={...base,id:'sold',brand:'AAA',variations:[{...base.variations[0],availability:{hwy99:{state:'out_of_stock',checkedAt:new Date().toISOString()}}}]};
 const available={...base,id:'available',brand:'ZZZ'};
 const result=discover([sold,available],{type:'freebase',strength:3,notes:['raspberry'],mode:'exact'});
 assert.deepEqual(result.map(row=>row.product.id),['available','sold']);
});

/*
 * Read-only audit sample from the 339-product Square snapshot (November 19, 2026).
 * These are the 43 formerly returned freebase/3mg/raspberry cards. Only public
 * identity/classification fields are retained: no credentials, SKUs or quantities.
 */
const auditedSquareCandidates=[{"id":"square-fe71d4b6fb41d2fe72ef","brand":"Brand not specified","name":"*Killa Fruits Clearance · *Blue Raspberry","type":"freebase","notes":["raspberry"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-fe71d4b6fb41d2fe72ef-3","strengthMg":3,"availability":{}}]},{"id":"square-a5cccf61f5934ba418f9","brand":"Brand not specified","name":"*Killa Fruits Clearance · *Blue Raspberry Ice","type":"freebase","notes":["raspberry"],"categories":["fruit","ice"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":4,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-a5cccf61f5934ba418f9-3","strengthMg":3,"availability":{}}]},{"id":"square-3359603252e12cc736bf","brand":"Brand not specified","name":"*Killa Fruits Clearance · *Raspberry Black Cherry Lemon","type":"freebase","notes":["raspberry","lemon","cherry"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-3359603252e12cc736bf-3","strengthMg":3,"availability":{}}]},{"id":"square-ad94a9d0eaddea9bd5d3","brand":"Coastal Clouds","name":"Blue Raspberry Chew","type":"freebase","notes":["raspberry"],"categories":["candy","fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-ad94a9d0eaddea9bd5d3-3","strengthMg":3,"availability":{}}]},{"id":"square-e6e2f7f803c751f3b15e","brand":"Coastal Clouds","name":"Iced Blue Raspberry Freeze","type":"freebase","notes":["raspberry"],"categories":["fruit","ice"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":4,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-e6e2f7f803c751f3b15e-3","strengthMg":3,"availability":{}}]},{"id":"square-4e72185d2573584f6bb5","brand":"Fusion","name":"Iced Raspberry Green Apple Watermelon","type":"freebase","notes":["raspberry","green-apple","watermelon"],"categories":["fruit","ice"],"sensory":{"sweetness":3,"tartness":3,"creaminess":0,"richness":0,"brightness":5,"cooling":4,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-4e72185d2573584f6bb5-3","strengthMg":3,"availability":{}}]},{"id":"square-daefcec4576ce3d6c5a1","brand":"Fusion","name":"Raspberry Green Apple Watermelon","type":"freebase","notes":["raspberry","green-apple","watermelon"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":3,"creaminess":0,"richness":0,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-daefcec4576ce3d6c5a1-3","strengthMg":3,"availability":{}}]},{"id":"square-f13468c3bd6fd278740f","brand":"Juice Head","name":"Freeze Raspberry Lemonade","type":"freebase","notes":["raspberry","lemon"],"categories":["beverage","fruit","ice"],"sensory":{"sweetness":3,"tartness":5,"creaminess":0,"richness":1,"brightness":5,"cooling":4,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-f13468c3bd6fd278740f-3","strengthMg":3,"availability":{}}]},{"id":"square-9e63aee0e7a2a29909ce","brand":"Juice Head","name":"Raspberry Lemonade","type":"freebase","notes":["raspberry","lemon"],"categories":["beverage","fruit"],"sensory":{"sweetness":3,"tartness":5,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-9e63aee0e7a2a29909ce-3","strengthMg":3,"availability":{}}]},{"id":"square-444c997462ebfd47f846","brand":"Air Factory","name":"Strawberry Crumble","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-444c997462ebfd47f846-3","strengthMg":3,"availability":{}}]},{"id":"square-78108ed1e407a0d3830b","brand":"Air Factory","name":"Watermelon Strawberry","type":"freebase","notes":["strawberry","watermelon"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-78108ed1e407a0d3830b-3","strengthMg":3,"availability":{}}]},{"id":"square-e8fcd7344cf9eab41df9","brand":"Air Factory","name":"Watermelon Strawberry Ice","type":"freebase","notes":["strawberry","watermelon"],"categories":["fruit","ice"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-e8fcd7344cf9eab41df9-3","strengthMg":3,"availability":{}}]},{"id":"square-7db2ea453c48a5881e2a","brand":"Bams","name":"Strawberry Cannoli","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-7db2ea453c48a5881e2a-3","strengthMg":3,"availability":{}}]},{"id":"square-0bf36c77fd1c0b285923","brand":"Brand not specified","name":"*Killa Fruits Clearance · *Strawberry Acai","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-0bf36c77fd1c0b285923-3","strengthMg":3,"availability":{}}]},{"id":"square-7658d125ffa8a2766aa6","brand":"Brand not specified","name":"Naked Strawberry Pom","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-7658d125ffa8a2766aa6-3","strengthMg":3,"availability":{}}]},{"id":"square-525eeeaf29344276fde8","brand":"Brand not specified","name":"Red's Strawberry","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-525eeeaf29344276fde8-3","strengthMg":3,"availability":{}}]},{"id":"square-efa4c86e4d3bb19d5b82","brand":"Brand not specified","name":"Red's Strawberry Iced","type":"freebase","notes":["strawberry"],"categories":["fruit","ice"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-efa4c86e4d3bb19d5b82-3","strengthMg":3,"availability":{}}]},{"id":"square-6e3f43cc1000b06c1499","brand":"Coastal Clouds","name":"Strawberry Chew","type":"freebase","notes":["strawberry"],"categories":["candy","fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-6e3f43cc1000b06c1499-3","strengthMg":3,"availability":{}}]},{"id":"square-37498a1a98fb953731dd","brand":"Coastal Clouds","name":"Strawberry Tart","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-37498a1a98fb953731dd-3","strengthMg":3,"availability":{}}]},{"id":"square-bf97be249b9ef575ff92","brand":"Fresh Farms","name":"Strawberry Farm Cake","type":"freebase","notes":["strawberry"],"categories":["dessert","fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-bf97be249b9ef575ff92-3","strengthMg":3,"availability":{}}]},{"id":"square-380edd6ad28894ca4b38","brand":"Fruitia","name":"Strawberry Coconut","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-380edd6ad28894ca4b38-3","strengthMg":3,"availability":{}}]},{"id":"square-ebb7fa3164fea1bc0fd8","brand":"Fusion","name":"Iced Strawberry Banana Apple","type":"freebase","notes":["strawberry"],"categories":["fruit","ice"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-ebb7fa3164fea1bc0fd8-3","strengthMg":3,"availability":{}}]},{"id":"square-74774e96e5c79f02320c","brand":"Fusion","name":"Strawberry Banana Apple","type":"freebase","notes":["strawberry"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-74774e96e5c79f02320c-3","strengthMg":3,"availability":{}}]},{"id":"square-5fe7aac2724950d94cde","brand":"Fusion","name":"Strawberry Blackberry Lemon","type":"freebase","notes":["strawberry","blackberry","lemon"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":3,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-5fe7aac2724950d94cde-3","strengthMg":3,"availability":{}}]},{"id":"square-0bee00eec0f9c7128987","brand":"Juice Head","name":"Blueberry Lemon","type":"freebase","notes":["blueberry","lemon"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-0bee00eec0f9c7128987-3","strengthMg":3,"availability":{}}]},{"id":"square-ceae8e5a79a18255ac54","brand":"Juice Head","name":"Freeze Blueberry Lemon","type":"freebase","notes":["blueberry","lemon"],"categories":["fruit","ice"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-ceae8e5a79a18255ac54-3","strengthMg":3,"availability":{}}]},{"id":"square-787807acc248f63a40f2","brand":"Juice Head","name":"Mango Strawberry","type":"freebase","notes":["strawberry","mango"],"categories":["fruit"],"sensory":{"sweetness":5,"tartness":2,"creaminess":1,"richness":2,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-787807acc248f63a40f2-3","strengthMg":3,"availability":{}}]},{"id":"square-5dac61f274f169b30feb","brand":"Juice Head","name":"Mango Strawberry Freeze","type":"freebase","notes":["strawberry","mango"],"categories":["fruit","ice"],"sensory":{"sweetness":5,"tartness":2,"creaminess":1,"richness":2,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-5dac61f274f169b30feb-3","strengthMg":3,"availability":{}}]},{"id":"square-711da9c69338e2a9ec78","brand":"Juice Head","name":"Strawberry Cream","type":"freebase","notes":["strawberry","cream"],"categories":["dessert","fruit"],"sensory":{"sweetness":4,"tartness":1,"creaminess":3,"richness":3,"brightness":2,"cooling":0,"crispness":1},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-711da9c69338e2a9ec78-3","strengthMg":3,"availability":{}}]},{"id":"square-40c30421e91edef7b007","brand":"Juice Head","name":"Strawberry Kiwi","type":"freebase","notes":["strawberry","kiwi"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":3,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-40c30421e91edef7b007-3","strengthMg":3,"availability":{}}]},{"id":"square-a85f0dd00e06abe50ae9","brand":"Juice Head","name":"Strawberry Kiwi Freeze","type":"freebase","notes":["strawberry","kiwi"],"categories":["fruit","ice"],"sensory":{"sweetness":4,"tartness":3,"creaminess":0,"richness":1,"brightness":5,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-a85f0dd00e06abe50ae9-3","strengthMg":3,"availability":{}}]},{"id":"square-22872feaefcfe607093e","brand":"Juice Head","name":"Watermelon Strawberry","type":"freebase","notes":["strawberry","watermelon"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-22872feaefcfe607093e-3","strengthMg":3,"availability":{}}]},{"id":"square-a7d92d83d0d57c6d2c11","brand":"Juice Head","name":"Watermelon Strawberry Freeze","type":"freebase","notes":["strawberry","watermelon"],"categories":["fruit","ice"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":4,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-a7d92d83d0d57c6d2c11-3","strengthMg":3,"availability":{}}]},{"id":"square-f9a1476bb2e76f4a6880","brand":"Prime!","name":"Strawberry Lemonade Frost","type":"freebase","notes":["strawberry","lemon"],"categories":["beverage","fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-f9a1476bb2e76f4a6880-3","strengthMg":3,"availability":{}}]},{"id":"square-adbe4340611baebfc50d","brand":"Prime!","name":"Strawberry Watermelon Frost","type":"freebase","notes":["strawberry","watermelon"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":2,"creaminess":0,"richness":1,"brightness":4,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-adbe4340611baebfc50d-3","strengthMg":3,"availability":{}}]},{"id":"square-71960754b61ffb92b4f2","brand":"Fruitia","name":"Apple Kiwi","type":"freebase","notes":["kiwi"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":4,"creaminess":0,"richness":0,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-71960754b61ffb92b4f2-3","strengthMg":3,"availability":{}}]},{"id":"square-6ae05873c065818dc666","brand":"Fruitia","name":"Pineapple Citrus","type":"freebase","notes":["pineapple"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-6ae05873c065818dc666-3","strengthMg":3,"availability":{}}]},{"id":"square-df5c7491161188d3c4b0","brand":"Juice Head","name":"Pineapple Grapefruit","type":"freebase","notes":["pineapple"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-df5c7491161188d3c4b0-3","strengthMg":3,"availability":{}}]},{"id":"square-a9e924d74edf1cfb74e8","brand":"Juice Head","name":"Pineapple Guava","type":"freebase","notes":["pineapple"],"categories":["fruit"],"sensory":{"sweetness":4,"tartness":4,"creaminess":0,"richness":1,"brightness":5,"cooling":0,"crispness":3},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-a9e924d74edf1cfb74e8-3","strengthMg":3,"availability":{}}]},{"id":"square-12d84a3c431cf6a64b4a","brand":"Coastal Clouds","name":"Lemon Tart","type":"freebase","notes":["lemon"],"categories":["fruit"],"sensory":{"sweetness":2,"tartness":5,"creaminess":0,"richness":0,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-12d84a3c431cf6a64b4a-3","strengthMg":3,"availability":{}}]},{"id":"square-a16fe030429266317465","brand":"Coastal Clouds","name":"Tropical Lemonade","type":"freebase","notes":["lemon"],"categories":["beverage","fruit"],"sensory":{"sweetness":2,"tartness":5,"creaminess":0,"richness":0,"brightness":5,"cooling":0,"crispness":4},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-a16fe030429266317465-3","strengthMg":3,"availability":{}}]},{"id":"square-0dd53995d404c5152a55","brand":"Juice Head","name":"Watermelon Lime","type":"freebase","notes":["watermelon","lime"],"categories":["fruit"],"sensory":{"sweetness":3,"tartness":3,"creaminess":0,"richness":0,"brightness":5,"cooling":0,"crispness":5},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-0dd53995d404c5152a55-3","strengthMg":3,"availability":{}}]},{"id":"square-d82295a60b411e9d54aa","brand":"Coastal Clouds","name":"Red Cherry Chew","type":"freebase","notes":["cherry"],"categories":["candy","fruit"],"sensory":{"sweetness":4,"tartness":3,"creaminess":0,"richness":2,"brightness":4,"cooling":0,"crispness":2},"sensorySource":"catalog-inferred","classificationSource":"catalog-inferred","classificationStatus":"provisional","strengths":[3],"variations":[{"id":"square-d82295a60b411e9d54aa-3","strengthMg":3,"availability":{}}]}];
const auditedOptions={type:'freebase',strength:3,notes:['raspberry'],mode:'explore',storeId:'hwy99'};
test('actual Square raspberry audit retains exact identities and excludes dessert/cooling detours',()=>{
 const results=discover(auditedSquareCandidates,auditedOptions);
 const exact=results.filter(row=>row.match.kind==='exact');
 assert.equal(exact.length,9);
 assert.deepEqual(new Set(exact.map(row=>row.product.id)),
   new Set(auditedSquareCandidates.filter(product=>product.notes.includes('raspberry')).map(product=>product.id)));
 const excluded=[
  'Strawberry Crumble','Watermelon Strawberry Ice','Strawberry Cannoli',
  "Red's Strawberry Iced",'Strawberry Farm Cake','Iced Strawberry Banana Apple',
  'Freeze Blueberry Lemon','Mango Strawberry Freeze','Strawberry Cream',
  'Strawberry Kiwi Freeze','Watermelon Strawberry Freeze',
  'Strawberry Lemonade Frost','Strawberry Watermelon Frost',
 ];
 for(const name of excluded)assert.equal(results.some(row=>row.product.name===name),false,name);
 assert.equal(results.length,30);
 assert.ok(exact.some(row=>row.product.name==='Iced Blue Raspberry Freeze'),
   'an inferred cooling contrast must never remove an exact named raspberry match');
});
test('real recommendations distinguish close berry identities, strong tart alternatives and softer exploration',()=>{
 const results=discover(auditedSquareCandidates,auditedOptions);
 const named=name=>results.find(row=>row.product.name===name);
 assert.equal(named('Blueberry Lemon').match.kind,'related');
 assert.equal(named('Strawberry Kiwi').match.kind,'related');
 assert.equal(named('Strawberry Blackberry Lemon').match.kind,'related');
 assert.equal(named('Pineapple Citrus').match.kind,'sensory');
 assert.equal(named('Apple Kiwi').match.kind,'sensory');
 assert.equal(named("Red's Strawberry").match.kind,'exploratory');
 assert.equal(named('Red Cherry Chew').match.kind,'exploratory');
 const tier={exact:0,related:1,sensory:2,exploratory:3};
 for(let index=1;index<results.length;index++)
  assert.ok(tier[results[index-1].match.kind]<=tier[results[index].match.kind]);
 assert.ok(results.indexOf(named('Blueberry Lemon'))<results.indexOf(named('Pineapple Citrus')));
 assert.ok(results.indexOf(named('Pineapple Citrus'))<results.indexOf(named("Red's Strawberry")));
});
test('type and strength remain hard gates for every new recommendation tier',()=>{
 const wrongFormat=auditedSquareCandidates.map(product=>({...product,id:product.id+'-salt',type:'salt'}));
 const wrongStrength=auditedSquareCandidates.map(product=>({...product,id:product.id+'-6',strengths:[6],
  variations:product.variations.map(variation=>({...variation,strengthMg:6}))}));
 const results=discover([...auditedSquareCandidates,...wrongFormat,...wrongStrength],auditedOptions);
 assert.equal(results.length,30);
 for(const row of results){
  assert.equal(row.product.type,'freebase');
  assert.deepEqual(row.strengths,[3]);
  assert.ok(row.eligibleVariations.every(variation=>variation.strengthMg===3));
  assert.equal(row.product.id.endsWith('-salt')||row.product.id.endsWith('-6'),false);
 }
});
test('multi-note exact matching means all notes while exploration declares partial coverage',()=>{
 const exact=discover(PRODUCTS,{...auditedOptions,notes:['raspberry','vanilla'],mode:'exact'});
 assert.deepEqual(exact.map(row=>row.product.id),['raspberry-ripple']);
 const broad=discover(PRODUCTS,{...auditedOptions,notes:['raspberry','vanilla']});
 const partial=broad.find(row=>row.product.id==='blueberry-velvet');
 assert.ok(partial);
 assert.deepEqual(partial.match.matchedNotes,['vanilla']);
 assert.equal(partial.match.selectedNoteCount,2);
 assert.equal(partial.match.matchedNoteCount,1);
 assert.match(partial.match.reason,/partial/i);
 assert.equal(broad.some(row=>row.product.id==='berry-noir'),false,
  'one raspberry note cannot erase an additional creamy preference');
 const cooling=discover(auditedSquareCandidates,{...auditedOptions,notes:['raspberry','mint']});
 assert.ok(cooling.some(row=>row.product.name==='Iced Blue Raspberry Freeze'));
 assert.ok(cooling.every(row=>row.product.sensory.cooling>=3));
 assert.ok(cooling.every(row=>row.match.kind!=='exact'),'cooling evidence does not invent a mint identity');
});
test('unknown identities or generic numbers alone cannot create inferred sensory recommendations',()=>{
 const source=auditedSquareCandidates.find(product=>product.name==='Pineapple Citrus');
 const unclassified={...source,id:'awaiting',notes:[],classificationStatus:'awaiting'};
 const unrelatedNumbers={...source,id:'generic-soda',name:'Cola',notes:['cola'],categories:['beverage']};
 assert.deepEqual(discover([unclassified,unrelatedNumbers],auditedOptions),[]);
});
test('recommendation confidence follows reviewed status, never source labels or a numerical score alone',()=>{
 const results=discover(auditedSquareCandidates,auditedOptions);
 for(const row of results){
  assert.equal(row.match.basis,'inferred');
  assert.equal(row.match.inferred,true);
  if(row.match.kind!=='exact'){
   assert.match(row.match.reason,/^May suit/);
   assert.match(row.match.shortReason,/^May suit:/);
  }
 }
 const source=auditedSquareCandidates.find(product=>product.name==='Pineapple Citrus');
 const variants=[
  {...source,id:'not-reviewed',classificationSource:'published',classificationStatus:'provisional'},
  {...source,id:'verified',classificationSource:'legacy_verified',classificationStatus:'existing_verified'},
  {...source,id:'established',classificationSource:'published',classificationStatus:'established'},
 ];
 const byId=new Map(discover(variants,auditedOptions).map(row=>[row.product.id,row.match]));
 assert.equal(byId.get('not-reviewed').basis,'inferred');
 assert.equal(byId.get('verified').basis,'verified');
 assert.equal(byId.get('verified').inferred,false);
 assert.equal(byId.get('established').basis,'established');
 assert.equal(byId.get('established').inferred,false);
});

test('ice cream dessert names do not masquerade as a menthol or cooling detour',()=>{
 const dessert={...PRODUCTS[0],id:'creamery-dessert',name:'Creamery Ice Cream',
  notes:['cream'],categories:['dessert'],
  sensory:{sweetness:4,tartness:0,creaminess:5,richness:4,brightness:0,cooling:0,crispness:0}};
 const hyphenated={...dessert,id:'hyphenated-dessert',name:'Creamery Ice-Cream'};
 const iced={...dessert,id:'iced-detour',name:'Creamery Ice'};
 const result=discover([dessert,hyphenated,iced],{type:'freebase',strength:3,notes:['vanilla']});
 assert.deepEqual(new Set(result.map(row=>row.product.id)),new Set(['creamery-dessert','hyphenated-dessert']));
 assert.ok(result.every(row=>row.match.kind==='related'));
});

test('published backend approval shape preserves established or recovered confidence',()=>{
 const source=auditedSquareCandidates.find(product=>product.name==='Pineapple Citrus');
 const variants=[
  {...source,id:'approved-established',classificationStatus:'approved',classificationConfidence:'established',classificationSource:'published'},
  {...source,id:'approved-recovered',classificationStatus:'approved',classificationConfidence:'existing_verified',classificationSource:'published'},
  {...source,id:'mislabelled-inference',classificationStatus:'approved',classificationConfidence:'existing_verified',classificationSource:'catalog-inferred'},
 ];
 const byId=new Map(discover(variants,auditedOptions).map(row=>[row.product.id,row.match]));
 assert.equal(byId.get('approved-established').basis,'established');
 assert.equal(byId.get('approved-established').inferred,false);
 assert.equal(byId.get('approved-recovered').basis,'verified');
 assert.equal(byId.get('approved-recovered').inferred,false);
 assert.equal(byId.get('mislabelled-inference').basis,'inferred');
 assert.equal(byId.get('mislabelled-inference').inferred,true);
});
