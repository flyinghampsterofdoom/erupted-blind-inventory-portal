"""Explicit collector-only migration; never installs Purchasing V4 application schema."""
from pathlib import Path
from sqlalchemy import text
from psycopg import sql
from app.services.evidence_collector_target import ROLE

READ=('ordering_catalog_identity','stores','consignment_sale_facts','consignment_return_facts',
      'purchasing_inventory_events','consignment_sales_sync_state')
SOURCE=('ordering_current_inventory','ordering_inventory_refresh_runs','purchasing_evidence_runs',
        'purchasing_sale_corrections','purchasing_order_source_state','purchasing_history_coverage')


def install(c,*,database_name,hostname,owner,password):
    if c.engine.url.database!=database_name or c.engine.url.host!=hostname:raise PermissionError('Migration target mismatch')
    db,user,recovery=c.execute(text('SELECT current_database(),current_user,pg_is_in_recovery()')).one()
    if db!=database_name or user!=owner or recovery:raise PermissionError('Migration server/owner mismatch')
    if c.scalar(text('SELECT version_num FROM public.alembic_version'))!='20261027_0053':raise RuntimeError('Collector migration requires reviewed public schema 0053')
    exists=c.scalar(text("SELECT to_regclass('purchasing_evidence.schema_version')"))
    if exists:
        if c.scalar(text('SELECT version FROM purchasing_evidence.schema_version WHERE id=1'))!='daily_1':raise RuntimeError('Unreviewed source schema')
        return {'status':'ALREADY_INSTALLED','version':'daily_1'}
    if c.scalar(text('SELECT 1 FROM pg_roles WHERE rolname=:role'),{'role':ROLE}):raise RuntimeError('Unknown preexisting collector role; do not reuse or reset it')
    c.execute(text("SET LOCAL lock_timeout='1000ms'"))
    c.exec_driver_sql((Path(__file__).resolve().parents[2]/'scripts/evidence/daily_1.sql').read_text())
    driver=c.connection.driver_connection
    driver.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(ROLE),sql.Literal(password)))
    driver.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database_name),sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT USAGE ON SCHEMA public,purchasing_evidence TO {}').format(sql.Identifier(ROLE)))
    for table in READ+SOURCE:
        driver.execute(sql.SQL('GRANT SELECT ON public.{} TO {}').format(sql.Identifier(table),sql.Identifier(ROLE)))
    for table in SOURCE:
        grants='INSERT, UPDATE' if table in ('ordering_current_inventory','purchasing_sale_corrections','purchasing_order_source_state','purchasing_history_coverage') else 'INSERT'
        driver.execute(sql.SQL('GRANT '+grants+' ON public.{} TO {}').format(sql.Identifier(table),sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT INSERT ON public.audit_log TO {}').format(sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT SELECT(id,created_at) ON public.audit_log TO {}').format(sql.Identifier(ROLE)))
    for table in ('ordering_inventory_refresh_runs','purchasing_evidence_runs','audit_log'):
        seq=c.scalar(text('SELECT pg_get_serial_sequence(:table,\'id\')'),{'table':'public.'+table})
        driver.execute(sql.SQL('GRANT USAGE, SELECT ON SEQUENCE {} TO {}').format(sql.Identifier(*seq.split('.')),sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT SELECT ON purchasing_evidence.schema_version,purchasing_evidence.shared_inventory_events TO {}').format(sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT SELECT,INSERT ON purchasing_evidence.collection_attempt TO {}').format(sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT INSERT ON purchasing_evidence.collection_page,purchasing_evidence.inventory_checkpoint TO {}').format(sql.Identifier(ROLE)))
    driver.execute(sql.SQL('GRANT SELECT,INSERT,DELETE ON purchasing_evidence.movement_leg TO {}').format(sql.Identifier(ROLE)))
    return {'status':'INSTALLED','version':'daily_1','worker':ROLE,'broader_v4_schema_installed':False}
