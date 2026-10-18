"""Small extensions to the established Scheduling lifecycle."""
from datetime import timedelta
from functools import wraps
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import SchedulePeriod, SchedulePeriodStatus, ScheduleGenerationRun


def publication_status(db, *, today):
    from app.services.v2_scheduling_pattern_service import sunday
    next_week = sunday(today) + timedelta(weeks=1)
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.week_end_date >= today)))
    published = {p.week_start_date for p in periods if p.status == SchedulePeriodStatus.PUBLISHED}
    if next_week not in published:
        return {'color': 'red', 'label': 'Next operating week unpublished', 'next_week': next_week}
    if any(p.status == SchedulePeriodStatus.DRAFT and p.generated_at is not None for p in periods):
        return {'color': 'yellow', 'label': 'Generated weeks awaiting review/publication', 'next_week': next_week}
    return {'color': 'green', 'label': 'All generated weeks published', 'next_week': next_week}


def generation_outcome(origin):
    """Savepoint protects the batch; failure evidence survives caller rollback."""
    def decorate(function):
        @wraps(function)
        def run(db, *, principal, **kwargs):
            try:
                with db.begin_nested():
                    result = function(db, principal=principal, **kwargs)
                    if result.get('status') != 'disabled':
                        db.add(ScheduleGenerationRun(actor_principal_id=principal.id,
                            origin=origin, status='SUCCESS', result={
                                'created_period_ids': result.get('created_period_ids', result.get('generated_period_ids', []))}))
                        db.flush()
                    return result
            except Exception as exc:
                # Separate connection intentionally stores only sanitized outcome metadata.
                try:
                    bind = db.get_bind()
                    with Session(bind=getattr(bind, 'engine', bind)) as evidence:
                        evidence.add(ScheduleGenerationRun(actor_principal_id=principal.id,
                            origin=origin, status='FAILED', result={'error_type': type(exc).__name__}))
                        evidence.commit()
                except Exception:
                    logging.getLogger(__name__).error('Schedule generation failed (%s); outcome storage unavailable.', type(exc).__name__)
                raise
        return run
    return decorate
