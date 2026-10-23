"""Independent read-only observation and missed-run decisions; no installed loop."""
from datetime import datetime,timedelta,timezone,date
from pathlib import Path
import json,os,tempfile
from .contracts import EvidenceError,stamp,TZ
from .safeguards import require_capacity,Limits
from .notifications import queue_email


def observe(provider, database, probe, *, now=None):
    """Adapters return actual timestamps. Never re-stamp cached provider data."""
    current=now or datetime.now(timezone.utc);value={'observed_at':current.isoformat(),'worker_rss_bytes':0}
    for name in ('web_memory','web_cpu','database_memory','database_cpu'):
        measured=provider(name)
        if measured.get('unit')!=('bytes' if 'memory' in name else 'cores'):
            raise EvidenceError('Observer metric units are not verified')
        if name=='web_cpu' and not 0<measured.get('window_seconds',0)<=5:
            raise EvidenceError('Fast web CPU observation required; minute averages cannot replace it')
        value[name+'_at']=measured['at']
        value[name+('_bytes' if 'memory' in name else '_cores')]=measured['value']
    pressure=database()
    required=('database_connections','active_business_connections','blocked_business_sessions',
              'business_backup_active','scheduling_active','source_sync_active')
    for key in required:value[key]=pressure[key]
    value['business_latency_ms']=probe()
    require_capacity(value,Limits(),now=current)
    return value


def write_signal(path, sample, *, now=None):
    require_capacity(sample,Limits(),now=now)
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.fv-observer-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(sample,f);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def missed_run(ledger, *, period_start, state, recipient_ref, expected=False, now=None):
    """Independent ledger can remain available while production PostgreSQL fails.

    The caller installs an expectation only after explicit execution approval.
    PREPARED capacity creates no missed-run obligation or outgoing email.
    """
    if expected is not True:return {'status':'PREPARED_NO_EXECUTION_EXPECTATION'}
    current=stamp(now or datetime.now(timezone.utc));start=date.fromisoformat(str(period_start))
    if start.weekday()!=0:raise EvidenceError('Expected period must start Monday')
    due=datetime.combine(start+timedelta(days=1),datetime.min.time(),TZ)+timedelta(hours=6)
    if current<due:return {'status':'NOT_DUE'}
    from .operations import period_identity
    ident=period_identity(start)
    try:complete=state(ident)
    except Exception:complete={'status':'UNAVAILABLE'}
    if complete.get('status')=='COMPLETE' and complete.get('backup_verified') is True:
        return {'status':'COMPLETE'}
    alert=queue_email(ledger,code='MISSED_OR_INCOMPLETE',period_id=ident,
                      recipient_ref=recipient_ref,now=current)
    return {'status':'OWNER_NOTIFICATION_QUEUED','notification_id':alert}
