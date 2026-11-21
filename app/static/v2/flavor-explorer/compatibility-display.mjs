
/** Presentation only. Original product names, catalog associations and SKU stay intact.
 * Locally corrected concise names pass through unchanged. Strip packaging prose,
 * never model numbers, generations, resistance or compatibility qualifiers.
 */
export function conciseName(product){
 let name=String(product.name||'').trim();
 if(!/\b(?:replacement|cartridge|\d+\s*[- ]?packs?)\b/i.test(name))return name;
 name=name.replace(/\b\d+(?:\.\d+)?\s*m[lL]\b/g,'')
  .replace(/\b\d+\s*[- ]?packs?\b/ig,'')
  .replace(/\breplacement\b/ig,'')
  .replace(/\bpod\s+cartridges?\b/ig,'Pods')
  .replace(/\bcartridges?\b/ig,'Pods')
  .replace(/\s+/g,' ').replace(/^[\s·,|/-]+|[\s·,|/-]+$/g,'').trim();
 return name||String(product.name||'');
}
export function relationshipGroup(kind){
 return kind==='device'?'Verified Compatible Devices':
  ['coil','pod'].includes(kind)?'Verified Compatible Coils & Pods':
  kind==='tank'?'Verified Compatible Tanks':'Other Verified Compatibility';
}
