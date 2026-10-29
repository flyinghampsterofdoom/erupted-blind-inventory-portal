"""Collector-only production binding, default off; no V4 workspace or PO capability."""
from datetime import datetime,time,timedelta,timezone
from uuid import uuid4
from sqlalchemy import create_engine,text
from app.services.daily_square_collector import run_once,ZONE
from app.services.evidence_collector_target import ProductionTarget


def configured():
    from app.config import settings
    if not settings.daily_evidence_collector_enabled:raise PermissionError('Daily source collection is disabled')
    target=ProductionTarget(settings.daily_evidence_database_url or '',settings.daily_evidence_database_name,settings.daily_evidence_database_host)
    engine=create_engine(target.url,pool_pre_ping=True,connect_args={'connect_timeout':15,'options':'-c statement_timeout=60000 -c lock_timeout=10000 -c timezone=UTC'})
    try:
        with engine.connect() as c:target.verify_privileges(c)
    except Exception:
        engine.dispose();raise
    return engine,target


class Refresh:
    def __init__(self,engine,target):self.engine=engine;self.target=target
    def __call__(self,*,reason):
        if reason=='MANUAL':
            # The 126-day bootstrap belongs to the dedicated worker, not a web
            # request. Later on-demand refreshes share its incremental pipeline.
            with self.engine.connect() as c:
                if not c.scalar(text("SELECT EXISTS(SELECT 1 FROM purchasing_evidence.collection_attempt WHERE status IN ('COMPLETE','PARTIAL'))")):
                    return {'status':'INITIAL_RECOVERY_REQUIRED'}
        return run_once(self.engine,source_post,reason=reason,target=self.target)


def bind(app):
    engine,target=configured()
    app.state.shared_source_collector=Refresh(engine,target)
    app.state.external_source_refresh_enabled=False
    app.state.daily_evidence_engine=engine


def status(c,*,now):
    """Read-only visibility; never marks financial import/attribution complete."""
    now=now.astimezone(timezone.utc)
    latest=c.execute(text('SELECT * FROM purchasing_evidence.collection_attempt ORDER BY completed_at DESC,started_at DESC,id DESC LIMIT 1')).mappings().first()
    success=c.execute(text("SELECT * FROM purchasing_evidence.collection_attempt WHERE status IN ('COMPLETE','PARTIAL') ORDER BY completed_at DESC,started_at DESC,id DESC LIMIT 1")).mappings().first()
    cutoff=datetime.combine(now.astimezone(ZONE).date(),time(),ZONE)
    result=dict(state='never',label='Never collected',last_successful_at=None,last_error=None,age_hours=None,coverage_start_at=None,coverage_through_at=None,
        required_start_at=cutoff-timedelta(days=126),required_through_at=cutoff,coverage_complete=False,collector='shared-daily',schedule_utc='12:00',employee_login_required=False)
    if success:
        m=success['manifest'];age=now-success['completed_at'];through=datetime.fromisoformat(m['sales_end']);complete=through>=cutoff and age<=timedelta(hours=24) and age>=timedelta()
        locations=[loc for sid,loc in m['scope']['stores']]
        coverage=c.execute(text("SELECT source,location_id,start_at,through_at,verified_at FROM public.purchasing_history_coverage WHERE source IN ('SALES','MOVEMENTS') AND location_id=ANY(:locations)"),{'locations':locations}).mappings().all()
        complete=complete and len(coverage)==8 and all(r['start_at']<=cutoff-timedelta(days=126) and r['through_at']>=cutoff and timedelta()<=now-r['verified_at']<=timedelta(hours=24) for r in coverage)
        full_start=min((r['start_at'] for r in coverage),default=None)
        result.update(last_successful_at=success['completed_at'].isoformat(),age_hours=int(max(0,age.total_seconds())//3600),coverage_start_at=full_start.isoformat() if full_start else None,coverage_through_at=m['sales_end'],coverage_complete=complete,
            qualified_inventory_pairs=m['qualified_counts'],expected_inventory_pairs=m['expected_counts'],unknown_inventory_pairs=m['expected_counts']-m['qualified_counts'],attempt_id=str(success['id']),
            state='stale' if age>timedelta(hours=24) else 'incomplete' if not complete or success['status']=='PARTIAL' else 'current',
            label='Stale evidence' if age>timedelta(hours=24) else 'Incomplete coverage' if not complete else 'Partial inventory coverage' if success['status']=='PARTIAL' else 'Current')
    if latest and latest['status']=='FAILED':result.update(state='failed',label='Collection failed',coverage_complete=False,last_error=latest['error_code'],latest_attempt_id=str(latest['id']))
    result['required_start_at']=result['required_start_at'].isoformat();result['required_through_at']=result['required_through_at'].isoformat()
    return result


def source_post(path,payload):
    from app.services.purchasing_history_service import history_post
    return history_post(path,payload,read_api_version="2026-09-16")
