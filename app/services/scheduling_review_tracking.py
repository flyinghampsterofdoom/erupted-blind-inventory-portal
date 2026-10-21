"""Monday review status, using captured generation time without legacy backfill."""
from datetime import datetime,timedelta,time,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import ScheduleGeneratorSnapshot,ScheduleApproval


def monday_deadline(generated_at, timezone_name):
    local=generated_at.astimezone(ZoneInfo(timezone_name))
    if local.weekday()!=6:
        return None
    return datetime.combine(local.date()+timedelta(days=1),time.max,tzinfo=local.tzinfo).astimezone(timezone.utc)


def review_status(db, *, period, now=None):
    from app.services.v2_scheduling_policy_service import organization_policy
    from app.services.v2_scheduling_snapshot_service import verify_approved_content
    from app.services.v2_scheduling_service import SchedulingValidationError
    tz=organization_policy(db).timezone_name
    due=period.review_due_at
    if due is None:
        original=db.scalar(select(ScheduleGeneratorSnapshot).where(
            ScheduleGeneratorSnapshot.schedule_period_id==period.id,
            ScheduleGeneratorSnapshot.generation_kind=='INITIAL').order_by(ScheduleGeneratorSnapshot.sequence))
        if original:
            captured=original.payload.get('result',{}).get('period',{}).get('generated_at')
            due=monday_deadline(datetime.fromisoformat(captured) if captured else original.created_at,tz)
    approval=db.scalar(select(ScheduleApproval).where(
        ScheduleApproval.schedule_period_id==period.id,ScheduleApproval.version==period.version))
    if approval:
        try:
            verify_approved_content(db,period=period,approval=approval)
        except SchedulingValidationError:
            approval=None
    status='APPROVED' if approval else ('OVERDUE' if due and (now or datetime.now(timezone.utc))>due else 'PENDING')
    return {'status':status,'label':{'PENDING':'Pending review','OVERDUE':'Review overdue','APPROVED':'Approved · awaiting publication'}[status],
            'due_at':due,'due_label':due.astimezone(ZoneInfo(tz)).strftime('%A, %b %d, %Y · %I:%M %p %Z') if due else None}
