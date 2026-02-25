from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    DailyChoreEntry,
    DailyChoreSheet,
    DailyChoreSheetStatus,
    DailyChoreTask,
    Store,
)

DEFAULT_DAILY_CHORE_TASKS: list[dict] = [
    {'position': 1, 'section': 'Opening', 'prompt': 'Take out trash and recycling'},
    {'position': 2, 'section': 'Opening', 'prompt': 'Count till and change box'},
    {'position': 3, 'section': 'Opening', 'prompt': 'Turn on case lights and open sign'},
    {'position': 4, 'section': 'Opening', 'prompt': 'Windex outer case glass'},
    {'position': 5, 'section': 'Opening', 'prompt': 'Send off Opening Checklist'},
    {'position': 6, 'section': 'Before 2pm', 'prompt': 'Forward Stock'},
    {'position': 7, 'section': 'Before 2pm', 'prompt': 'Face and Organize Product'},
    {'position': 8, 'section': 'Before 2pm', 'prompt': 'Clean cases inside and out'},
    {'position': 9, 'section': 'Before 2pm', 'prompt': 'Clean counter tops'},
    {'position': 10, 'section': 'Before 2pm', 'prompt': 'Windex outside windows'},
    {'position': 11, 'section': 'Before 2pm', 'prompt': 'Windex inside windows'},
    {'position': 12, 'section': 'Before 2pm', 'prompt': 'Clean floors'},
    {'position': 13, 'section': 'Before 2pm', 'prompt': 'Wipe down baseboards'},
    {'position': 14, 'section': 'Before 2pm', 'prompt': 'Wipe down cabinets'},
    {'position': 15, 'section': 'Before 2pm', 'prompt': 'Clean behind counters/register'},
    {'position': 16, 'section': 'Before 10pm', 'prompt': 'Forward stock'},
    {'position': 17, 'section': 'Before 10pm', 'prompt': 'Face and Organize Product'},
    {'position': 18, 'section': 'Before 10pm', 'prompt': 'Wipe counters'},
    {'position': 19, 'section': 'Before 10pm', 'prompt': 'Windex outer case glass'},
    {'position': 20, 'section': 'Before 10pm', 'prompt': 'Windex inside windows'},
    {'position': 21, 'section': 'Before 10pm', 'prompt': 'Clean refrigerator'},
    {'position': 22, 'section': 'Before 10pm', 'prompt': 'Pack up belongings'},
    {'position': 23, 'section': 'Closing', 'prompt': 'Lock door'},
    {'position': 24, 'section': 'Closing', 'prompt': 'Collect trash and replace liners'},
    {'position': 25, 'section': 'Closing', 'prompt': 'Turn off case lights and open sign'},
    {'position': 26, 'section': 'Closing', 'prompt': 'Clean bathroom'},
    {'position': 27, 'section': 'Closing', 'prompt': 'Count till and change box'},
    {'position': 28, 'section': 'Closing', 'prompt': 'Send off checklist'},
]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today_utc() -> date:
    return _now().date()


def ensure_default_tasks(db: Session, *, store_id: int) -> list[DailyChoreTask]:
    store_exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store_exists:
        raise ValueError('Store not found')

    existing = db.execute(
        select(DailyChoreTask)
        .where(DailyChoreTask.store_id == store_id, DailyChoreTask.active.is_(True))
        .order_by(DailyChoreTask.position.asc())
    ).scalars().all()
    if existing:
        return existing

    db.add_all(
        [
            DailyChoreTask(
                store_id=store_id,
                position=item['position'],
                section=item['section'],
                prompt=item['prompt'],
                active=True,
            )
            for item in DEFAULT_DAILY_CHORE_TASKS
        ]
    )
    db.flush()

    return db.execute(
        select(DailyChoreTask)
        .where(DailyChoreTask.store_id == store_id, DailyChoreTask.active.is_(True))
        .order_by(DailyChoreTask.position.asc())
    ).scalars().all()


def get_or_create_today_sheet(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
) -> tuple[DailyChoreSheet, bool]:
    ensure_default_tasks(db, store_id=store_id)
    today = _today_utc()

    sheet = db.execute(
        select(DailyChoreSheet)
        .where(
            DailyChoreSheet.store_id == store_id,
            DailyChoreSheet.sheet_date == today,
        )
        .order_by(DailyChoreSheet.created_at.desc())
    ).scalars().first()
    if sheet:
        return sheet, False

    sheet = DailyChoreSheet(
        store_id=store_id,
        sheet_date=today,
        employee_name='',
        status=DailyChoreSheetStatus.DRAFT,
        created_by_principal_id=principal_id,
    )
    db.add(sheet)
    db.flush()

    tasks = db.execute(
        select(DailyChoreTask)
        .where(DailyChoreTask.store_id == store_id, DailyChoreTask.active.is_(True))
        .order_by(DailyChoreTask.position.asc())
    ).scalars().all()

    db.add_all(
        [
            DailyChoreEntry(
                sheet_id=sheet.id,
                task_id=task.id,
                completed=False,
            )
            for task in tasks
        ]
    )
    db.flush()
    return sheet, True


def get_store_today_sheet(db: Session, *, store_id: int) -> DailyChoreSheet | None:
    return db.execute(
        select(DailyChoreSheet)
        .where(
            DailyChoreSheet.store_id == store_id,
            DailyChoreSheet.sheet_date == _today_utc(),
        )
        .order_by(DailyChoreSheet.created_at.desc())
    ).scalars().first()


