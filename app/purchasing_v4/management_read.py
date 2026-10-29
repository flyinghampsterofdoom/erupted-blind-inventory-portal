"""Bounded management projections. Reads do not refresh or establish authority."""
import json,re
from sqlalchemy import text
from .identity import digest
from .isolation import require_isolated
from .controls import heads,live


def catalog_projection(connection,*,source_namespace):
    require_isolated(connection)
    collisions=connection.execute(text("SELECT 1 FROM purchasing_v4.supplier f JOIN purchasing_v4.authority_supplier a ON a.id=f.id WHERE a.origin!='FOUNDATION' OR a.foundation_supplier_id IS DISTINCT FROM f.id LIMIT 1")).scalar_one_or_none()
    if collisions:raise ValueError('Immutable local/foundation supplier identity collision; review required')
    connection.execute(text("INSERT INTO purchasing_v4.authority_supplier(id,foundation_supplier_id,origin,provenance) SELECT id,id,'FOUNDATION','Explicit observed identity intake' FROM purchasing_v4.supplier ON CONFLICT(id) DO NOTHING"))
    rows=connection.execute(text('''SELECT c.*,r.variation_id::text AS internal_id FROM public.ordering_catalog_identity c
      JOIN purchasing_v4.variation_reference r ON r.square_variation_id=c.square_variation_id AND r.source_namespace=:ns
      ORDER BY r.variation_id'''),{'ns':source_namespace}).mappings().all()
    for row in rows:
        label=(row['item_name'] or row['product_name'] or 'Unnamed observed product')+' / '+(row['variation_name'] or '')
        connection.execute(text('''INSERT INTO purchasing_v4.management_catalog(variation_id,square_variation_id,source_namespace,label,sku,categories,source_digest,search_vector,observed_at)
        VALUES (:id,:square,:ns,:label,:sku,CAST(:categories AS jsonb),:sha,to_tsvector('simple',:search),:seen)
        ON CONFLICT(variation_id) DO UPDATE SET label=EXCLUDED.label,sku=EXCLUDED.sku,categories=EXCLUDED.categories,
        source_digest=EXCLUDED.source_digest,search_vector=EXCLUDED.search_vector,observed_at=EXCLUDED.observed_at'''),
        dict(id=row['internal_id'],square=row['square_variation_id'],ns=source_namespace,label=label,sku=row['sku'],
             categories=json.dumps((row['catalog_attributes'] or {}).get('category_ids',[])),sha=digest(dict(row)),search=label+' '+(row['sku'] or '')+' '+str((row['catalog_attributes'] or {}).get('gtin') or ''),seen=row['last_seen_at']))
    freeze_legacy_manual(connection,source_namespace=source_namespace)
    return len(rows)


def import_review_evidence(connection,entries,*,source_digest):
    require_isolated(connection)
    for category,payload in entries:
        key=digest(dict(category=category,payload=payload,source_digest=source_digest))
        connection.execute(text('''INSERT INTO purchasing_v4.review_evidence(id,category,payload,source_digest)
          VALUES (:id,:category,CAST(:payload AS jsonb),:sha) ON CONFLICT(id) DO NOTHING'''),
          dict(id=key,category=category,payload=json.dumps(payload,sort_keys=True,default=str),sha=source_digest))


def list_catalog(connection,*,search='',after='',limit=50):
    if type(limit) is not int or not 1<=limit<=100 or len(search)>200:raise ValueError('Bounded page/search required')
    tokens=re.findall(r'[a-z0-9]+',search.lower())
    query=' & '.join(token+':*' for token in tokens)
    rows=connection.execute(text('''SELECT c.variation_id::text,c.label,c.sku,c.categories,coalesce(l.status::text='NO_FUTURE_REORDER',false) AS observed_no_reorder FROM purchasing_v4.management_catalog c
      LEFT JOIN public.ordering_product_lifecycle l ON l.square_variation_id=c.square_variation_id
      WHERE (:after='' OR variation_id>CAST(NULLIF(:after,'') AS uuid)) AND (:query='' OR search_vector @@ to_tsquery('simple',:query))
      ORDER BY variation_id LIMIT :limit'''),dict(after=after,query=query,limit=limit+1)).mappings().all()
    page=rows[:limit];keys={('PRODUCT',r['variation_id']) for r in page}|{('ASSIGNMENT',r['variation_id']) for r in page}
    state=heads(connection,keys)
    dependencies=set()
    for (kind,key),record in state.items():
        assignment=live(record)
        if kind=='ASSIGNMENT' and assignment and assignment['supplier_id']:
            dependencies|={('SUPPLIER',assignment['supplier_id']),('OFFER',key+':'+assignment['supplier_id'])}
    related=heads(connection,dependencies);result=[]
    for row in page:
        assignment=live(state.get(('ASSIGNMENT',row['variation_id'])))
        sid=assignment['supplier_id'] if assignment else None
        supplier=live(related.get(('SUPPLIER',sid))) if sid else None
        offer=live(related.get(('OFFER',row['variation_id']+':'+sid))) if sid else None
        authority='OWNER_ASSIGNED' if supplier and supplier['active'] and offer and offer['approved'] else 'UNRESOLVED_AUTHORITY'
        result.append(dict(row,assignment_name=supplier['name'] if supplier else None,assignment_state=authority,
            owner_pack_size=offer['pack_size'] if offer else None,owner_cost_unknown=not offer or offer['expected_unit_cost'] is None,
            product=state.get(('PRODUCT',row['variation_id'])),assignment=state.get(('ASSIGNMENT',row['variation_id']))))
    return dict(rows=result,next_cursor=page[-1]['variation_id'] if len(rows)>limit else None)



