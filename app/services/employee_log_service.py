from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Employee, EmployeeLogCategory, EmployeeLogEntry, Principal as PrincipalModel

DEFAULT_CATEGORY_LABELS = [
    'Kudos',
    'Late',
    'Change/Till Errors',
    'Customer Service Issue',
    'Ring Downs/Returns',
]

WS_RE = re.compile(r'\s+')


def normalize_name(value: str) -> str:
    return WS_RE.sub(' ', str(value or '').strip().lower())


def ensure_default_categories(db: Session) -> None:
    existing = {
        row[0]
        for row in db.execute(select(EmployeeLogCategory.normalized_label)).all()
    }
    for idx, label in enumerate(DEFAULT_CATEGORY_LABELS):
        normalized = normalize_name(label)
        if normalized in existing:
            continue
        db.add(
            EmployeeLogCategory(
                label=label,
                normalized_label=normalized,
                position=(idx + 1) * 10,
                active=True,
            )
        )
    db.flush()


def list_categories(db: Session, *, include_inactive: bool = False) -> list[EmployeeLogCategory]:
    ensure_default_categories(db)
    query = select(EmployeeLogCategory).order_by(
        EmployeeLogCategory.active.desc(),
        EmployeeLogCategory.position.asc(),
        EmployeeLogCategory.label.asc(),
    )
    if not include_inactive:
        query = query.where(EmployeeLogCategory.active.is_(True))
    return db.execute(query).scalars().all()


def add_category(db: Session, *, label: str, principal_id: int) -> EmployeeLogCategory:
    clean_label = WS_RE.sub(' ', str(label or '').strip())
    if not clean_label:
        raise ValueError('Category label is required')
    normalized = normalize_name(clean_label)
    existing = db.execute(
        select(EmployeeLogCategory).where(EmployeeLogCategory.normalized_label == normalized)
    ).scalar_one_or_none()
    if existing:
        existing.label = clean_label
        existing.active = True
        existing.created_by_principal_id = principal_id
        db.flush()
        return existing

    max_position = db.execute(
        select(EmployeeLogCategory.position).order_by(EmployeeLogCategory.position.desc()).limit(1)
    ).scalar_one_or_none()
    category = EmployeeLogCategory(
        label=clean_label,
        normalized_label=normalized,
        position=(int(max_position) + 10) if max_position is not None else 10,
        active=True,
        created_by_principal_id=principal_id,
    )
    db.add(category)
    db.flush()
    return category


def save_category(db: Session, *, category_id: int, label: str, active: bool) -> EmployeeLogCategory:
    category = db.execute(
        select(EmployeeLogCategory).where(EmployeeLogCategory.id == category_id)
    ).scalar_one_or_none()
    if not category:
        raise ValueError('Category not found')

    clean_label = WS_RE.sub(' ', str(label or '').strip())
    if not clean_label:
        raise ValueError('Category label is required')
    normalized = normalize_name(clean_label)
    duplicate = db.execute(
        select(EmployeeLogCategory).where(
            EmployeeLogCategory.normalized_label == normalized,
            EmployeeLogCategory.id != category_id,
        )
    ).scalar_one_or_none()
    if duplicate:
        raise ValueError('Category label is already in use')

    category.label = clean_label
    category.normalized_label = normalized
    category.active = bool(active)
    db.flush()
    return category


def deactivate_category(db: Session, *, category_id: int) -> EmployeeLogCategory:
    category = db.execute(
        select(EmployeeLogCategory).where(EmployeeLogCategory.id == category_id)
    ).scalar_one_or_none()
    if not category:
        raise ValueError('Category not found')
    category.active = False
    db.flush()
    return category


def list_employees_for_entry(db: Session, *, include_hidden: bool = False) -> list[Employee]:
    query = (
        select(Employee)
        .where(Employee.active.is_(True))
        .order_by(Employee.full_name.asc(), Employee.id.asc())
    )
    if not include_hidden:
        query = query.where(Employee.visible_to_leads.is_(True))
    return db.execute(query).scalars().all()


def list_employee_management_rows(db: Session) -> list[dict]:
    employees = db.execute(
        select(Employee).order_by(Employee.active.desc(), Employee.full_name.asc(), Employee.id.asc())
    ).scalars().all()
    if not employees:
        return []

    stats = db.execute(
        select(
            EmployeeLogEntry.employee_id,
            func.count(EmployeeLogEntry.id).label('entry_count'),
            func.max(EmployeeLogEntry.created_at).label('last_entry_at'),
        ).group_by(EmployeeLogEntry.employee_id)
    ).all()
    stats_by_employee = {
        int(row.employee_id): {
            'entry_count': int(row.entry_count or 0),
            'last_entry_at': row.last_entry_at,
        }
        for row in stats
    }
    return [
        {
            'id': int(employee.id),
            'full_name': str(employee.full_name),
            'visible_to_leads': bool(employee.visible_to_leads),
            'active': bool(employee.active),
            'entry_count': stats_by_employee.get(int(employee.id), {}).get('entry_count', 0),
            'last_entry_at': stats_by_employee.get(int(employee.id), {}).get('last_entry_at'),
        }
        for employee in employees
    ]


