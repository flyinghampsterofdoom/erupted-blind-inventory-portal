"""Finite, fail-closed collection. Network work never owns a source transaction."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json,time
from sqlalchemy import select,text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from app import models
from .contracts import TZ,digest,EvidenceError

SOURCE_MODELS=(models.Store,models.Vendor,models.VendorSkuConfig,models.PurchaseOrder,
 models.PurchaseOrderLine,models.PurchaseOrderStoreAllocation,models.PurchaseOrderReceipt,
 models.PurchaseOrderReceiptLine,models.PurchaseOrderReceiptAllocation,models.OrderingProductLifecycle,
 models.ConsignmentSaleFact,models.PurchasingSaleCorrection,models.PurchasingInventoryEvent,
 models.PurchasingHistoryCoverage,models.ConsignmentSalesSyncState,models.PurchasingOwnerDecision)

@contextmanager
def source_session(engine):
    """Preserve the primary error even if rollback/close also fails."""
    db=Session(engine)
    try:
        yield db
    except BaseException as original:
        try:db.close()
        except Exception as cleanup:
            # Python3.10 has no add_note; retain safe diagnostic metadata on the original.
            original.collection_cleanup_error=type(cleanup).__name__
        raise
    else:db.close()

def readonly(db):
    postgres=db.bind.dialect.name=='postgresql'
    if postgres:
        if db.execute(text('SHOW transaction_read_only')).scalar()!='on':raise EvidenceError('Source database connection must be read-only')
        if db.execute(text('SHOW transaction_isolation')).scalar()!='repeatable read':raise EvidenceError('Source requires repeatable read isolation')
    elif db.execute(text('PRAGMA query_only')).scalar()!=1:raise EvidenceError('Source database connection must be read-only')

def fingerprint(db,first,last):
    """Hash all consumed source tables/columns, with fixed history-date predicates.

    The certificate is compared across short transactions; no open transaction is
    carried over a network wait. All mutable inputs must be unchanged, not merely
    their maximum timestamp. PostgreSQL hashes never leave the database as rows.
    """
    readonly(db);result={}
    for model in SOURCE_MODELS:
        table=model.__table__;where='';params={}
        if model in (models.ConsignmentSaleFact,models.PurchasingInventoryEvent):
            where=' WHERE business_date BETWEEN :first AND :last';params={'first':first,'last':last if model==models.PurchasingInventoryEvent else last-timedelta(days=1)}
        if db.bind.dialect.name=='postgresql':
            name=db.bind.dialect.identifier_preparer.quote(table.name)
            order=','.join(db.bind.dialect.identifier_preparer.quote(c.name) for c in table.primary_key)
            q=f"SELECT count(*),encode(sha256(convert_to(coalesce(string_agg(row_to_json(t)::text,chr(10) ORDER BY {order}),''),'UTF8')),'hex') FROM {name} t{where}"
            count,sha=db.execute(text(q),params).one()
        else:
            statement=select(table).order_by(*table.primary_key)
            if where:statement=statement.where(table.c.business_date.between(params['first'],params['last']))
            rows=db.execute(statement).mappings().all();count=len(rows)
            sha=digest([{k:(v.isoformat() if hasattr(v,'isoformat') else str(v) if isinstance(v,Decimal) else v) for k,v in r.items()} for r in rows])
        result[table.name]={'rows':count,'sha256':sha}
    return result

class FrozenGateway:
    def __init__(self,catalog,inventory,events):self.catalog=catalog;self.inventory=inventory;self.events=events
    def fetch_active_catalog(self):return self.catalog
    def fetch_current_inventory_counts(self,**kw):return self.inventory
    def fetch_verified_inventory_events(self,*args):return self.events

def collect(engine,gateway,*,max_variations=1500,budget_seconds=900,observer=None):
    """One sequential observation interval, one common frozen model input set.

    The database must be unchanged before/after Square retrieval. Square reads are
    explicitly sequential; record-level calculated/known times remain in evidence.
    An absent historical absolute count is still UNKNOWN, never a fabricated anchor.
    """
    from .capture import _assemble
    if not isinstance(engine,Engine):raise EvidenceError('Collector requires an owned engine, not an open connection')
    began=datetime.now(timezone.utc);clock=time.monotonic();today=began.astimezone(TZ).date();first=today-timedelta(days=84)
    with source_session(engine) as db:
        before=fingerprint(db,first,today)
        locations=[s.square_location_id for s in db.scalars(select(models.Store).where(models.Store.active.is_(True))) if ''.join(c for c in s.name.lower() if c.isalnum()) in ('andresen','hwy99','highway99','sr503','longview')]
    if len(locations)!=4 or len(set(locations))!=4:raise EvidenceError('Four distinct store identities required')
    source_read=datetime.now(timezone.utc)
    if observer:observer('before_network',{'source':before})
    catalog=gateway.fetch_active_catalog()
    if not catalog or len(catalog)>max_variations:raise EvidenceError('Invalid or unbounded active catalog scope')
    catalog_observed=datetime.now(timezone.utc);vids=sorted(catalog)
    inventory=gateway.fetch_current_inventory_counts(location_ids=locations,variation_ids=vids)
    inventory_observed=datetime.now(timezone.utc)
    events=gateway.fetch_verified_inventory_events(locations,vids,datetime.combine(first,datetime.min.time(),TZ),inventory_observed) if hasattr(gateway,'fetch_verified_inventory_events') else None
    square_finished=datetime.now(timezone.utc)
    if time.monotonic()-clock>budget_seconds:raise TimeoutError('Source collection elapsed budget exhausted')
    if observer:observer('after_network',{'catalog_variations':len(catalog),'inventory_pairs':len(inventory.counts),'history_records':len(events or [])})
    with source_session(engine) as db:
        after=fingerprint(db,first,today)
        changed=[name for name in before if before[name]!=after[name]]
        if changed:raise EvidenceError('Source changed during collection: '+','.join(changed))
        snapshot=_assemble(db,FrozenGateway(catalog,inventory,events),max_variations=max_variations,budget_seconds=180,began_at=began,observed_at=square_finished,inventory_observed_at=inventory_observed,catalog_observed_at=catalog_observed,release_source=True)
    completed=datetime.now(timezone.utc)
    snapshot['collection']={'started_at':began.isoformat(),'source_initial_read_completed_at':source_read.isoformat(),'catalog_retrieved_at':catalog_observed.isoformat(),'inventory_retrieved_at':inventory_observed.isoformat(),'history_query_through_at':inventory_observed.isoformat(),'square_retrieval_completed_at':square_finished.isoformat(),'completed_at':completed.isoformat(),'source_before':before,'source_after':after,'source_changes_detected':[],'source_transaction_during_network':False,'consistency':'Identical database inputs across two repeatable-read snapshots; sequential Square observations with retained record timestamps, not a physically simultaneous snapshot','seconds':time.monotonic()-clock,'square_pages':getattr(gateway,'page_evidence',[]),'catalog_versions':getattr(gateway,'catalog_versions',{}),'history_batches':getattr(gateway,'history_batches',[])}
    if observer:observer('complete',snapshot['collection'])
    return snapshot
