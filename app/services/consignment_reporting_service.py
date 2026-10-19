"""Read-only consignment views: current configured vendor, Square quantities, local cost.

No financial posting, historical ownership, PO/receipt attribution or database writes.
"""
from collections import defaultdict, Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from sqlalchemy import select
from app.models import Vendor, VendorSkuConfig, OrderingCatalogIdentity, Store
from app.services.consignment_product_report import number, sku_key
from app.services.vendor_product_ownership import single_default_mapping
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
from app.services.v2_ordering_square_gateway import _parse_datetime
from app.services.v2_consignment_facts_service import SquareOrdersReader
from app.services.square_ordering_data_service import _square_get

ZERO=Decimal(0)


def money(value):
    return value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)


def configured_products(db, vendor_id):
    """Reuse the existing Primary / Reporting Vendor assignment, never financing lots."""
    vendor=db.get(Vendor,vendor_id)
    if vendor is None or not vendor.active:raise ValueError('Choose an active consignment vendor.')
    stores=list(db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.id)))
    identities={r.square_variation_id:r for r in db.scalars(select(OrderingCatalogIdentity))}
    by_sku=defaultdict(set)
    for vid,row in identities.items():
        if row.sku:by_sku[sku_key(row.sku)].add(vid)
    mappings=list(db.scalars(select(VendorSkuConfig).join(Vendor).where(Vendor.active.is_(True)).order_by(VendorSkuConfig.id)))
    paths=defaultdict(list);keys={}
    for m in mappings:
        vid=m.square_variation_id
        if not vid and len(by_sku[sku_key(m.sku)])==1:vid=next(iter(by_sku[sku_key(m.sku)]))
        keys[m.id]=vid
        if vid and m.active:paths[vid].append(m)
    rows=[];seen=set()
    for m in mappings:
        if m.vendor_id!=vendor_id:continue
        vid=keys[m.id];identity=identities.get(vid);owner=single_default_mapping(paths[vid]) if vid else None
        state='inactive' if not m.active else 'assigned' if owner is m else 'secondary' if not m.is_default_vendor else 'unresolved'
        key=vid or f'mapping:{m.id}'
        if state=='assigned' and key in seen:continue
        if state=='assigned':seen.add(key)
        cost=number(m.unit_cost)
        if cost is not None and cost<0:cost=None
        rows.append({'key':key,'variation_id':vid,'mapping_id':m.id,'sku':m.sku,
            'name':(identity.product_name or identity.item_name or m.sku) if identity else m.sku,
            'variation':identity.variation_name or '' if identity else '',
            'unit_cost':cost,'assignment':state,'assigned':state=='assigned',
            'identity_missing':not vid,'current_assignment_issue':state=='unresolved'})
    return vendor,stores,rows


def inventory_value(db, *, vendor_id, gateway=None):
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    vendor,stores,products=configured_products(db,vendor_id)
    scope=[p for p in products if p['assigned'] or p['current_assignment_issue']]
    location_counts=Counter(s.square_location_id for s in stores if s.square_location_id)
    locations=[location for location,count in location_counts.items() if count==1]
    counts=(gateway or SquareOrderingReadGateway()).fetch_current_inventory_counts(
        location_ids=locations,variation_ids=[p['variation_id'] for p in scope if p['variation_id']],preserve_partial=True).counts if scope else {}
    read_at=datetime.now(timezone.utc);summary=[];details=[];issues=[];negatives=[]
    for store in stores:
        store_rows=[]
        for product in scope:
            count=counts.get((store.square_location_id,product['variation_id'])) if store.square_location_id in locations else None
            qty=number(count.quantity) if count else None
            positive=max(ZERO,qty) if qty is not None else None
            issue=None
            if product['current_assignment_issue']:issue='Current primary vendor mapping needs correction'
            elif qty is None:issue=f'Current Square quantity unknown for location {store.square_location_id or "unconfigured"}'
            elif positive>0 and product['unit_cost'] is None:issue='Configured vendor unit cost missing or invalid'
            value=positive*product['unit_cost'] if positive is not None and product['unit_cost'] is not None else ZERO if positive==0 else None
            if issue:value=None;issues.append({'sku':product['sku'],'store':store.name,'reason':issue})
            row={**product,'store_id':str(store.id),'store_name':store.name,'quantity':qty,'positive_units':positive,
                 'value':value,'issue':issue,'negative':qty is not None and qty<0,'observed_at':count.calculated_at if count else None}
            if row['negative']:negatives.append({'sku':product['sku'],'store':store.name,'quantity':qty})
            details.append(row);store_rows.append(row)
        known=sum((r['value'] for r in store_rows if r['value'] is not None),ZERO)
        complete=all(r['value'] is not None for r in store_rows)
        summary.append({'store_id':str(store.id),'name':store.name,'positive_units':sum((r['positive_units'] or ZERO for r in store_rows),ZERO),
                        'known_value':known,'value':known if complete else None,'complete':complete})
    known=sum((s['known_value'] for s in summary),ZERO);complete=all(s['complete'] for s in summary)
    return {'vendor':vendor,'assigned_skus':len(scope),'positive_units':sum((s['positive_units'] for s in summary),ZERO),
            'value':known if complete else None,'known_value':known,'complete':complete,'stores':summary,'rows':details,
            'issues':issues,'negative_observations':negatives,'products':products,'read_at':read_at}