def list_manual(connection,*,search='',store_id=None,state=None,after='',limit=50):
    if type(limit) is not int or not 1<=limit<=100 or len(search)>200:raise ValueError('Bounded page/search required')
    tokens=' & '.join(t+':*' for t in re.findall(r'[a-z0-9]+',search.lower()))
    rows=connection.execute(text('''SELECT h.*,c.label,c.sku,s.name AS store_name FROM purchasing_v4.control_head h
      JOIN purchasing_v4.management_catalog c ON c.variation_id=(h.payload->>'variation_id')::uuid
      JOIN public.stores s ON s.id=(h.payload->>'store_id')::bigint
      WHERE h.kind='MANUAL' AND h.record_key>:after AND (CAST(:store AS text) IS NULL OR h.payload->>'store_id'=:store)
      AND (CAST(:state AS text) IS NULL OR h.payload->>'state'=:state)
      AND (:query='' OR c.search_vector @@ to_tsquery('simple',:query))
      ORDER BY h.record_key LIMIT :limit'''),dict(after=after,store=str(store_id) if store_id else None,state=state,query=tokens,limit=limit+1)).mappings().all()
    page=rows[:limit]
    return dict(rows=[dict(r) for r in page],next_cursor=page[-1]['record_key'] if len(rows)>limit else None)


def supplier_context(connection,variation_id):
    assignment=heads(connection,{('ASSIGNMENT',variation_id)}).get(('ASSIGNMENT',variation_id));value=live(assignment)
    if not value or not value['supplier_id']:return dict(state='UNRESOLVED_DEFAULT',supplier_id=None,pack_size=None,expected_unit_cost=None)
    supplier_id=value['supplier_id'];rows=heads(connection,{('SUPPLIER',supplier_id),('OFFER',variation_id+':'+supplier_id)})
    supplier=live(rows.get(('SUPPLIER',supplier_id)));offer=live(rows.get(('OFFER',variation_id+':'+supplier_id)))
    if not supplier or not supplier['active'] or not offer or not offer['approved']:
        return dict(state='UNRESOLVED_AUTHORITY',supplier_id=supplier_id,pack_size=None,expected_unit_cost=None)
    return dict(state='OWNER_ASSIGNED',supplier_id=supplier_id,pack_size=offer['pack_size'],expected_unit_cost=offer['expected_unit_cost'],cost_provenance=offer['cost_provenance'],assignment_version=assignment['version'])


def list_legacy_manual(connection,*,after=0,search='',limit=50,source_namespace):
    if not 1<=limit<=100 or len(search)>200:raise ValueError('Bounded legacy page required')
    rows=connection.execute(text('''SELECT p.*,s.name AS store_name,x.ids AS possible_variations,f.observation AS frozen_observation,f.source_digest AS frozen_source_digest FROM public.par_levels p
      LEFT JOIN public.stores s ON s.id=p.store_id
      LEFT JOIN purchasing_v4.legacy_manual_evidence f ON f.source_namespace=:ns AND f.legacy_id=p.id
      LEFT JOIN LATERAL(SELECT array_agg(DISTINCT r.variation_id::text ORDER BY r.variation_id::text) AS ids
       FROM public.vendor_sku_configs c JOIN purchasing_v4.variation_reference r ON r.square_variation_id=c.square_variation_id AND r.source_namespace=:ns
       WHERE c.active AND c.vendor_id=p.vendor_id AND c.sku=p.sku) x ON true
      WHERE (p.par_source='MANUAL' OR f.legacy_id IS NOT NULL) AND p.id>:after AND (:query='' OR p.sku ILIKE :like)
      ORDER BY p.id LIMIT :limit'''),dict(ns=source_namespace,after=after,query=search,like='%'+search+'%',limit=limit+1)).mappings().all()
    page=[dict(r) for r in rows[:limit]]
    keys={('MANUAL',r['possible_variations'][0]+':'+str(r['store_id'])) for r in page if r['possible_variations'] and len(r['possible_variations'])==1 and r['store_id']}
    current=heads(connection,keys)
    from .controls import legacy_manual_observation
    for row in page:
        row['source_digest']=digest(legacy_manual_observation(row))
        key=row['possible_variations'][0]+':'+str(row['store_id']) if row['possible_variations'] and len(row['possible_variations'])==1 and row['store_id'] else None
        row['current_owner_version']=current.get(('MANUAL',key),{}).get('version',0)
    return dict(rows=page,next_cursor=page[-1]['id'] if len(rows)>limit else None)


