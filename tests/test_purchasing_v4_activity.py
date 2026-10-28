from datetime import date,timedelta
from decimal import Decimal
from app.purchasing_v4.activity import ActivityEvent,classify,HORIZONS,SOURCES

CUTOFF=date(2026,10,27)

def event(days,source='sales',units=1):return ActivityEvent(source,'fixture',CUTOFF-timedelta(days=days),Decimal(units) if units is not None else None,'fixture-source')

def coverage(known):return {(s,h):known for s in SOURCES for h in HORIZONS}

def test_positive_activity_does_not_require_inventing_missing_negative_coverage():
    row=classify([event(2)],coverage(False),cutoff=CUTOFF)
    assert row['cohort']=='VERIFIED_RECENT_ACTIVITY' and row['policy_changed'] is False
    assert row['streams']['receipts']['windows']['30']['state']=='UNKNOWN'


def test_intermitttent_longer_term_unknown_and_verified_none_distinct():
    assert classify([event(60)],coverage(False),cutoff=CUTOFF)['cohort']=='INTERMITTENT_OBSERVED_ACTIVITY'
    row=classify([event(200)],coverage(False),cutoff=CUTOFF)
    assert row['cohort']=='INSUFFICIENT_EVIDENCE' and row['longer_term']=='OBSERVED_ACTIVITY_ONLY_BEFORE_180_DAYS'
    assert classify([],coverage(True),cutoff=CUTOFF)['cohort']=='NO_MEANINGFUL_RECENT_RECORDED_ACTIVITY'
    assert classify([],coverage(False),cutoff=CUTOFF)['cohort']=='INSUFFICIENT_EVIDENCE'


def test_each_window_separates_sources_and_unknown_from_zero():
    row=classify([event(29),event(30),event(59,'receipts'),event(89,'purchasing'),event(179,'sales'),event(10,'inventory',None)],coverage(False),cutoff=CUTOFF)
    assert row['streams']['sales']['windows']['30']['positive_records']==2
    assert row['streams']['sales']['windows']['180']['positive_records']==3
    assert row['streams']['inventory']['windows']['30']['observation_count']==1
    assert row['streams']['inventory']['windows']['30']['state']=='UNKNOWN'
    assert row['streams']['receipts']['windows']['30']['state']=='UNKNOWN'
