"""Explicit read-only reconciliation; findings never repair or adopt historical facts."""
import hashlib
from sqlalchemy import text
from .isolation import require_read_only
from .reconciliation import public_fingerprints

DOMAINS={
 'product_identities':['purchasing_v4.product','public.ordering_catalog_identity'],
 'variation_identities':['purchasing_v4.variation','purchasing_v4.variation_reference','purchasing_v4.identity_exception'],
 'supplier_assignments':['public.vendors','public.vendor_sku_configs','purchasing_v4.supplier','purchasing_v4.supplier_candidate','purchasing_v4.authority_supplier','purchasing_v4.control_head'],
 'manual_policies':['public.par_levels','purchasing_v4.legacy_manual_evidence','purchasing_v4.control_revision'],
 'pack_configurations':['public.vendor_sku_configs','purchasing_v4.supplier_candidate'],
 'purchase_orders':['public.purchase_orders','purchasing_v4.po_ownership','purchasing_v4.authorized_vendor'],
 'supplier_obligations':['public.purchase_order_lines','purchasing_v4.request_reservation','purchasing_v4.quotation_intent_resolution'],
 'store_allocations':['public.purchase_order_store_allocations','purchasing_v4.receiving_event'],
 'historical_receipts':['public.purchase_order_receipts','public.purchase_order_receipt_lines','public.purchase_order_receipt_allocations','purchasing_v4.receiving_order','purchasing_v4.receiving_event'],
 'financial_commitments':['public.funding_ledger_entries','public.funding_report_lines','purchasing_v4.financial_line_bridge','purchasing_v4.quotation_financial'],
 'payments':['public.order_payments','public.order_payment_events','public.order_manual_payment_entries','public.purchasing_payment_batches','public.purchasing_payment_batch_entries','public.funding_payments','public.funding_payment_allocations'],
 'inventory_postings':['public.square_sync_events','public.purchase_order_receipt_allocations','public.purchasing_inventory_events','purchasing_v4.inventory_command','purchasing_v4.inventory_operation','purchasing_v4.inventory_verification'],
 'vendor_documents':['public.purchase_order_pdf_templates','purchasing_v4.vendor_artifact','purchasing_v4.quotation_revision','purchasing_v4.quotation_upload','purchasing_v4.mail_message','purchasing_v4.mail_attachment'],
 'audit':['public.audit_log','purchasing_v4.source_observation','purchasing_v4.import_run','purchasing_v4.control_command','purchasing_v4.control_revision','purchasing_v4.receiving_event','purchasing_v4.mail_attempt']}


def private_fingerprints(connection):
    require_read_only(connection);result={};quote=connection.dialect.identifier_preparer.quote
    tables=list(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' ORDER BY tablename")).scalars())
    for table in tables:
        checksum=hashlib.sha256();count=0
        for row in connection.execute(text('SELECT to_jsonb(t)::text FROM purchasing_v4.'+quote(table)+' t ORDER BY to_jsonb(t)::text COLLATE "C"')):
            value=row[0].encode();checksum.update(str(len(value)).encode()+b':'+value);count+=1
        result[table]=dict(rows=count,sha256=checksum.hexdigest())
    return result


def report(connection):
    require_read_only(connection);public=public_fingerprints(connection);private=private_fingerprints(connection);all_tables={**{'public.'+k:v for k,v in public.items()},**{'purchasing_v4.'+k:v for k,v in private.items()}}
    domains={name:{table:all_tables[table] for table in tables if table in all_tables} for name,tables in DOMAINS.items()}
    quote=connection.dialect.identifier_preparer.quote
    fks=connection.execute(text('''SELECT n.nspname AS schema,t.relname AS table_name,con.conname,con.convalidated,
      rn.nspname AS parent_schema,rt.relname AS parent_table,
      ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum,pos) JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.attnum ORDER BY k.pos) AS child_columns,
      ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum,pos) JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.attnum ORDER BY k.pos) AS parent_columns
      FROM pg_constraint con JOIN pg_class t ON t.oid=con.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
      JOIN pg_class rt ON rt.oid=con.confrelid JOIN pg_namespace rn ON rn.oid=rt.relnamespace
      WHERE con.contype='f' AND n.nspname IN ('public','purchasing_v4') ORDER BY 1,2,3''')).mappings().all()
    foreign_keys=[]
    for fk in fks:
        table=quote(fk['schema'])+'.'+quote(fk['table_name']);parent=quote(fk['parent_schema'])+'.'+quote(fk['parent_table'])
        present=' AND '.join('child.'+quote(k)+' IS NOT NULL' for k in fk['child_columns'])
        join=' AND '.join('parent.'+quote(p)+'=child.'+quote(k) for k,p in zip(fk['child_columns'],fk['parent_columns']))
        count=connection.execute(text('SELECT count(*) FROM '+table+' child WHERE '+present+' AND NOT EXISTS(SELECT 1 FROM '+parent+' parent WHERE '+join+')')).scalar_one()
        foreign_keys.append(dict(schema=fk['schema'],table=fk['table_name'],constraint=fk['conname'],validated=fk['convalidated'],orphan_rows=count))
    missing=list(connection.execute(text('''SELECT p.id FROM public.purchase_orders p LEFT JOIN purchasing_v4.po_ownership o ON o.purchase_order_id=p.id WHERE o.purchase_order_id IS NULL ORDER BY p.id''')).scalars())
    ownership=[dict(x) for x in connection.execute(text('SELECT origin,writer,count(*) AS orders FROM purchasing_v4.po_ownership GROUP BY origin,writer ORDER BY origin,writer')).mappings()]
    balance_rows=connection.execute(text('''SELECT p.id AS purchase_order_id,p.status::text,l.id AS line_id,l.variation_id,l.ordered_qty,l.received_qty_total,
      count(a.id) AS allocation_records,sum(a.allocated_qty) AS allocated_units,sum(a.store_received_qty) AS allocated_received_units,
      o.writer FROM public.purchase_orders p JOIN public.purchase_order_lines l ON l.purchase_order_id=p.id
      LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
      LEFT JOIN purchasing_v4.po_ownership o ON o.purchase_order_id=p.id WHERE NOT l.removed
      GROUP BY p.id,p.status,l.id,o.writer ORDER BY p.id,l.id''')).mappings().all()
    balances=[]
    for row in balance_rows:
        reasons=[]
        if row['allocation_records']==0:reasons.append('MISSING_HISTORICAL_STORE_ALLOCATION')
        elif row['allocated_units']!=row['ordered_qty'] or row['allocated_received_units']!=row['received_qty_total']:reasons.append('CONFLICTING_HISTORICAL_ALLOCATION')
        if row['received_qty_total']>row['ordered_qty']:reasons.append('RECEIVED_ABOVE_ORIGINAL_ORDER')
        if reasons:balances.append(dict(row,reasons=reasons,affects_open_supplier_obligation=row['status'] in ('IN_TRANSIT','RECEIVED_SPLIT_PENDING'),review_path='/management/ordering-tool/orders/'+str(row['purchase_order_id']) if row['writer']=='LEGACY' else '/receiving',automatic_repair=False))
    return dict(status='RECONCILED_WITH_RETAINED_EXCEPTIONS' if balances else 'RECONCILED',public=public,private=private,domains=domains,
      foreign_keys=foreign_keys,orphan_rows=sum(x['orphan_rows'] for x in foreign_keys),ownership=ownership,missing_po_ownership=missing,
      balance_exceptions=balances,exception_review_path='/purchasing-v4/policies',source_mutations=0,historical_receipts_replayed=0,purchasing_commitments_created=0)
