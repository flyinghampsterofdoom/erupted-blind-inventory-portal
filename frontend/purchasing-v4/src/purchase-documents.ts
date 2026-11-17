import type {Row} from './model.ts';
type Context={rows:()=>Row[];session:()=>{id:string;version:number};ready:()=>Promise<void>};
const escape=(v:unknown)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
export const orderDocumentUrl=(id:number)=>'/api/purchasing-v4/orders/'+encodeURIComponent(id)+'/documents/preview';

/** Full saved vendor contents, independent of grid filters or the rendered page. */
export function openPurchaseDocuments(context:Context){
 const dialog=document.createElement('dialog');dialog.className='submission-dialog';dialog.id='purchase-documents-dialog';
 const vendors=new Map<string,{name:string;complete:boolean;positive:boolean}>(),orders=new Map<number,string>();
 for(const row of context.rows()){
  if(row.removed)continue;
  const supplier=row.supplier_id,offer=supplier?row.offers[supplier]:undefined,name=offer?.name??'Vendor';
  const po=row.sealed?.purchase_order_id??row.sealed?.resolved_purchase_order_id;
  if(po)orders.set(po,name);
  if(!row.sealed&&supplier){
   const current=vendors.get(supplier)??{name,complete:true,positive:false};
   current.complete&&=row.packs.every(p=>p!==null)&&(!row.packs.some(p=>p!==null&&p>0)||row.pack_size!==null);
   current.positive||=row.packs.some(p=>p!==null&&p>0);vendors.set(supplier,current);
  }
 }
 dialog.innerHTML='<form method="dialog"><div class="dialog-heading"><h2>Print / PDF</h2><button aria-label="Close purchase documents">×</button></div><div class="dialog-body"><p>Choose a vendor draft or a recorded purchase order. Documents include all positive quantities for that vendor, including rows outside the current search or page.</p>'+
 (vendors.size?'<h3>Current vendor drafts</h3><p>Drafts show saved edits and expected costs. Printing does not issue an order.</p>'+[...vendors].map(([id,vendor])=>'<p><button type="button" data-document-supplier="'+escape(id)+'" '+(!vendor.complete||!vendor.positive?'disabled':'')+'>'+escape(vendor.name)+' · Print / PDF</button>'+(!vendor.complete?' <span>Resolve quantities and pack sizes in the worksheet first.</span>':!vendor.positive?' <span>No positive quantities to print.</span>':'')+'</p>').join(''):'')+
 (orders.size?'<h3>Recorded purchase orders</h3>'+[...orders].map(([id,name])=>'<p><a target="_blank" rel="noopener" href="'+orderDocumentUrl(id)+'">'+escape(name)+' · PO #'+id+' · Print / PDF</a></p>').join(''):'')+
 (!vendors.size&&!orders.size?'<p>No vendor purchase documents are available in this worksheet.</p>':'')+
 '<p id="purchase-documents-error" role="alert"></p></div><div class="dialog-footer"><button>Close</button></div></form>';
 document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
 dialog.querySelectorAll<HTMLButtonElement>('[data-document-supplier]').forEach(button=>button.onclick=async()=>{
  // Open in the click handler before awaiting saves (Safari/Chrome popup rules).
  const preview=window.open('about:blank','_blank');
  if(!preview){dialog.querySelector('#purchase-documents-error')!.textContent='Allow the document tab to open, then try again.';return;}
  preview.opener=null;preview.document.body.textContent='Preparing saved purchase document…';button.disabled=true;
  try{await context.ready();const session=context.session();preview.location.href='/api/worksheets/'+encodeURIComponent(session.id)+'/purchase-documents/'+encodeURIComponent(button.dataset.documentSupplier!)+'/preview?version='+session.version;}
  catch(error){preview.close();dialog.querySelector('#purchase-documents-error')!.textContent=(error as Error).message;}
  finally{button.disabled=false;}
 });
}
