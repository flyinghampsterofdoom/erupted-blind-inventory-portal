import {Store,type View,type Row,moneyExact,decimal,times} from './model.ts';
type StoreLabel={id:number;name:string};
/** Full filtered worksheet, independent of the viewport, collapsed groups or page. */
export function worksheetRows(store:Store,view:View):Row[]{return store.visible({...view,collapsed:[]}).ids.map(id=>store.rows.get(id)!);}
function cells(row:Row,showCosts:boolean):string[]{const offer=row.supplier_id?row.offers[row.supplier_id]:undefined;const result=[offer?.name??'Supplier unresolved',row.name,row.variation,row.sku??'',row.pack_size===null?'Unresolved':String(row.pack_size),...row.packs.map(p=>p===null?'Unresolved':String(p)),row.packs.some(p=>p===null)?'Unresolved':String(row.packs.reduce<number>((n,p)=>n+(p??0),0))];if(showCosts)result.push(offer?.expected_unit_cost==null||row.pack_size===null?'Unknown':moneyExact(times(decimal(offer.expected_unit_cost),BigInt(row.pack_size))));return result;}
function headings(stores:StoreLabel[],showCosts:boolean){return ['Vendor','Product','Variation','SKU','Units per pack',...stores.map(s=>s.name+' (packs)'),'Total packs',...(showCosts?['Pack cost']:[])];}
const csv=(value:string)=>'"'+(/^[\s]*[=+@-]/.test(value)?"'"+value:value).replace(/"/g,'""')+'"';
const html=(value:string)=>value.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
export function worksheetCsv(store:Store,view:View,stores:StoreLabel[]):string{return [headings(stores,view.showCosts),...worksheetRows(store,view).map(r=>cells(r,view.showCosts))].map(row=>row.map(csv).join(',')).join('\r\n')+'\r\n';}
export function worksheetPrint(title:string,store:Store,view:View,stores:StoreLabel[]):string{return '<h1>'+html(title)+'</h1><table><thead><tr>'+headings(stores,view.showCosts).map(c=>'<th>'+html(c)+'</th>').join('')+'</tr></thead><tbody>'+worksheetRows(store,view).map(r=>'<tr>'+cells(r,view.showCosts).map(c=>'<td>'+html(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
