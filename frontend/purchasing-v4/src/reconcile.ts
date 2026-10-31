import {working,type Action,type Change,type DefaultChange,type Snapshot,type Working} from './model.ts';
export type EditConflict={id:string;fields:string[]};
export type Reconciled={changes:Change[];defaults:DefaultChange[];conflicts:EditConflict[]};
const equal=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);
/** Merge only explicit local edits; newer saved fields never get copied over. */
export function reconcile(server:Snapshot,actions:Action[],ignoreRows:string[]=[],choice:'ask'|'local'|'server'='ask'):Reconciled{
 const ignored=new Set(ignoreRows),rows=new Map(server.rows.map(r=>[r.id,r])),edits=new Map<string,{before:Working;after:Working}>(),policies=new Map<string,{before:string|null;after:string|null;version:number;id:string}>(),conflicts=new Map<string,Set<string>>();
 const problem=(id:string,field:string)=>{if(!conflicts.has(id))conflicts.set(id,new Set());conflicts.get(id)!.add(field);};
 for(const action of actions.filter(a=>(a.status==='pending'||a.status==='choice')).sort((a,b)=>a.seq-b.seq)){
  for(const change of action.changes){if(ignored.has(change.id))continue;const before=action.inverse.find(c=>c.id===change.id)?.value;if(!before){problem(change.id,'Saved edit history');continue;}const entry=edits.get(change.id)??{before:working(before),after:working(before)},wanted=change.value;
   const compose=(name:string,old:unknown,next:unknown,current:unknown,apply:()=>void)=>{if(equal(old,next))return;if(!equal(current,old)&&!equal(current,next))problem(change.id,name);apply();};
   compose('Vendor / pack size',[before.supplier_id,before.pack_size],[wanted.supplier_id,wanted.pack_size],[entry.after.supplier_id,entry.after.pack_size],()=>{entry.after.supplier_id=wanted.supplier_id;entry.after.pack_size=wanted.pack_size;});
   for(let i=0;i<4;i++)compose(server.session.stores?.[i]?.name??'Store '+(i+1),[before.packs[i],before.touched[i]],[wanted.packs[i],wanted.touched[i]],[entry.after.packs[i],entry.after.touched[i]],()=>{entry.after.packs[i]=wanted.packs[i];entry.after.touched[i]=wanted.touched[i];});
   compose('Included in this worksheet',before.removed,wanted.removed,entry.after.removed,()=>{entry.after.removed=wanted.removed;});edits.set(change.id,entry);}
  for(const d of action.defaults){const row=server.rows.find(r=>r.variation_id===d.variation_id);if(!row||ignored.has(row.id))continue;const prior=policies.get(d.variation_id),inverse=action.inverseDefaults.find(v=>v.variation_id===d.variation_id);policies.set(d.variation_id,{before:prior?.before??inverse?.supplier_id??null,after:d.supplier_id,version:prior?.version??d.expected_version,id:row.id});}
 }
 const changes:Change[]=[];const defaults:DefaultChange[]=[];
 for(const [id,{before,after}] of edits){
  const row=rows.get(id);if(!row){problem(id,'Item no longer available');continue;}
  // A newer removal wins over stale quantity edits. Keep those edits in recovery,
  // but never silently put a stopped/deleted product back into the worksheet.
  if(row.removed&&!before.removed)continue;
  const saved=working(row),merged=working(row),supplyChanged=before.supplier_id!==after.supplier_id||before.pack_size!==after.pack_size,remoteSupplyChanged=before.supplier_id!==saved.supplier_id||before.pack_size!==saved.pack_size;
  const take=(field:string,old:unknown,wanted:unknown,current:unknown,apply:()=>void,dependency=false)=>{
   if(equal(old,wanted))return;
   if(!dependency&&(equal(current,old)||equal(current,wanted))){apply();return;}
   problem(id,field);if(choice==='local'&&!row.sealed)apply();
  };
  if(row.sealed&&!equal(saved,after)){problem(id,'Item is already authorized');continue;}
  if(supplyChanged){
   const currentPack=after.supplier_id?row.pack_sizes?.[after.supplier_id]:undefined;
   const packChangedElsewhere=currentPack!==undefined&&(after.supplier_id===before.supplier_id?currentPack!==before.pack_size:currentPack!==after.pack_size);
   take('Vendor / pack size',[before.supplier_id,before.pack_size],[after.supplier_id,after.pack_size],[saved.supplier_id,saved.pack_size],()=>{merged.supplier_id=after.supplier_id;merged.pack_size=after.pack_size;},packChangedElsewhere);
  }
  for(let i=0;i<4;i++)take(server.session.stores?.[i]?.name??'Store '+(i+1),[before.packs[i],before.touched[i]],[after.packs[i],after.touched[i]],[saved.packs[i],saved.touched[i]],()=>{merged.packs[i]=after.packs[i];merged.touched[i]=after.touched[i];},supplyChanged&&!equal([merged.supplier_id,merged.pack_size],[after.supplier_id,after.pack_size])||remoteSupplyChanged&&!equal([saved.supplier_id,saved.pack_size],[after.supplier_id,after.pack_size])||supplyChanged&&!equal([saved.packs[i],saved.touched[i]],[before.packs[i],before.touched[i]])&&!equal([saved.packs[i],saved.touched[i]],[after.packs[i],after.touched[i]]));
  take('Included in this worksheet',before.removed,after.removed,saved.removed,()=>{merged.removed=after.removed;});
  if(!equal(merged,saved))changes.push({id,value:merged});
 }
 for(const [variation,p] of policies){const row=rows.get(p.id)!;if(row.default_supplier_id===p.after)continue;if(row.assignment_version!==p.version||row.default_supplier_id!==p.before){problem(p.id,'Default vendor');if(choice!=='local')continue;}defaults.push({variation_id:variation,supplier_id:p.after,expected_version:row.assignment_version});}
 return {changes,defaults,conflicts:[...conflicts].map(([id,fields])=>({id,fields:[...fields]}))};
}
