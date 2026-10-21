"""Owner comparisons and retained week audit, including removed shift evidence."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from sqlalchemy import select,or_,and_
from app.auth import Role
from app.models import (AuditLog,SchedulePeriod,ScheduleShift,ScheduleApproval,ScheduleGeneratorSnapshot,
    Principal as PrincipalModel,Employee,Store,ScheduleAttendanceEvent,ScheduleCoverageCommitment,LeadDutyOutcome)
from app.services.v2_scheduling_snapshot_service import canonical_checksum


FIELD_LABELS={'employee_id':'Employee','store_id':'Store','shift_date':'Date','start_time':'Start time','end_time':'End time','unpaid_break_minutes':'Unpaid break','is_lead_of_day':'Lead designation','lead_of_day_manually_assigned':'Lead override','is_double_coverage':'Double Coverage','double_coverage_manually_assigned':'Coverage override','employee_note':'Assignment note','manually_locked':'Assignment protection','lock_reason':'Protection reason'}


def week_history(db,period):
    periods=list(db.scalars(select(SchedulePeriod).where(SchedulePeriod.week_start_date==period.week_start_date).order_by(SchedulePeriod.revision_number)))
    pids=[p.id for p in periods]
    shift_ids=set(db.scalars(select(ScheduleShift.id).where(ScheduleShift.schedule_period_id.in_(pids))))
    base=or_(and_(AuditLog.meta['entity_type'].as_string()=='schedule_period',AuditLog.meta['entity_id'].as_string().in_([str(x) for x in pids])),
             AuditLog.meta['metadata']['schedule_period_id'].as_string().in_([str(x) for x in pids]))
    # Generic removal events retain the deleted shift ID and its period ID.
    seed=list(db.scalars(select(AuditLog).where(AuditLog.action.like('V2:SCHEDULING%:%'),base)))
    shift_ids.update(int(a.meta['entity_id']) for a in seed if a.meta.get('entity_type')=='schedule_shift' and str(a.meta.get('entity_id','')).isdigit())
    event_ids={
        'schedule_attendance_event':list(db.scalars(select(ScheduleAttendanceEvent.id).where(ScheduleAttendanceEvent.schedule_shift_id.in_(shift_ids)))),
        'schedule_coverage_commitment':list(db.scalars(select(ScheduleCoverageCommitment.id).where(ScheduleCoverageCommitment.schedule_shift_id.in_(shift_ids)))),
        'lead_duty_outcome':list(db.scalars(select(LeadDutyOutcome.id).where(or_(LeadDutyOutcome.scheduled_shift_id.in_(shift_ids),LeadDutyOutcome.business_date.between(period.week_start_date,period.week_end_date))))),
    }
    ids=[str(i) for i in shift_ids]
    clauses=[base,and_(AuditLog.meta['entity_type'].as_string()=='schedule_shift',AuditLog.meta['entity_id'].as_string().in_(ids))]
    clauses.extend(AuditLog.meta['metadata'][key].as_string().in_(ids) for key in ('shift_id','counterpart_shift_id','schedule_shift_id','scheduled_shift_id'))
    clauses.extend(and_(AuditLog.meta['entity_type'].as_string()==kind,AuditLog.meta['entity_id'].as_string().in_([str(i) for i in values])) for kind,values in event_ids.items())
    audits=list(db.scalars(select(AuditLog).where(AuditLog.action.like('V2:SCHEDULING%:%'),or_(*clauses)).order_by(AuditLog.id)))
    approvals=list(db.scalars(select(ScheduleApproval).where(ScheduleApproval.schedule_period_id.in_(pids)).order_by(ScheduleApproval.created_at,ScheduleApproval.id)))
    people={p.id:p.username for p in db.scalars(select(PrincipalModel))}
    employees={e.id:e.full_name for e in db.scalars(select(Employee))}
    stores={s.id:s.name for s in db.scalars(select(Store))}
    events=[]
    for a in audits:
        before=a.meta.get('before') or {};after=a.meta.get('after') or {};metadata=a.meta.get('metadata',{})
        def person(row):
            eid=row.get('employee_id')
            return employees.get(eid,f'Employee #{eid}') if eid else 'Unassigned'
        try:
            at=datetime.fromisoformat(a.meta.get('occurred_at',''))
        except (TypeError,ValueError):
            at=a.created_at
        def display(key,value):
            if key=='store_id':return stores.get(value,f'Store #{value}')
            if isinstance(value,bool):return 'Yes' if value else 'No'
            return value if value is not None and value!='' else '—'
        changes=[{'label':label,'before':display(key,before.get(key)),'after':display(key,after.get(key))}
                 for key,label in FIELD_LABELS.items() if key!='employee_id' and before and after and before.get(key)!=after.get(key)]
        events.append({'record':a,'changes':changes,'actor':people.get(a.actor_principal_id,f'User #{a.actor_principal_id}'),
            'at':at,'action':a.action.split(':')[-1].replace('_',' ').title(),
            'before':person(before) if 'employee_id' in before else None,
            'after':person(after) if 'employee_id' in after else None,
            'shift_id':metadata.get('shift_id') or metadata.get('schedule_shift_id') or (a.meta.get('entity_id') if a.meta.get('entity_type')=='schedule_shift' else None)})
    return {'periods':periods,'approvals':approvals,'audits':audits,'events':events,'principal_names':people}


def owner_comparison(db, *, principal, periods, approved_id=None, published_id=None):
    if not principal.active or principal.role!=Role.ADMIN:
        raise PermissionError('Original schedule evidence requires an active Admin.')
    snapshots=list(db.scalars(select(ScheduleGeneratorSnapshot).where(ScheduleGeneratorSnapshot.schedule_period_id.in_([p.id for p in periods])).order_by(ScheduleGeneratorSnapshot.id)))
    for row in snapshots:
        if canonical_checksum(row.payload)!=row.checksum:
            raise ValueError('Snapshot checksum verification failed; comparison is unavailable.')
    kinds={kind:[r for r in snapshots if r.generation_kind==kind] for kind in ('INITIAL','APPROVAL','PUBLICATION')}
    def choose(kind,selected):
        rows=kinds[kind]
        if selected:
            found=next((r for r in rows if r.id==selected),None)
            if found is None:raise ValueError('Choose a captured snapshot from this scheduling week.')
            return found
        return rows[0] if kind=='INITIAL' and rows else rows[-1] if rows else None
    chosen=[choose('INITIAL',None),choose('APPROVAL',approved_id),choose('PUBLICATION',published_id)]
    period_map={p.id:p for p in periods}
    people={p.id:p.username for p in db.scalars(select(PrincipalModel))}
    def describe(snapshot):
        return {'id':snapshot.id,'kind':snapshot.generation_kind,'revision':period_map[snapshot.schedule_period_id].revision_number,
            'version':snapshot.version_after,'actor':people.get(snapshot.actor_principal_id,f'User #{snapshot.actor_principal_id}'),
            'at':snapshot.created_at,'checksum':snapshot.checksum,'approval_id':snapshot.payload.get('approval_id')}
    states=[deepcopy(s.payload.get('result',{}).get('shifts',[])) if s else [] for s in chosen]
    # Follow captured lineage, with a coverage-slot fallback for repaired rows.
    lineage={row['id']:row.get('source_shift_id') for s in snapshots for row in s.payload.get('result',{}).get('shifts',[])}
    def root(row):
        key=row['id'];seen=set()
        while lineage.get(key) and key not in seen:
            seen.add(key);key=lineage[key]
        return key
    def slot(row):return tuple(row.get(k) for k in ('shift_date','store_id','start_time','end_time','is_double_coverage'))
    groups={}
    roots={}
    for col,rows in enumerate(states):
        for row in rows:
            key=roots.get(root(row))
            if key is None:
                key=next((key for key,values in groups.items() if values[col] is None and any(v and slot(v)==slot(row) for v in values)),None)
            if key is None:key=len(groups);groups[key]=[None,None,None]
            roots[root(row)]=key;groups[key][col]=row
    compare_fields=('employee_id','store_id','shift_date','start_time','end_time','unpaid_break_minutes','is_lead_of_day','lead_of_day_manually_assigned','is_double_coverage','double_coverage_manually_assigned','employee_note','manually_locked','lock_reason')
    rows=[]
    for values in groups.values():
        present=[v for v in values if v]
        first=present[0]
        changes=[]
        # Missing evidence is unknown, not an empty historical schedule.
        baseline=values[0] if chosen[0] else next((v for i,v in enumerate(values) if chosen[i] and v),None)
        for i,row in enumerate(values):
            if chosen[i] is None:changes.append('Not captured')
            elif baseline is None:changes.append('Added' if row else '')
            elif row is None:changes.append('Removed')
            else:
                changed=[k for k in compare_fields if baseline.get(k)!=row.get(k)]
                changes.append('Added' if chosen[0] and values[0] is None else ', '.join(FIELD_LABELS[k] for k in changed) if changed else 'Unchanged')
        rows.append({'date':first.get('shift_date'),'store':first.get('store_name') or f"Store #{first.get('store_id')}",
            'time':f"{first.get('start_time','')[:5]}–{first.get('end_time','')[:5]}",'states':values,'changes':changes,
            'changed':len({tuple(v.get(k) for k in compare_fields) if v else None for i,v in enumerate(values) if chosen[i]})>1})
    rows.sort(key=lambda r:(r['date'] or '',r['store'],r['time']))
    return {'selected':[describe(s) if s else None for s in chosen],'rows':rows,
        'approval_options':[describe(s) for s in kinds['APPROVAL']],
        'publication_options':[describe(s) for s in kinds['PUBLICATION']],
        'changed_count':sum(r['changed'] for r in rows)}
