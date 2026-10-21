"""Calendar reads must be cheap, accurate and unable to change scheduling facts."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event, select

from app.models import SchedulePeriod, SchedulePeriodStatus, StoreSpecialHour
from app.services.v2_scheduling_calendar_service import calendar_week_statuses
from app.services.v2_scheduling_service import create_draft_period
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary, login


def test_status_metadata_single_query_and_no_mutations(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    with Session() as db:
        published = create_draft_period(db, principal=actor, week_start=date(2026, 12, 6))
        published.status = SchedulePeriodStatus.PUBLISHED
        published.published_at = datetime.now(timezone.utc)
        mixed = create_draft_period(db, principal=actor, week_start=date(2026, 12, 13))
        mixed.status = SchedulePeriodStatus.PUBLISHED
        mixed.published_at = datetime.now(timezone.utc)
        db.flush()
        create_draft_period(db, principal=actor, week_start=date(2026, 12, 13))
        generated = create_draft_period(db, principal=actor, week_start=date(2026, 12, 20))
        generated.generated_at = datetime.now(timezone.utc)
        cleared = create_draft_period(db, principal=actor, week_start=date(2026, 12, 27))
        cleared.status = SchedulePeriodStatus.ARCHIVED
        cleared.cleared_at = datetime.now(timezone.utc)
        db.commit()
        statements = []
        def capture(conn, cursor, statement, *args):
            statements.append(statement)
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            result = calendar_week_statuses(db, start=date(2026, 12, 1), end=date(2027, 1, 2))
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert len(statements) == 1
        assert 'EXISTS (SELECT schedule_shifts.id' in statements[0]
        assert 'schedule_shifts.employee_id' not in statements[0]
        assert 'schedule_periods.notes' not in statements[0]
        assert [week['status'] for week in result] == ['not-generated', 'published', 'mixed', 'unpublished', 'not-generated']
        assert not db.new and not db.dirty and not db.deleted
        assert result[3]['start'] == '2026-12-20'
        with pytest.raises(ValueError):
            calendar_week_statuses(db, start=date(2026, 1, 1), end=date(2026, 12, 31))
        with pytest.raises(ValueError):
            calendar_week_statuses(db, start=date(2026, 12, 2), end=date(2026, 12, 1))


def test_navigation_christmas_closure_empty_week_and_range_guards(primary):
    primary.account('ADMIN'); login(primary)
    with primary.Session() as db:
        p = create_draft_period(db, principal=primary.manager, week_start=date(2026, 12, 20))
        p.generated_at = datetime.now(timezone.utc)
        period_id, version = p.id, p.version
        closure = StoreSpecialHour(store_id=primary.ids['north'], calendar_date=date(2026, 12, 25),
            event_name='Christmas', closed_all_day=True, active=True,
            created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id)
        db.add(closure); db.commit()
    response = primary.client.get('/v2/scheduling/week?start=2026-12-25')
    assert response.status_code == 200
    assert 'data-week-start="2026-12-20"' in response.text
    assert '2026-12-25' in response.text and 'Business Closed' in response.text
    assert 'data-week-picker-open' in response.text
    assert 'start=2026-12-13' in response.text and 'start=2026-12-27' in response.text
    response = primary.client.get('/v2/scheduling/api/calendar-weeks?start=2026-11-29&end=2027-01-02')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert next(w for w in response.json()['weeks'] if w['start'] == '2026-12-20')['status'] == 'unpublished'
    for query in ('start=2026-12-30&end=2026-12-01', 'start=invalid&end=2026-12-01', 'start=2026-01-01&end=2026-12-01'):
        assert primary.client.get('/v2/scheduling/api/calendar-weeks?' + query).status_code == 422
    assert primary.client.get('/v2/scheduling/api/calendar-weeks?start=2026-12-01&end=2026-12-31&store_id=999999').status_code in (403, 404)
    empty = primary.client.get('/v2/scheduling/week?start=2027-02-07')
    assert empty.status_code == 200 and 'No schedule exists for this week' in empty.text
    with primary.Session() as db:
        assert db.get(SchedulePeriod, period_id).version == version
        assert db.scalar(select(SchedulePeriod).where(SchedulePeriod.week_start_date == date(2027, 2, 7))) is None
        assert db.scalar(select(StoreSpecialHour.closed_all_day).where(StoreSpecialHour.calendar_date == date(2026, 12, 25))) is True


def test_calendar_requires_board_access(primary):
    primary.account('STORE'); login(primary)
    assert primary.client.get('/v2/scheduling/api/calendar-weeks?start=2026-12-01&end=2026-12-31').status_code == 403


def test_empty_draft_is_not_generated_and_manual_content_is_unpublished(scheduling_db):
    from app.services.v2_scheduling_service import create_shift
    from test_v2_scheduling_foundation import _shift
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=actor, week_start=date(2026, 12, 20))
        first = calendar_week_statuses(db, start=date(2026, 12, 20), end=date(2026, 12, 26))
        assert first[0]['status'] == 'not-generated'
        assert first[0]['label'] == 'Not generated (empty draft)'
        create_shift(db, principal=actor, schedule_period_id=period.id,
            values=_shift(ids['alex'], ids['north'], day=date(2026, 12, 21)),
            expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']))
        second = calendar_week_statuses(db, start=date(2026, 12, 20), end=date(2026, 12, 26))
        assert second[0]['status'] == 'unpublished'


def test_archived_publication_and_historical_entries_are_not_current_publication(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        archived = create_draft_period(db, principal=actor, week_start=date(2026, 12, 20))
        archived.status = SchedulePeriodStatus.ARCHIVED
        archived.published_at = datetime.now(timezone.utc)
        archived.generated_at = datetime.now(timezone.utc)
        db.flush()
        draft = create_draft_period(db, principal=actor, week_start=date(2026, 12, 20))
        draft.generated_at = datetime.now(timezone.utc)
        historical = create_draft_period(db, principal=actor, week_start=date(2026, 12, 27))
        historical.is_historical = True
        db.flush()
        result = calendar_week_statuses(db, start=date(2026, 12, 20), end=date(2027, 1, 2))
        assert result[0]['status'] == 'unpublished'
        assert result[1]['status'] == 'not-generated'
