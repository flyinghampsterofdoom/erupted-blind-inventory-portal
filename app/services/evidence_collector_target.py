"""Explicit source-only database capability. Never weakens the V4 isolation guard."""
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROLE='erupted_daily_evidence_worker'
PROTECTED=('purchase_orders','purchase_order_lines','purchase_order_store_allocations','par_levels',
           'vendor_sku_configs','funding_ledger_entries','consignment_sale_facts','consignment_return_facts',
           'purchasing_sessions','purchasing_session_lines','purchasing_draft_batches')


def require_isolated_url(value):
    url=make_url(value) if isinstance(value,str) else value
    if (url.get_backend_name()!='postgresql' or url.host!='127.0.0.1' or not (url.database or '').startswith(('erupted_v4_phase1_','erupted_purchasing_p1_'))
        or {'host','hostaddr','port','dbname','service','servicefile'}.intersection(url.query)):
        raise PermissionError('Explicit disposable loopback source database required')
    return url


def require_isolated(c):
    url=require_isolated_url(c.engine.url)
    db,host,recovery=c.execute(text('SELECT current_database(),inet_server_addr()::text,pg_is_in_recovery()')).one()
    if db!=url.database or host not in ('127.0.0.1','127.0.0.1/32') or recovery:raise PermissionError('Isolated source server mismatch')


@dataclass(frozen=True)
class ProductionTarget:
    url:object
    database_name:str
    hostname:str
    namespace:str='purchasing_evidence'

    def __post_init__(self):
        url=make_url(self.url) if isinstance(self.url,str) else self.url
        object.__setattr__(self,'url',url)
        if (url.get_backend_name()!='postgresql' or url.username!=ROLE or url.database!=self.database_name
            or url.host!=self.hostname or not self.hostname or self.namespace!='purchasing_evidence'
            or {'host','hostaddr','port','dbname','service','servicefile'}.intersection(url.query)):
            raise PermissionError('Production collector requires its exact reviewed database, hostname and restricted role')

    def guard(self,c):
        if c.engine.url!=self.url:raise PermissionError('Unexpected collector engine')
        database,user,superuser,recovery=c.execute(text('SELECT current_database(),current_user,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user),pg_is_in_recovery()')).one()
        if database!=self.database_name or user!=ROLE or superuser or recovery:raise PermissionError('Collector database authority is not restricted to its reviewed target')
        if c.scalar(text("SELECT version FROM purchasing_evidence.schema_version WHERE id=1"))!='daily_1':raise PermissionError('Collector schema is not qualified')

    def verify_privileges(self,c):
        self.guard(c)
        for table in PROTECTED:
            if c.scalar(text('SELECT to_regclass(:name)'),{'name':'public.'+table}) is None:continue
            if c.scalar(text("SELECT has_table_privilege(current_user,:name,'INSERT,UPDATE,DELETE,TRUNCATE')"),{'name':'public.'+table}):
                raise PermissionError('Collector has prohibited business-write authority: '+table)