def add_employee(
    db: Session,
    *,
    full_name: str,
    visible_to_leads: bool,
    principal_id: int,
) -> Employee:
    clean_name = WS_RE.sub(' ', str(full_name or '').strip())
    if not clean_name:
        raise ValueError('Employee name is required')
    normalized = normalize_name(clean_name)
    existing = db.execute(select(Employee).where(Employee.normalized_name == normalized)).scalar_one_or_none()
    if existing:
        existing.full_name = clean_name
        existing.visible_to_leads = bool(visible_to_leads)
        existing.active = True
        existing.created_by_principal_id = principal_id
        db.flush()
        return existing

    employee = Employee(
        full_name=clean_name,
        normalized_name=normalized,
        visible_to_leads=bool(visible_to_leads),
        active=True,
        created_by_principal_id=principal_id,
    )
    db.add(employee)
    db.flush()
    return employee


def save_employee(
    db: Session,
    *,
    employee_id: int,
    full_name: str,
    visible_to_leads: bool,
    active: bool,
) -> Employee:
    employee = db.execute(select(Employee).where(Employee.id == employee_id)).scalar_one_or_none()
    if not employee:
        raise ValueError('Employee not found')

    clean_name = WS_RE.sub(' ', str(full_name or '').strip())
    if not clean_name:
        raise ValueError('Employee name is required')
    normalized = normalize_name(clean_name)
    duplicate = db.execute(
        select(Employee).where(Employee.normalized_name == normalized, Employee.id != employee_id)
    ).scalar_one_or_none()
    if duplicate:
        raise ValueError('Employee name is already in use')

    employee.full_name = clean_name
    employee.normalized_name = normalized
    employee.visible_to_leads = bool(visible_to_leads)
    employee.active = bool(active)
    db.flush()
    return employee


def deactivate_employee(db: Session, *, employee_id: int) -> Employee:
    employee = db.execute(select(Employee).where(Employee.id == employee_id)).scalar_one_or_none()
    if not employee:
        raise ValueError('Employee not found')
    employee.active = False
    employee.visible_to_leads = False
    db.flush()
    return employee


def create_entry(
    db: Session,
    *,
    employee_id: int,
    category_id: int,
    note: str,
    principal_id: int,
    allow_hidden_employee: bool = False,
) -> EmployeeLogEntry:
    employee = db.execute(select(Employee).where(Employee.id == employee_id)).scalar_one_or_none()
    if not employee or not employee.active:
        raise ValueError('Employee not found')
    if not allow_hidden_employee and not employee.visible_to_leads:
        raise PermissionError('Employee is not visible to leads')

    category = db.execute(
        select(EmployeeLogCategory).where(
            EmployeeLogCategory.id == category_id,
            EmployeeLogCategory.active.is_(True),
        )
    ).scalar_one_or_none()
    if not category:
        raise ValueError('Category is required')

    clean_note = str(note or '').strip()
    if not clean_note:
        raise ValueError('Details are required')

    entry = EmployeeLogEntry(
        employee_id=employee.id,
        category_id=category.id,
        category_label=category.label,
        note=clean_note,
        created_by_principal_id=principal_id,
    )
    db.add(entry)
    db.flush()
    return entry


def _entry_date_conditions(
    *,
    from_date: date | None,
    to_date: date | None,
) -> list:
    conditions = []
    if from_date:
        conditions.append(EmployeeLogEntry.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
    if to_date:
        conditions.append(
            EmployeeLogEntry.created_at < datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )
    return conditions


def list_admin_breakdown(
    db: Session,
    *,
    employee_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    conditions = _entry_date_conditions(from_date=from_date, to_date=to_date)
    if employee_id:
        conditions.append(EmployeeLogEntry.employee_id == employee_id)

    query = (
        select(
            EmployeeLogEntry.id,
            EmployeeLogEntry.employee_id,
            Employee.full_name.label('employee_name'),
            EmployeeLogEntry.category_label,
            EmployeeLogEntry.note,
            EmployeeLogEntry.created_at,
            PrincipalModel.username.label('created_by_username'),
        )
        .join(Employee, Employee.id == EmployeeLogEntry.employee_id)
        .outerjoin(PrincipalModel, PrincipalModel.id == EmployeeLogEntry.created_by_principal_id)
        .order_by(Employee.full_name.asc(), EmployeeLogEntry.created_at.desc(), EmployeeLogEntry.id.desc())
    )
    if conditions:
        query = query.where(and_(*conditions))

    grouped: dict[int, dict] = {}
    for row in db.execute(query).all():
        employee_key = int(row.employee_id)
        group = grouped.setdefault(
            employee_key,
            {
                'employee_id': employee_key,
                'employee_name': row.employee_name,
                'category_counts': Counter(),
                'entries': [],
            },
        )
        category_label = str(row.category_label)
        group['category_counts'][category_label] += 1
        group['entries'].append(
            {
                'id': int(row.id),
                'category_label': category_label,
                'note': row.note,
                'created_at': row.created_at,
                'created_by_username': row.created_by_username or '-',
            }
        )

    if employee_id and not grouped:
        employee = db.execute(select(Employee).where(Employee.id == employee_id)).scalar_one_or_none()
        if employee:
            grouped[int(employee.id)] = {
                'employee_id': int(employee.id),
                'employee_name': employee.full_name,
                'category_counts': Counter(),
                'entries': [],
            }

    output = list(grouped.values())
    for group in output:
        group['category_counts'] = [
            {'label': label, 'count': count}
            for label, count in sorted(group['category_counts'].items(), key=lambda item: (-item[1], item[0].lower()))
        ]
    return output
