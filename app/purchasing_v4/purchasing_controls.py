"""Bounded owner view over the existing global PRODUCT purchasing authority."""
import re
from sqlalchemy import text
from . import controls


def list_products(connection,*,search='',supplier='',status='all',sort='product',direction='asc',offset=0,limit=50):
    order={'product':'lower(product)','variation':'lower(variation)','sku':'lower(sku)','supplier':'lower(supplier_name)','status':'no_reorder'}
    if sort not in order or direction not in ('asc','desc') or status not in ('all','active','no_reorder'):
        raise controls.InvalidPolicy('Choose a supported product purchasing filter or sort')
    if type(offset) is not int or not 0<=offset<=1000000 or type(limit) is not int or not 1<=limit<=100 or len(search)>200:
        raise controls.InvalidPolicy('Bounded product search/page required')
    if supplier:controls.uuid(supplier)
    tokens=re.findall(r'[a-z0-9]+',search.lower())
    params=dict(supplier=supplier,status=status,offset=offset,limit=limit)
    filters=[]
    for n,token in enumerate(tokens):filters.append('haystack LIKE :term'+str(n));params['term'+str(n)]='%'+token+'%'
    predicate=' AND '.join(filters) or 'true'
    sql='''WITH barcodes AS (
      SELECT square_variation_id,string_agg(DISTINCT gtin,' ') AS codes FROM public.vendor_sku_configs
      WHERE gtin IS NOT NULL GROUP BY square_variation_id
    ), items AS (
      SELECT c.variation_id::text AS variation_id,c.square_variation_id,
       coalesce(i.item_name,c.label) AS product,coalesce(i.variation_name,'') AS variation,coalesce(c.sku,'') AS sku,
       coalesce(b.codes,'') AS barcode,coalesce(a.payload->>'supplier_id','') AS supplier_id,
       coalesce(s.payload->>'name','Unassigned supplier') AS supplier_name,
       CASE WHEN p.payload IS NOT NULL AND NOT coalesce((p.payload->>'_removed')::boolean,false)
        THEN (p.payload->>'no_reorder')::boolean
        ELSE coalesce(l.status::text IN ('NO_FUTURE_REORDER','ARCHIVED'),false) END AS no_reorder,
       coalesce(p.version,0) AS version,CASE WHEN coalesce((p.payload->>'_removed')::boolean,false) THEN NULL ELSE p.payload->'coverage' END AS coverage,
       lower(c.label||' '||coalesce(c.sku,'')||' '||coalesce(b.codes,'')||' '||coalesce(i.catalog_attributes->>'gtin','')) AS haystack
      FROM purchasing_v4.management_catalog c
      LEFT JOIN public.ordering_catalog_identity i USING(square_variation_id)
      LEFT JOIN public.ordering_product_lifecycle l USING(square_variation_id)
      LEFT JOIN barcodes b USING(square_variation_id)
      LEFT JOIN purchasing_v4.control_head p ON p.kind='PRODUCT' AND p.record_key=c.variation_id::text
      LEFT JOIN purchasing_v4.control_head a ON a.kind='ASSIGNMENT' AND a.record_key=c.variation_id::text AND NOT coalesce((a.payload->>'_removed')::boolean,false)
      LEFT JOIN purchasing_v4.control_head s ON s.kind='SUPPLIER' AND s.record_key=(a.payload->>'supplier_id') AND NOT coalesce((s.payload->>'_removed')::boolean,false)
    ), filtered AS (
      SELECT * FROM items WHERE (:supplier='' OR supplier_id=:supplier)
       AND (:status='all' OR no_reorder=(:status='no_reorder')) AND '''+predicate+'''
    ) SELECT *,count(*) OVER() AS total FROM filtered ORDER BY '''+order[sort]+' '+direction+',lower(variation),variation_id LIMIT :limit OFFSET :offset'
    rows=[dict(r) for r in connection.execute(text(sql),params).mappings()]
    total=rows[0]['total'] if rows else connection.scalar(text('SELECT count(*) FROM purchasing_v4.management_catalog')) if offset==0 and not tokens and not supplier and status=='all' else 0
    for row in rows:row.pop('haystack',None);row.pop('total',None)
    return dict(rows=rows,total=total,offset=offset,limit=limit,next_offset=offset+limit if offset+len(rows)<total else None)