def inventory_table(view, *, query='', store='', status='all', assignment='assigned', sort='name', page=1, page_size=25):
    if page_size not in (25,50):raise ValueError('Choose 25 or 50 rows per page.')
    if assignment not in ('assigned','all','secondary','inactive','unresolved'):raise ValueError('Invalid assignment filter.')
    if status not in ('all','positive','zero','negative','unknown'):raise ValueError('Invalid inventory status.')
    if sort not in ('name','sku','units_desc','value_desc'):raise ValueError('Invalid sort.')
    if store and store not in {s['store_id'] for s in view['stores']}:raise ValueError('Choose an active store.')
    tokens=query.casefold().split();rows=[];by_mapping=defaultdict(list)
    for observation in view['rows']:by_mapping[observation['mapping_id']].append(observation)
    for product in view['products']:
        if assignment!='all' and product['assignment']!=assignment:continue
        if any(t not in ' '.join(str(product[k] or '') for k in ('sku','name','variation')).casefold() for t in tokens):continue
        observations=[r for r in by_mapping[product['mapping_id']] if not store or r['store_id']==store]
        positive=sum((r['positive_units'] or ZERO for r in observations),ZERO)
        missing=not observations or any(r['quantity'] is None for r in observations)
        negative=any(r['negative'] for r in observations)
        matches={'all':True,'positive':positive>0,'zero':not missing and not negative and positive==0,'negative':negative,'unknown':missing or any(r['issue'] for r in observations)}
        if not matches[status]:continue
        complete=bool(observations) and all(r['value'] is not None for r in observations)
        rows.append({**product,'positive_units':positive,'inventory_unknown':missing,'negative':negative,
                     'value':sum((r['value'] for r in observations),ZERO) if complete else None})
    rows.sort(key=lambda r:(r['sku'].casefold(),r['mapping_id']) if sort=='sku' else (-r['positive_units'],r['name']) if sort=='units_desc' else (r['value'] is None,-(r['value'] or ZERO),r['name']) if sort=='value_desc' else (r['name'].casefold(),r['variation'],r['sku']))
    total=len(rows);pages=max(1,(total+page_size-1)//page_size);page=max(1,min(page,pages))
    return {'rows':rows[(page-1)*page_size:page*page_size],'total':total,'page':page,'pages':pages,'page_size':page_size,
            'query':query,'store':store,'status':status,'assignment':assignment,'sort':sort}


def sales_report(db, *, vendor_id, start_date, end_date, reader=None, retrieve_order=None):
    if not isinstance(start_date,date) or end_date<start_date or end_date>datetime.now(BUSINESS_TIMEZONE).date():raise ValueError('Choose a valid, non-future reporting period.')
    vendor,stores,products=configured_products(db,vendor_id)
    scope={p['variation_id']:p for p in products if p['assigned'] and p['variation_id']}
    issues=[{'sku':p['sku'],'reason':'Current assignment or Square variation ID requires correction'} for p in products if p['current_assignment_issue']]
    by_sku=defaultdict(set)
    for vid,p in scope.items():by_sku[sku_key(p['sku'])].add(vid)
    start=datetime.combine(start_date,time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    stop=datetime.combine(end_date+timedelta(days=1),time.min,BUSINESS_TIMEZONE).astimezone(timezone.utc)
    locations={s.square_location_id:s for s in stores if s.square_location_id}
    if len(locations)!=len(stores):raise ValueError('An active store lacks its Square location; report is unavailable.')
    source=reader or SquareOrdersReader();orders={};cursor=start
    # Exhaust bounded intervals; the end is exclusive in our final filtering.
    while cursor<stop:
        end=min(cursor+timedelta(days=7),stop)
        for page in source.search(location_ids=list(locations),start_at=cursor,end_at=end,date_filter='CLOSED_AT'):
            for order in page:
                oid=order.get('id');stamp=_parse_datetime(order.get('updated_at') or order.get('closed_at'))
                if not oid or not stamp:raise ValueError('Square returned an order without stable ID or timestamp.')
                old=orders.get(oid)
                if old is None or stamp>_parse_datetime(old.get('updated_at') or old.get('closed_at')):orders[oid]=order
        cursor=end
    retrieve=retrieve_order or (lambda oid:_square_get('/v2/orders/'+quote(oid,safe=''))['order'])
    originals={};activity=defaultdict(lambda:{'sold':ZERO,'returned':ZERO});amount_refunds=[];completed=0
    def identity(line):
        vid=line.get('catalog_object_id')
        if vid:return vid
        matches=by_sku[sku_key(line.get('sku'))]
        return next(iter(matches)) if len(matches)==1 else None
    for oid,order in orders.items():
        if order.get('state')!='COMPLETED':continue
        stamp=_parse_datetime(order.get('closed_at'))
        if not stamp:raise ValueError(f'Square completed order {oid} is missing closed_at.')
        if not start<=stamp<stop:continue
        loc=order.get('location_id')
        if loc not in locations:raise ValueError(f'Square order {oid} has an unexpected location.')
        completed+=1
        for line in order.get('line_items') or []:
            vid=identity(line)
            if vid not in scope:continue
            qty=number(line.get('quantity'))
            if qty is None or qty<0:issues.append({'sku':scope[vid]['sku'],'reason':f'Invalid sale quantity in Square order {oid}'});continue
            activity[(vid,loc)]['sold']+=qty
        for returned in order.get('returns') or []:
            for line in returned.get('return_line_items') or []:
                if line.get('item_type')=='CUSTOM_AMOUNT':
                    amount_refunds.append(oid);continue
                vid=identity(line)
                if not vid and returned.get('source_order_id') and line.get('source_line_item_uid'):
                    original_id=returned['source_order_id']
                    if original_id not in originals:originals[original_id]=orders.get(original_id) or retrieve(original_id)
                    original=originals[original_id]
                    if original.get('id')!=original_id:raise ValueError('Square original return reference did not match requested order.')
                    source_line=next((r for r in original.get('line_items',[]) if r.get('uid')==line['source_line_item_uid']),None)
                    if source_line:vid=identity(source_line)
                if vid not in scope:
                    if not vid:issues.append({'sku':None,'reason':f'Unmatched item return in Square order {oid}; vendor membership cannot be established'})
                    continue
                qty=number(line.get('quantity'))
                if qty is None or qty<0:issues.append({'sku':scope[vid]['sku'],'reason':f'Invalid returned quantity in Square order {oid}'});continue
                activity[(vid,loc)]['returned']+=qty
        if any(r.get('status','COMPLETED')=='COMPLETED' for r in order.get('refunds',[])) and not order.get('returns'):amount_refunds.append(oid)
    lines=[]
    for (vid,loc),a in sorted(activity.items()):
        product=scope[vid];net=a['sold']-a['returned'];cost=product['unit_cost'];value=net*cost if cost is not None else None
        if value is None:issues.append({'sku':product['sku'],'reason':'Configured vendor unit cost missing or invalid'})
        lines.append({**product,'store_id':str(locations[loc].id),'store_name':locations[loc].name,**a,'net_units':net,'cogs':value})
    store_totals=[]
    for loc,store in locations.items():
        rows=[r for r in lines if r['store_id']==str(store.id)];known=sum((r['cogs'] for r in rows if r['cogs'] is not None),ZERO)
        store_totals.append({'store_id':str(store.id),'name':store.name,'sold':sum((r['sold'] for r in rows),ZERO),'returned':sum((r['returned'] for r in rows),ZERO),
            'net_units':sum((r['net_units'] for r in rows),ZERO),'known_cogs':known,'cogs':known if all(r['cogs'] is not None for r in rows) else None})
    known=sum((r['known_cogs'] for r in store_totals),ZERO)
    return {'vendor':vendor,'start_date':start_date,'end_date':end_date,'start_at':start,'end_exclusive':stop,'stores':store_totals,
            'assigned_skus':len(scope),'orders_checked':completed,'sold':sum((r['sold'] for r in store_totals),ZERO),
            'returned':sum((r['returned'] for r in store_totals),ZERO),'net_units':sum((r['net_units'] for r in store_totals),ZERO),
            'cogs':known if not issues else None,'known_cogs':known,'complete':not issues,'issues':issues,
            'amount_only_refunds':len(set(amount_refunds)),'rows':lines,'calculated_at':datetime.now(timezone.utc)}
