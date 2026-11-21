import { NOTES, CATEGORIES } from './catalog.mjs';

const noteById = new Map(NOTES.map(note => [note.id, note]));
const categoryById = new Map(CATEGORIES.map(category => [category.id, category]));
const sensoryKeys = ['sweetness','tartness','creaminess','richness','brightness','cooling','crispness'];
const validTypes = new Set(['freebase','salt','disposable']);
const validStates = new Set(['available','out_of_stock','stale','check_staff','unknown']);
const unique = values => [...new Set(values)];
const compareText = (left, right) => left < right ? -1 : left > right ? 1 : 0;
const words = value => String(value ?? '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
function parsedStrength(strength) {
  if (strength === null || strength === undefined || strength === '') return null;
  const value = typeof strength === 'number' ? strength : Number(strength);
  return Number.isFinite(value) && value >= 0 ? value : NaN;
}
function variationsFor(product, strength = null) {
  const value = parsedStrength(strength);
  if (Number.isNaN(value)) return [];
  return (product.variations || []).filter(variation =>
    variation.isActive !== false && (Number.isFinite(variation.strengthMg) || (value === null && variation.strengthMg == null)) &&
    (value === null || variation.strengthMg === value));
}
export function availableStrengths(product) {
  return unique(variationsFor(product).map(variation => variation.strengthMg).filter(Number.isFinite)).sort((a,b) => a-b);
}
function summarizeAvailability(variations, storeId) {
  const records = variations.map(variation => {
    let value = variation.availability?.[storeId];
    if(value && ['available','out_of_stock'].includes(value.state) && value.validUntil && (!Number.isFinite(Date.parse(value.validUntil)) || Date.parse(value.validUntil)<=Date.now())) value={...value,state:'stale'};
    if (!value || !validStates.has(value.state)) return { state: 'unknown', checkedAt: null };
    // A confirmed stock state requires an evidence timestamp; absence is unknown.
    if (['available','out_of_stock'].includes(value.state) && !Number.isFinite(Date.parse(value.checkedAt))) {
      return { state: 'unknown', checkedAt: null };
    }
    return { state: value.state, checkedAt: value.checkedAt || null };
  });
  let state = 'unknown';
  if (records.some(record => record.state === 'available')) state = 'available';
  else if (records.length && records.every(record => record.state === 'out_of_stock')) state = 'out_of_stock';
  else if (records.some(record => record.state === 'unknown')) state = 'unknown';
  else if (records.some(record => record.state === 'check_staff')) state = 'check_staff';
  else if (records.some(record => record.state === 'stale')) state = 'stale';
  const relevantDates = records.filter(record => record.state === state && Number.isFinite(Date.parse(record.checkedAt)))
    .map(record => record.checkedAt).sort((a,b) => Date.parse(a)-Date.parse(b));
  const labels = {
    available: 'Available', out_of_stock: 'Out of stock',
    stale: 'Availability needs a refresh', check_staff: 'Check with staff', unknown: 'Availability unconfirmed',
  };
  return { state, label: labels[state], checkedAt: relevantDates.at(-1) || null, storeId };
}
export function availabilityFor(product, storeId, strength = null) {
  return summarizeAvailability(variationsFor(product, strength), storeId);
}
function profileFor(value) {
  const profile={};
  for(const key of sensoryKeys){
    const number=value?.[key];
    profile[key]=typeof number==='number' && Number.isFinite(number) && number>=0 && number<=5?number:null;
  }
  return profile;
}
function recommendationContext(product,selected) {
  // New approved identities may match exactly, but their names do not establish
  // a sensory profile or a family relationship.
  if(selected.some(note=>!noteById.has(note.id)))return null;
  const target=Object.fromEntries(sensoryKeys.map(key=>[
    key,selected.reduce((sum,note)=>sum+note.sensory[key],0)/selected.length,
  ]));
  const profile=profileFor(product.sensory);
  if(sensoryKeys.some(key=>profile[key]===null))return null;
  const productNotes=(product.notes||[]).map(id=>noteById.get(id)).filter(Boolean);
  // Unclassified records are not assigned a plausible taste from zero-filled fields.
  if(!productNotes.length && matchBasis(product)==='inferred')return null;
  const selectedIds=new Set(selected.map(note=>note.id));
  const selectedFamilies=new Set(selected.map(note=>note.family));
  const families=new Set(productNotes.map(note=>note.family));
  const direct=selected.filter(note=>(product.notes||[]).includes(note.id));
  const familyCount=selected.filter(note=>families.has(note.family)).length;
  const directCoverage=direct.length/selected.length;
  const familyCoverage=familyCount/selected.length;
  const wantsCooling=selected.some(note=>note.sensory.cooling>=4);
  const wantsCream=selected.some(note=>note.sensory.creaminess>=4);
  const wantsTobacco=selectedIds.has('tobacco');
  const hasTobacco=(product.categories||[]).includes('tobacco')||(product.notes||[]).includes('tobacco');
  const coolingName=/(?:\b(?:iced|freeze|frozen|frost|menthol|cooling)\b|\bice\b(?![\s-]*cream))/i.test(product.name||'');
  const dessertName=/\b(?:cannoli|crumble|cake|cheesecake|custard|cream|pudding|cookie|cookies|donut|doughnut|pastry|milkshake|yogurt)\b/i.test(product.name||'');
  // A related word must not bypass a major taste detour. Exact-note matches run
  // before these recommendation gates and are never removed by inferred tastes.
  if(hasTobacco!==wantsTobacco)return null;
  if(wantsCooling?(profile.cooling<3&&!coolingName):(profile.cooling>=3||coolingName))return null;
  if(wantsCream?profile.creaminess<2:(profile.creaminess>=3||dessertName))return null;
  const weights={sweetness:1,tartness:3,creaminess:3,richness:1.5,brightness:2,cooling:3,crispness:1.5};
  const totalWeight=Object.values(weights).reduce((sum,value)=>sum+value,0);
  const distance=sensoryKeys.reduce((sum,key)=>sum+weights[key]*Math.abs(target[key]-profile[key]),0);
  const similarity=1-distance/(5*totalWeight);
  const semanticBridge=selected.some(note=>productNotes.some(candidate=>note.categories.some(category=>candidate.categories.includes(category))));
  const identityAffinity=selected.reduce((sum,note)=>{
    if((product.notes||[]).includes(note.id))return sum+1;
    if(families.has(note.family))return sum+.72;
    return sum;
  },0)/selected.length;
  const defining=sensoryKeys.filter(key=>target[key]>=3.5);
  const definingTaste=['tartness','creaminess','richness','cooling'].filter(key=>target[key]>=3.5);
  const shared=defining.filter(key=>profile[key]>=Math.max(3,target[key]-1));
  const preservesTaste=definingTaste.every(key=>profile[key]>=target[key]-1);
  const preservesShape=defining.every(key=>profile[key]>=target[key]-1);
  // Several requested identities require either majority identity coverage or
  // majority family coverage. One incidental shared note cannot satisfy a blend.
  if(selected.length>1 && directCoverage<.5 && familyCoverage<.75)return null;
  return {target,profile,productNotes,selectedFamilies,direct,directCoverage,familyCoverage,
    identityAffinity,similarity,shared,definingTaste,preservesTaste,preservesShape,semanticBridge};
}
function matchBasis(product){
  const confidence=product.classificationConfidence||product.classificationStatus;
  if(confidence==='existing_verified' && ['published','legacy_verified'].includes(product.classificationSource))return 'verified';
  if(confidence==='established' && product.classificationSource==='published')return 'established';
  return 'inferred';
}
function recommendation(product,kind,label,phrase,score,context,selected){
  const basis=matchBasis(product);
  const inferred=basis==='inferred';
  const partial=context.direct.length>0 && context.direct.length<selected.length;
  const notes=context.direct.map(note=>note.label.toLowerCase()).join(' + ');
  const reason=partial
    ?'Partial match: '+notes+'. '+(inferred?'May suit':'Explore')+' your preference for '+phrase.toLowerCase()+'.'
    :(inferred?'May suit your preference for ':'Shares ')+phrase.toLowerCase()+'.';
  return {kind,label,score:Math.round(score),reason,
    shortReason:(inferred?'May suit: ':'')+phrase,
    basis,inferred,matchedNotes:context.direct.map(note=>note.id),
    selectedNoteCount:selected.length,matchedNoteCount:context.direct.length,
    evidence:{identityCoverage:context.directCoverage,familyCoverage:context.familyCoverage,
      sensorySimilarity:Math.round(context.similarity*1000)/1000}};
}
function familyPhrase(context){
  if(context.selectedFamilies.has('berry') && context.selectedFamilies.has('cream'))return 'Berry & cream alternative';
  if(context.selectedFamilies.has('berry') && context.selectedFamilies.has('herbal'))return 'Berry & cooling alternative';
  if(context.selectedFamilies.has('berry'))return context.profile.tartness>=3?'Tart berry profile':'Softer berry profile';
  if(context.selectedFamilies.has('cream')||context.selectedFamilies.has('confection'))return 'Related creamy dessert';
  if(context.selectedFamilies.has('tropical'))return 'Related tropical fruit';
  if(context.selectedFamilies.has('orchard'))return 'Related orchard fruit';
  if(context.selectedFamilies.has('citrus'))return 'Related citrus notes';
  if(context.selectedFamilies.has('herbal'))return 'Comparable cooling';
  if(context.selectedFamilies.has('tobacco'))return 'Related tobacco notes';
  return 'Related flavor notes';
}
function sensoryPhrase(context){
  if(context.target.tartness>=3.5 && context.profile.brightness>=4)return context.productNotes.some(note=>note.categories.includes('fruit'))?'Bright, acidic fruit':'Bright, tart characteristics';
  if(context.target.creaminess>=3.5)return 'Creamy dessert characteristics';
  if(context.target.cooling>=3.5)return 'Comparable sweetness & cooling';
  if(context.target.richness>=3.5)return 'Similar rich flavor';
  return 'Closely shared taste characteristics';
}
function matchFor(product, selected, mode) {
  const basis=matchBasis(product),inferred=basis==='inferred';
  if (!selected.length) return {kind:'browse',label:'Explore this flavor',score:0,
    reason:'Discover the full flavor profile.',basis,inferred};
  const productNotes=new Set(product.notes||[]);
  const matched=selected.filter(note=>productNotes.has(note.id));
  if(matched.length===selected.length){
    const names=selected.map(note=>note.label.toLowerCase()).join(' + ');
    return {kind:'exact',label:'Exact flavor match',score:100,
      reason:inferred?'Catalog suggests '+names+'.':'Classified with '+names+'.',
      shortReason:selected.map(note=>note.label).join(' + '),basis,inferred,
      matchedNotes:selected.map(note=>note.id),selectedNoteCount:selected.length,matchedNoteCount:matched.length};
  }
  if(mode==='exact')return null;
  const context=recommendationContext(product,selected);
  if(!context)return null;
  const partial=context.direct.length>0;
  if(context.familyCoverage===1 && context.identityAffinity>=.7 &&
      context.similarity>=.78 && context.preservesTaste && context.preservesShape){
    return recommendation(product,'related',partial?'Some selected notes':'Related flavor',
      familyPhrase(context),80+10*context.identityAffinity+9*context.similarity,context,selected);
  }
  if((context.semanticBridge||basis!=='inferred') && context.definingTaste.length && context.shared.length>=2 && context.preservesTaste &&
      context.preservesShape && context.similarity>=.84){
    return recommendation(product,'sensory','Similar profile',sensoryPhrase(context),
      60+19*context.similarity,context,selected);
  }
  const relatedIdentity=(context.familyCoverage>=.75 && context.identityAffinity>=.65 && context.similarity>=.74) ||
    (selected.length>1 && context.directCoverage>=.5 && context.similarity>=.76);
  const tartFruit=context.target.tartness>=4 && context.profile.tartness>=3 &&
    context.profile.brightness>=4 && context.similarity>=.78 &&
    selected.every(note=>note.categories.includes('fruit')) &&
    context.productNotes.some(note=>note.categories.includes('fruit'));
  if(relatedIdentity||tartFruit){
    return recommendation(product,'exploratory',partial?'Some selected notes':'Explore nearby',
      relatedIdentity?familyPhrase(context):'Tart fruit alternative',
      35+10*context.identityAffinity+14*context.similarity,context,selected);
  }
  return null;
}
function searchText(product,definitions) {
  return words([
    product.brand, product.name, product.description, product.formatLabel,
    ...(product.notes || []).map(id => noteById.get(id)?.label || definitions.get(id)?.label || id),
    ...(product.categories || []).map(id => categoryById.get(id)?.label || id),
  ].join(' '));
}
function noteDefinitions(value) {
  const result=new Map();
  const entries=value instanceof Map?[...value.entries()]:
    Array.isArray(value)?value.map(note=>[note?.id,note]):
    value && typeof value==='object'?Object.entries(value):[];
  for(const [id,definition] of entries){
    const label=typeof definition==='string'?definition:definition?.label;
    if(typeof id==='string' && id && typeof label==='string' && label.trim()){
      // Public dictionaries provide identity labels, not verified taste semantics.
      result.set(id,{id,label:label.trim()});
    }
  }
  return result;
}
function eligibleTypeAndStrength(product,type,strength) {
  return validTypes.has(product.type) && (type==='all'||product.type===type)
    && variationsFor(product,strength).length>0;
}
function reviewedRelationshipSource(product) {
  return matchBasis(product)!=='inferred' &&
    ['approved','established','existing_verified'].includes(product.classificationStatus);
}
function approvedRelationships(products,selected,{type,strength,anchorProductId}) {
  const matches=new Map();
  if(!selected.length && !anchorProductId)return matches;
  const anchors=products.filter(product=>
    eligibleTypeAndStrength(product,type,strength) && reviewedRelationshipSource(product) &&
    (anchorProductId?product.id===anchorProductId:
      selected.every(note=>(product.notes||[]).includes(note.id))));
  for(const anchor of anchors){
    for(const edge of Array.isArray(anchor.similarities)?anchor.similarities:[]){
      if(!edge || typeof edge.productId!=='string' || edge.productId===anchor.id ||
          typeof edge.weight!=='number' || !Number.isFinite(edge.weight) || edge.weight<=0 || edge.weight>1)continue;
      if(edge.inferred===true || edge.pending===true || edge.approved===false)continue;
      if([edge.status,edge.classificationStatus,edge.classificationConfidence].some(status=>
          status!=null && !['approved','established','existing_verified'].includes(String(status).toLowerCase())))continue;
      const edgeSource=edge.classificationSource??edge.source;
      if(edgeSource!=null && !['published','legacy_verified','approved','established','existing_verified'].includes(String(edgeSource).toLowerCase()))continue;
      const phrase=typeof edge.reason==='string' && edge.reason.trim()?edge.reason.trim():'Selected by our team.';
      const candidate={kind:'related',label:'Team recommendation',score:Math.round(90+9*edge.weight),
        reason:'Team recommendation: '+phrase,shortReason:'Team recommendation',
        basis:matchBasis(anchor),inferred:false,
        selectedNoteCount:selected.length,
        evidence:{approvedRelationship:true,sourceProductId:anchor.id,relationshipWeight:edge.weight}};
      const existing=matches.get(edge.productId);
      if(!existing || edge.weight>existing.evidence.relationshipWeight ||
          (edge.weight===existing.evidence.relationshipWeight &&
            (compareText(anchor.id,existing.evidence.sourceProductId)<0 ||
              (anchor.id===existing.evidence.sourceProductId && compareText(candidate.reason,existing.reason)<0)))){
        matches.set(edge.productId,candidate);
      }
    }
  }
  return matches;
}
/**
 * Product format and strength are immutable eligibility gates. Flavor relevance
 * runs only after those gates. This function neither mutates data nor makes IO.
 */
export function discover(products, options = {}) {
  const {
    type = 'all', strength = null, brand = '', sensory = [], notes = [], categories = [],
    query = '', mode = 'explore', storeId = 'hwy99', anchorProductId = null,
  } = options;
  if (type !== 'all' && !validTypes.has(type)) return [];
  const strengthValue = parsedStrength(strength);
  if (Number.isNaN(strengthValue)) return [];
  const noteIds = unique(Array.isArray(notes) ? notes : []);
  const categoryIds = unique(Array.isArray(categories) ? categories : []);
  const definitions=noteDefinitions(options.noteDefinitions);
  const catalogNoteIds=new Set(products.flatMap(product=>Array.isArray(product.notes)?product.notes:[]));
  if (noteIds.some(id => typeof id!=='string' || (!noteById.has(id) && !definitions.has(id) && !catalogNoteIds.has(id))) ||
      categoryIds.some(id => !categoryById.has(id))) return [];
  const selected = noteIds.map(id => noteById.get(id) || definitions.get(id) || {id,label:id});
  const approved=mode==='exact'?new Map():approvedRelationships(products,selected,{
    type,strength:strengthValue,anchorProductId,
  });
  const queryWords = words(query).trim().split(/\s+/).filter(Boolean);
  const results = [];
  for (const product of products) {
    if (!validTypes.has(product.type) || (type !== 'all' && product.type !== type)) continue;
    if (brand && product.brand !== brand) continue;
    if (sensory.some(axis => !Number.isFinite(product.sensory?.[axis]) || product.sensory[axis] < 3)) continue;
    const eligibleVariations = variationsFor(product, strengthValue);
    if (!eligibleVariations.length) continue;
    if (categoryIds.length && !categoryIds.some(id => (product.categories || []).includes(id))) continue;
    if (queryWords.length) {
      const text = searchText(product,definitions);
      if (!queryWords.every(word => text.includes(word))) continue;
    }
    let match = matchFor(product, selected, mode === 'exact' ? 'exact' : 'explore');
    const reviewed=approved.get(product.id);
    if(reviewed && match?.kind!=='exact'){
      const matched=selected.filter(note=>(product.notes||[]).includes(note.id));
      match={...reviewed,score:Math.max(reviewed.score,match?.kind==='related'?match.score:0),
        matchedNotes:matched.map(note=>note.id),matchedNoteCount:matched.length};
    }
    // An explicit no-note anchor is a recommendation view. Ordinary browse
    // remains unchanged and never labels its cards through dormant edges.
    if(mode!=='exact' && anchorProductId && !selected.length && !reviewed)continue;
    if (!match) continue;
    results.push({
      product, match, eligibleVariations,
      strengths: unique(eligibleVariations.map(variation => variation.strengthMg).filter(Number.isFinite)).sort((a,b) => a-b),
      availability: summarizeAvailability(eligibleVariations, storeId),
    });
  }
  const priority = { exact: 0, related: 1, sensory: 2, exploratory: 3, browse: 4 };
  return results.sort((a,b) =>
    priority[a.match.kind]-priority[b.match.kind] || b.match.score-a.match.score ||
    ({available:0,unknown:1,stale:2,check_staff:2,out_of_stock:3}[a.availability.state]??2)-({available:0,unknown:1,stale:2,check_staff:2,out_of_stock:3}[b.availability.state]??2) ||
    compareText(a.product.brand + ' ' + a.product.name, b.product.brand + ' ' + b.product.name) ||
    compareText(a.product.id,b.product.id));
}
