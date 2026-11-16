// Resolve against the pre-commit order: committing can hide the current row.
export function navigationTarget(previous:string[],visible:string[],id:string,col:number,dy:number,dx:number):{id:string;col:number}|undefined {
 const index=previous.indexOf(id);
 if(index<0||!visible.length)return;
 const direction=dy||dx||1;
 let next=index+dy,c=col+dx;
 if(c>3){c=0;next++;}
 if(c<0){c=3;next--;}
 // A disappearing row has no next cell within itself.
 if(next===index&&!visible.includes(id)){next+=direction>0?1:-1;c=direction>0?0:3;}
 const shown=new Set(visible);
 for(let i=next;i>=0&&i<previous.length;i+=direction>0?1:-1){
  if(shown.has(previous[i]))return {id:previous[i],col:c};
 }
 // At a boundary, stay at the nearest surviving row instead of wrapping.
 for(let i=Math.min(previous.length-1,Math.max(0,next));i>=0&&i<previous.length;i+=direction>0?-1:1){
  if(shown.has(previous[i]))return {id:previous[i],col:c};
 }
}
