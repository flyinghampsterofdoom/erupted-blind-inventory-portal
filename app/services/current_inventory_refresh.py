"""Explicit Square reads update the inventory mirror, never worksheet decisions.

Use the daily collector's scope, paging, two-read qualification and singleton.
No sales/movement coverage or historical checkpoints are changed here.
"""
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models import OrderingInventoryRefreshRun
from app.services.daily_square_collector import Collector,LOCK,load_scope,instant,number
from app.services.v2_ordering_inventory_repository import InventoryObservation,persist_inventory_refresh
from app.services.purchasing_evidence_service import record_run


def refresh(engine,post,*,actor_id,command_id,target=None,clock=None):
    command_id=str(UUID(command_id))
    collector=Collector(engine,post,target=target,**({'clock':clock} if clock else {}))
    with engine.connect() as lock:
        collector.guard(lock)
        if not lock.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':LOCK}):raise RuntimeError('Source collection is already running; retry after it completes')
        lock.commit()
        try:
            with engine.connect() as c:
                existing=c.execute(text('SELECT * FROM public.ordering_inventory_refresh_runs WHERE correlation_id=:id'),{'id':command_id}).mappings().one_or_none()
                if existing:
                    if existing['refreshed_by_principal_id']!=actor_id:raise PermissionError('Inventory refresh identity belongs to another actor')
                    return result(existing,replayed=True)
                scope=load_scope(c,guard=collector.guard)
            started=instant(collector.clock());observations=[];error=None
            try:
                before=collector.counts(scope);after=collector.counts(scope)
                def index(rows):
                    values={};locations={loc for _,loc in scope.stores}
                    for row in rows:
                        key=(row.get('catalog_object_id'),row.get('location_id'))
                        if key in values or key[0] not in scope.variations or key[1] not in locations or row.get('state')!='IN_STOCK':raise ValueError('Conflicting or out-of-scope inventory count')
                        quantity=number(row.get('quantity'));calculated=instant(row['calculated_at']) if row.get('calculated_at') else None
                        values[key]=(quantity,calculated)
                    return values
                first=index(before['rows']);last=index(after['rows'])
                for vid in scope.variations:
                    for sid,loc in scope.stores:
                        value=first.get((vid,loc))
                        if value is not None and value==last.get((vid,loc)) and value[1] is not None and value[1]<=started:
                            observations.append(InventoryObservation(vid,sid,loc,value[0],value[1]))
            except Exception as exc:
                # Keep all previous values on a failed source read. No partial
                # page can certify a successful refresh; never expose raw errors.
                observations=[];error=type(exc).__name__
            finished=instant(collector.clock())
            if finished<started:raise ValueError('Inventory source clock moved backwards')
            expected=len(scope.variations)*len(scope.stores)
            with engine.begin() as c:
                collector.guard(c)
                c.execute(text('SELECT pg_advisory_xact_lock(730202607250009)'))
                newer=set(c.execute(text('SELECT square_variation_id,store_id FROM public.ordering_current_inventory WHERE refreshed_at>:at'),{'at':started}).tuples())
                observations=[o for o in observations if (o.square_variation_id,o.store_id) not in newer]
                count=len(observations);status='COMPLETE' if count==expected else 'PARTIAL' if count else 'FAILED'
                run=OrderingInventoryRefreshRun(correlation_id=command_id,result=status,expected_variation_count=len(scope.variations),active_store_count=len(scope.stores),expected_pair_count=expected,covered_pair_count=count,missing_pair_count=expected-count,square_request_count=len(collector.pages),refreshed_by_principal_id=actor_id,started_at=started,completed_at=finished,error_code='PARTIAL_COVERAGE' if status=='PARTIAL' else 'INVENTORY_READ_FAILED' if status=='FAILED' else None,error_summary=('Square inventory read failed ('+error+'); prior values retained.') if error else 'No stable explicit counts; prior values retained.' if status=='FAILED' else None)
                db=Session(bind=c)
                try:
                    persist_inventory_refresh(db,run=run,observations=tuple(observations),refreshed_at=finished)
                    if count:
                        known={(o.square_variation_id,o.store_id) for o in observations}
                        missing=[dict(vid=vid,sid=sid,at=started) for vid in scope.variations for sid,_ in scope.stores if (vid,sid) not in known]
                        if missing:c.execute(text("UPDATE public.ordering_current_inventory SET freshness_state='STALE' WHERE square_variation_id=:vid AND store_id=:sid AND refreshed_at<=:at"),missing)
                    record_run(db,source='INVENTORY',status=status,started_at=started,completed_at=finished,scope=dict(variation_ids=list(scope.variations),store_ids=[s for s,_ in scope.stores]),evidence=dict(observed_pairs=[[o.square_variation_id,o.store_id] for o in observations],error_code=run.error_code,manual_inventory_only=True,actor_id=actor_id,command_id=command_id,pages=collector.pages))
                    db.flush();outcome=result(dict(id=run.id,correlation_id=command_id,result=status,covered_pair_count=count,missing_pair_count=expected-count,expected_pair_count=expected,completed_at=finished,error_code=run.error_code),replayed=False)
                finally:db.close()
            return outcome
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LOCK});lock.commit()


def result(row,*,replayed):
    return dict(status=row['result'],command_id=row['correlation_id'],refresh_run_id=row['id'],known_pairs=row['covered_pair_count'],unknown_pairs=row['missing_pair_count'],expected_pairs=row['expected_pair_count'],observed_at=instant(row['completed_at']).isoformat(),error_code=row['error_code'],replayed=replayed)