def supplier_suggestions(connection,*,source_namespace,after='',limit=50):
    if not 1<=limit<=100:raise ValueError('Bounded suggestion page required')
    run,config=after.split('/',1) if after else ('00000000-0000-0000-0000-000000000000','0')
    from .controls import uuid
    run=uuid(run);config=int(config)
    rows=connection.execute(text('''SELECT sc.run_id::text,sc.source_config_id,sc.variation_id::text,sc.supplier_id::text,
      sc.explicit_default,sc.disposition,o.evidence,c.label,v.name AS observed_supplier_name
      FROM purchasing_v4.supplier_candidate sc JOIN purchasing_v4.import_run ir ON ir.id=sc.run_id AND ir.source_namespace=:ns
      JOIN purchasing_v4.source_observation o ON o.run_id=sc.run_id AND o.source_table='vendor_sku_configs' AND o.source_key=sc.source_config_id::text
      LEFT JOIN purchasing_v4.management_catalog c ON c.variation_id=sc.variation_id
      LEFT JOIN purchasing_v4.supplier_reference r ON r.supplier_id=sc.supplier_id AND r.source_namespace=:ns
      LEFT JOIN public.vendors v ON v.id=r.legacy_vendor_id
      WHERE (sc.run_id,sc.source_config_id)>(CAST(:run AS uuid),:config) ORDER BY sc.run_id,sc.source_config_id LIMIT :limit'''),
      dict(ns=source_namespace,run=run,config=config,limit=limit+1)).mappings().all()
    page=rows[:limit]
    return dict(rows=[dict(r) for r in page],next_cursor=page[-1]['run_id']+'/'+str(page[-1]['source_config_id']) if len(rows)>limit else None)


def freeze_legacy_manual(connection,*,source_namespace):
    """Explicit intake only: retain original intent without creating owner decisions."""
    from .controls import legacy_manual_observation
    rows=connection.execute(text("""SELECT p.*,x.ids AS possible_variations FROM public.par_levels p
      LEFT JOIN LATERAL(SELECT array_agg(DISTINCT r.variation_id::text ORDER BY r.variation_id::text) AS ids
       FROM public.vendor_sku_configs c JOIN purchasing_v4.variation_reference r ON r.square_variation_id=c.square_variation_id AND r.source_namespace=:ns
       WHERE c.active AND c.vendor_id=p.vendor_id AND c.sku=p.sku) x ON true WHERE p.par_source='MANUAL'"""),{'ns':source_namespace}).mappings().all()
    for row in rows:
        mapped=row['possible_variations'] or []
        scope=mapped or list(connection.execute(text("SELECT variation_id::text FROM purchasing_v4.management_catalog WHERE sku=:sku ORDER BY variation_id"),{'sku':row['sku']}).scalars())
        observation=legacy_manual_observation(row)
        connection.execute(text("""INSERT INTO purchasing_v4.legacy_manual_evidence(source_namespace,legacy_id,store_id,mapped_variations,scope_variations,observation,original_record,source_digest)
         VALUES (:ns,:id,:store,CAST(:mapped AS jsonb),CAST(:scope AS jsonb),CAST(:observation AS jsonb),CAST(:original AS jsonb),:sha)
         ON CONFLICT(source_namespace,legacy_id) DO NOTHING"""),dict(ns=source_namespace,id=row['id'],store=row['store_id'],mapped=json.dumps(mapped),scope=json.dumps(scope),observation=json.dumps(observation,default=str),original=json.dumps(dict(row),default=str),sha=digest(observation)))
    return len(rows)
