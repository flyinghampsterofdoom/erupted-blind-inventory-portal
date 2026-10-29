"""Explicit rollback preparation after the collector is disabled. Never deletes source facts."""
from datetime import datetime,timezone
import json
from uuid import uuid4
from sqlalchemy import text


def restore_legacy_certificates(c,*,database_name,hostname,owner):
    if c.engine.url.database!=database_name or c.engine.url.host!=hostname:raise PermissionError('Rollback target mismatch')
    if c.execute(text('SELECT current_database(),current_user')).one()!=(database_name,owner):raise PermissionError('Rollback server/owner mismatch')
    c.execute(text('SELECT pg_advisory_xact_lock(730202610280018)'))
    first=c.execute(text("SELECT id,manifest FROM purchasing_evidence.collection_attempt WHERE status IN ('COMPLETE','PARTIAL') ORDER BY completed_at,started_at,id LIMIT 1")).mappings().first()
    if first is None:return {'status':'NO_COLLECTION_TO_RECONCILE'}
    baseline=first['manifest'].get('legacy_coverage_before')
    if not isinstance(baseline,list):raise RuntimeError('Original legacy coverage checkpoint missing; halt calculation and recover forward')
    before=[{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in r.items()} for r in c.execute(text('SELECT * FROM public.purchasing_history_coverage ORDER BY source,location_id FOR UPDATE')).mappings()]
    for row in baseline:
        if row['source'] not in ('SALES','MOVEMENTS'):raise RuntimeError('Unknown coverage source')
    # These are replaceable certificates; all original and new source facts/pages remain retained.
    c.execute(text('DELETE FROM public.purchasing_history_coverage'))
    if baseline:c.execute(text('''INSERT INTO public.purchasing_history_coverage(source,location_id,start_at,through_at,verified_at)
      VALUES(:source,:location_id,:start_at,:through_at,:verified_at)'''),baseline)
    now=datetime.now(timezone.utc);command='rollback:'+str(uuid4())
    c.execute(text("""INSERT INTO purchasing_evidence.collection_attempt
      (id,command_key,reason,started_at,completed_at,status,manifest,error_code)
      VALUES(CAST(:id AS uuid),:key,'MANUAL',:now,:now,'FAILED',CAST(:manifest AS jsonb),'COLLECTOR_ROLLBACK_DISABLED')"""),
      dict(id=str(uuid4()),key=command,now=now,manifest=json.dumps(dict(legacy_checkpoint_attempt=str(first['id']),coverage_before_rollback=before,restored_legacy_coverage=baseline))))
    return {'status':'LEGACY_CERTIFICATES_RESTORED','original_attempt':str(first['id']),'source_facts_deleted':0}
