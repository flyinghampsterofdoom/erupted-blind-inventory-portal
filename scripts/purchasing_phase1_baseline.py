"""Bounded, read-only verification. Never imports app startup or calls Square.

Usage: python scripts/purchasing_phase1_baseline.py --url-env AUTHORIZED_READ_URL
Connection strings are read from the named environment variable and never printed.
Fingerprints cover at most 200 rows per table; they are not a full database backup.
"""
import argparse
import hashlib
import json
import os
from sqlalchemy import create_engine, text

TABLES = ('stores', 'vendors', 'vendor_sku_configs', 'purchase_orders', 'purchase_order_lines',
    'purchase_order_store_allocations', 'purchase_order_receipts', 'purchase_order_receipt_lines',
    'square_sync_events', 'order_payments', 'funding_accounts', 'funding_ledger_entries',
    'funding_payments', 'funding_payment_allocations', 'funding_reports', 'consignment_reports',
    'consignment_ledger_entries', 'consignment_replenishments', 'ordering_inventory_refresh_runs',
    'ordering_current_inventory', 'consignment_sales_sync_state', 'purchasing_evidence_runs',
    'purchasing_product_authority', 'funding_balance_checkpoints', 'funding_actual_activity')


def baseline(url):
    engine=create_engine(url, connect_args={'connect_timeout':5})
    report={'read_only':True,'sample_limit_per_table':200,'tables':{}}
    try:
        with engine.connect() as con, con.begin():
            con.execute(text('SET TRANSACTION READ ONLY'))
            con.execute(text("SET LOCAL statement_timeout = '5s'"))
            con.execute(text("SET LOCAL lock_timeout = '1s'"))
            report['database']=con.execute(text('SELECT current_database()')).scalar_one()
            report['revision']=con.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
            for name in TABLES:
                with con.begin_nested():
                    exists=con.execute(text('SELECT to_regclass(:name)'),{'name':'public.'+name}).scalar()
                    if not exists:
                        report['tables'][name]={'exists':False};continue
                    # Server hashes source rows; no financial detail is returned to the caller.
                    order='1,2' if name=='ordering_current_inventory' else '1'
                    rows=con.execute(text(f'SELECT md5(row_to_json(t)::text) FROM (SELECT * FROM {name} ORDER BY {order} LIMIT 200) t')).scalars().all()
                    report['tables'][name]={'exists':True,'sample_rows':len(rows),
                        'sample_sha256':hashlib.sha256(''.join(rows).encode()).hexdigest()}
            report['store_location_counts']=[list(row) for row in con.execute(text(
                'SELECT square_location_id,count(*) FROM stores WHERE active GROUP BY square_location_id'))]
            report['po_status_counts']=[list(row) for row in con.execute(text(
                'SELECT status,count(*) FROM purchase_orders GROUP BY status'))]
    finally:
        engine.dispose()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url-env',required=True)
    args=parser.parse_args()
    url=os.getenv(args.url_env)
    if not url:
        raise SystemExit('Named connection environment variable is unavailable; no connection attempted.')
    try:
        print(json.dumps(baseline(url),indent=2,default=str))
    except Exception as exc:
        # Never echo the connection string or detailed driver error containing connection information.
        raise SystemExit(f'Read-only baseline incomplete ({type(exc).__name__}); no mutation attempted.')