def get_store_sheet_strict_today(db: Session, *, store_id: int, sheet_id: int) -> DailyChoreSheet:
    sheet = db.execute(select(DailyChoreSheet).where(DailyChoreSheet.id == sheet_id)).scalar_one_or_none()
    if not sheet:
        raise ValueError('Daily chore sheet not found')
    if sheet.store_id != store_id:
        raise PermissionError('Not allowed to access this daily chore sheet')
    if sheet.sheet_date != _today_utc():
        raise PermissionError('Store logins can only access today\'s daily chore sheet')
    return sheet


def get_store_sheet_rows(db: Session, *, sheet_id: int) -> list[dict]:
    rows = db.execute(
        select(
            DailyChoreTask.id,
            DailyChoreTask.position,
            DailyChoreTask.section,
            DailyChoreTask.prompt,
            DailyChoreEntry.completed,
            DailyChoreEntry.completed_at,
        )
        .join(DailyChoreEntry, DailyChoreEntry.task_id == DailyChoreTask.id)
        .where(DailyChoreEntry.sheet_id == sheet_id)
        .order_by(DailyChoreTask.position.asc())
    ).all()
    return [
        {
            'task_id': row.id,
            'position': row.position,
            'section': row.section,
            'prompt': row.prompt,
            'completed': bool(row.completed),
            'completed_at': row.completed_at,
        }
        for row in rows
    ]


def save_sheet_progress(
    db: Session,
    *,
    sheet: DailyChoreSheet,
    employee_name: str,
    completed_task_ids: set[int],
    submit: bool,
) -> DailyChoreSheet:
    if sheet.status == DailyChoreSheetStatus.SUBMITTED:
        raise ValueError('Daily chore sheet is already submitted')

    clean_employee_name = employee_name.strip()
    if not clean_employee_name:
        raise ValueError('Name is required')

    entries = db.execute(select(DailyChoreEntry).where(DailyChoreEntry.sheet_id == sheet.id)).scalars().all()
    now = _now()

    for entry in entries:
        should_be_completed = entry.task_id in completed_task_ids
        if should_be_completed and not entry.completed:
            entry.completed = True
            entry.completed_at = now
            entry.updated_at = now
        elif not should_be_completed and entry.completed:
            entry.completed = False
            entry.completed_at = None
            entry.updated_at = now

    sheet.employee_name = clean_employee_name
    sheet.updated_at = now
    if submit:
        sheet.status = DailyChoreSheetStatus.SUBMITTED
        sheet.submitted_at = now

    db.flush()
    return sheet


def restart_today_sheet(db: Session, *, store_id: int, sheet_id: int) -> DailyChoreSheet:
    sheet = get_store_sheet_strict_today(db, store_id=store_id, sheet_id=sheet_id)
    if sheet.status != DailyChoreSheetStatus.SUBMITTED:
        raise ValueError('Only submitted daily chore sheets can be restarted')

    now = _now()
    entries = db.execute(select(DailyChoreEntry).where(DailyChoreEntry.sheet_id == sheet.id)).scalars().all()
    for entry in entries:
        entry.completed = False
        entry.completed_at = None
        entry.updated_at = now

    sheet.status = DailyChoreSheetStatus.DRAFT
    sheet.submitted_at = None
    sheet.employee_name = ''
    sheet.updated_at = now
    db.flush()
    return sheet


def list_sheets_for_audit(
    db: Session,
    *,
    store_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    conditions = []
    if store_id:
        conditions.append(DailyChoreSheet.store_id == store_id)
    if from_date:
        conditions.append(DailyChoreSheet.sheet_date >= from_date)
    if to_date:
        conditions.append(DailyChoreSheet.sheet_date <= to_date)

    query = (
        select(
            DailyChoreSheet.id,
            DailyChoreSheet.sheet_date,
            DailyChoreSheet.status,
            DailyChoreSheet.employee_name,
            DailyChoreSheet.submitted_at,
            DailyChoreSheet.updated_at,
            Store.name.label('store_name'),
        )
        .join(Store, Store.id == DailyChoreSheet.store_id)
        .order_by(DailyChoreSheet.sheet_date.desc(), DailyChoreSheet.updated_at.desc())
    )
    if conditions:
        query = query.where(and_(*conditions))

    return [
        {
            'id': row.id,
            'sheet_date': row.sheet_date,
            'status': row.status.value if hasattr(row.status, 'value') else str(row.status),
            'employee_name': row.employee_name,
            'submitted_at': row.submitted_at,
            'updated_at': row.updated_at,
            'store_name': row.store_name,
        }
        for row in db.execute(query).all()
    ]


def get_sheet_detail_for_audit(db: Session, *, sheet_id: int) -> dict:
    sheet_row = db.execute(
        select(DailyChoreSheet, Store.name)
        .join(Store, Store.id == DailyChoreSheet.store_id)
        .where(DailyChoreSheet.id == sheet_id)
    ).one_or_none()
    if not sheet_row:
        raise ValueError('Daily chore sheet not found')

    sheet, store_name = sheet_row
    rows = get_store_sheet_rows(db, sheet_id=sheet.id)
    return {
        'id': sheet.id,
        'store_name': store_name,
        'sheet_date': sheet.sheet_date,
        'status': sheet.status.value,
        'employee_name': sheet.employee_name,
        'submitted_at': sheet.submitted_at,
        'updated_at': sheet.updated_at,
        'rows': rows,
    }
