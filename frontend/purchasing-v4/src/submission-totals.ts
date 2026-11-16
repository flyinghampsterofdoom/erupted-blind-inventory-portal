import {decimal,plus,times,money,type Money} from './model.ts';
export function submissionTotals(review:any,choices:Record<string,{unit_value:string}>){
 const groups=review.groups.map((g:any)=>{
  const lines=review.lines.filter((l:any)=>l.supplier_id===g.supplier_id&&l.total_packs!==0);
  let total:Money={n:0n,scale:0},commitment:Money={n:0n,scale:0},known=true,approved=true;
  for(const l of lines){
   const chosen=g.kind==='PO'?choices[l.row_id]?.unit_value:undefined;
   const value=chosen??l.expected_unit_cost;
   if(g.kind==='PO'&&(chosen==null||chosen===''))approved=false;
   let cost:Money;
   try{if(l.total_units==null||value==null||value==='')throw Error('Unknown cost or quantity');cost=times(decimal(String(value)),BigInt(l.total_units));}
   catch{known=false;if(g.kind==='PO')approved=false;continue;}
   total=plus(total,cost);if(g.kind==='PO'&&chosen!=null&&chosen!=='')commitment=plus(commitment,cost);
  }
  return {id:g.supplier_id,name:g.profile.name,kind:g.kind,products:g.positive_lines,packs:lines.some((l:any)=>l.total_packs==null)?null:lines.reduce((n:number,l:any)=>n+l.total_packs,0),total,commitment,known,approved};
 });
 const sum=(key:'total'|'commitment')=>groups.reduce((n:Money,g:any)=>plus(n,g[key]),{n:0n,scale:0});
 return {groups,orderTotal:groups.every((g:any)=>g.known)?money(sum('total')):'Unknown',committedTotal:groups.every((g:any)=>g.approved)?money(sum('commitment')):'Unknown'};
}
