from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    ChecklistAnswerValue,
    ChecklistNotesType,
    OpeningChecklistAnswer,
    OpeningChecklistItem,
    OpeningChecklistItemType,
    OpeningChecklistSubmission,
    Store,
)

# Keep checklist content/order exactly as provided.
DEFAULT_CHECKLIST_ITEMS: list[dict] = [
    {'position': 1, 'prompt': 'Was the alarm set and doors locked?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 2, 'prompt': 'Is trash collected and liner replaced?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 3, 'prompt': 'If No, Did you do it?', 'item_type': 'SUB', 'parent_position': 2},
    {'position': 4, 'prompt': 'Is product forward stocked, faced, and organized?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 5, 'prompt': 'If No, Did you fix it?', 'item_type': 'SUB', 'parent_position': 4},
    {'position': 6, 'prompt': 'Are cases clean inside and out?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 7, 'prompt': 'If No, Did you clean them?', 'item_type': 'SUB', 'parent_position': 6},
    {'position': 8, 'prompt': 'Are floors and baseboards clean?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 9, 'prompt': 'If No, Did you clean them?', 'item_type': 'SUB', 'parent_position': 8},
    {'position': 10, 'prompt': 'Are counter tops and cabinets clean?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 11, 'prompt': 'If No, Did you clean them?', 'item_type': 'SUB', 'parent_position': 10},
    {'position': 12, 'prompt': 'Are windows clean inside and out?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 13, 'prompt': 'If No, Did you clean them?', 'item_type': 'SUB', 'parent_position': 12},
    {'position': 14, 'prompt': 'Is behind the counters and register clean?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 15, 'prompt': 'If No, Did you clean them?', 'item_type': 'SUB', 'parent_position': 14},
    {'position': 16, 'prompt': 'Is the bathroom clean and stocked?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 17, 'prompt': 'If No, Did you do it?', 'item_type': 'SUB', 'parent_position': 16},
    {'position': 18, 'prompt': 'Is the refrigerator clean?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 19, 'prompt': 'If No, Did you clean it?', 'item_type': 'SUB', 'parent_position': 18},
    {'position': 20, 'prompt': 'Is the store free of all personal property?', 'item_type': 'PARENT', 'parent_position': None},
    {'position': 21, 'prompt': 'If No, Please add notes below.', 'item_type': 'SUB', 'parent_position': 20},
]


def _ensure_store_exists(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def ensure_default_items(db: Session, *, store_id: int) -> list[OpeningChecklistItem]:
    _ensure_store_exists(db, store_id)
    existing = db.execute(
        select(OpeningChecklistItem)
        .where(OpeningChecklistItem.store_id == store_id, OpeningChecklistItem.active.is_(True))
        .order_by(OpeningChecklistItem.position.asc())
    ).scalars().all()
    if existing:
        return existing

    by_position: dict[int, OpeningChecklistItem] = {}
    for item in DEFAULT_CHECKLIST_ITEMS:
        created = OpeningChecklistItem(
            store_id=store_id,
            position=item['position'],
            prompt=item['prompt'],
            item_type=OpeningChecklistItemType(item['item_type']),
            parent_item_id=None,
            active=True,
        )
        db.add(created)
        db.flush()
        by_position[item['position']] = created

    for item in DEFAULT_CHECKLIST_ITEMS:
        if not item['parent_position']:
            continue
        child = by_position[item['position']]
        parent = by_position[item['parent_position']]
        child.parent_item_id = parent.id

    db.flush()
    return db.execute(
        select(OpeningChecklistItem)
        .where(OpeningChecklistItem.store_id == store_id, OpeningChecklistItem.active.is_(True))
        .order_by(OpeningChecklistItem.position.asc())
    ).scalars().all()


def list_items_for_store(db: Session, *, store_id: int) -> list[OpeningChecklistItem]:
    return ensure_default_items(db, store_id=store_id)


def create_submission(
    db: Session,
    *,
    store_id: int,
    created_by_principal_id: int,
    submitted_by_name: str,
    lead_name: str | None,
    previous_employee: str | None,
    summary_notes_type: str,
    summary_notes: str | None,
    answers_by_item_id: dict[int, str],
) -> OpeningChecklistSubmission:
    items = list_items_for_store(db, store_id=store_id)
    items_by_id = {item.id: item for item in items}

    clean_name = submitted_by_name.strip()
    if not clean_name:
        raise ValueError('Name is required')

    notes_type_value = summary_notes_type.strip().upper()
    try:
        notes_type = ChecklistNotesType(notes_type_value)
    except ValueError as exc:
        raise ValueError('Notes Type is required') from exc

    normalized_answers: dict[int, ChecklistAnswerValue] = {}
    for item in items:
        raw = (answers_by_item_id.get(item.id) or '').strip().upper()
        if item.item_type == OpeningChecklistItemType.PARENT:
            if raw not in {'Y', 'N'}:
                raw = 'N'
        else:
            if raw not in {'Y', 'N', 'NA'}:
                raw = 'NA'
            raw = 'NA' if raw in {'N/A', 'NA'} else raw
        normalized_answers[item.id] = ChecklistAnswerValue(raw)

    # Parent/sub dependency rules.
    for item in items:
        if item.item_type != OpeningChecklistItemType.SUB:
            continue
        if not item.parent_item_id:
            raise ValueError(f'Sub item at position {item.position} is missing parent mapping')

        parent_answer = normalized_answers.get(item.parent_item_id)
        child_answer = normalized_answers.get(item.id)
        if parent_answer is None or child_answer is None:
            raise ValueError('Checklist answers are incomplete')

        if parent_answer == ChecklistAnswerValue.Y and child_answer != ChecklistAnswerValue.NA:
            raise ValueError(f'Sub item at position {item.position} must be N/A when parent is Y')
        if parent_answer == ChecklistAnswerValue.N and child_answer == ChecklistAnswerValue.NA:
            raise ValueError(f'Sub item at position {item.position} must be Y or N when parent is N')

    submission = OpeningChecklistSubmission(
        store_id=store_id,
        submitted_by_name=clean_name,
        lead_name=lead_name.strip() if lead_name and lead_name.strip() else None,
        previous_employee=previous_employee.strip() if previous_employee and previous_employee.strip() else None,
        summary_notes_type=notes_type,
        summary_notes=summary_notes.strip() if summary_notes and summary_notes.strip() else None,
        created_by_principal_id=created_by_principal_id,
    )
    db.add(submission)
    db.flush()

    db.add_all(
        [
            OpeningChecklistAnswer(
                submission_id=submission.id,
                item_id=item.id,
                answer=normalized_answers[item.id],
            )
            for item in items
        ]
    )
    db.flush()
    return submission


def list_submissions(
    db: Session,
    *,
    store_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    conditions = []
    if store_id:
        conditions.append(OpeningChecklistSubmission.store_id == store_id)
    if from_date:
        conditions.append(
            OpeningChecklistSubmission.submitted_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        )
    if to_date:
        conditions.append(
            OpeningChecklistSubmission.submitted_at
            < datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    query = (
        select(
            OpeningChecklistSubmission.id,
            OpeningChecklistSubmission.store_id,
            Store.name.label('store_name'),
            OpeningChecklistSubmission.submitted_by_name,
            OpeningChecklistSubmission.lead_name,
            OpeningChecklistSubmission.summary_notes_type,
            OpeningChecklistSubmission.submitted_at,
        )
        .join(Store, Store.id == OpeningChecklistSubmission.store_id)
        .order_by(OpeningChecklistSubmission.submitted_at.desc())
    )
    if conditions:
        query = query.where(and_(*conditions))

    return [
        {
            'id': row.id,
            'store_id': row.store_id,
            'store_name': row.store_name,
            'submitted_by_name': row.submitted_by_name,
            'lead_name': row.lead_name,
            'summary_notes_type': row.summary_notes_type.value
            if hasattr(row.summary_notes_type, 'value')
            else str(row.summary_notes_type),
            'submitted_at': row.submitted_at,
        }
        for row in db.execute(query).all()
    ]


def get_submission_detail(db: Session, *, submission_id: int) -> dict:
    submission = db.execute(
        select(OpeningChecklistSubmission, Store.name)
        .join(Store, Store.id == OpeningChecklistSubmission.store_id)
        .where(OpeningChecklistSubmission.id == submission_id)
    ).one_or_none()
    if not submission:
        raise ValueError('Opening checklist submission not found')

    submission_row, store_name = submission
    items = db.execute(
        select(OpeningChecklistItem, OpeningChecklistAnswer.answer)
        .join(
            OpeningChecklistAnswer,
            and_(
                OpeningChecklistAnswer.item_id == OpeningChecklistItem.id,
                OpeningChecklistAnswer.submission_id == submission_id,
            ),
        )
        .where(OpeningChecklistItem.store_id == submission_row.store_id)
        .order_by(OpeningChecklistItem.position.asc())
    ).all()

    return {
        'id': submission_row.id,
        'store_name': store_name,
        'submitted_by_name': submission_row.submitted_by_name,
        'lead_name': submission_row.lead_name,
        'previous_employee': submission_row.previous_employee,
        'summary_notes_type': submission_row.summary_notes_type.value,
        'summary_notes': submission_row.summary_notes,
        'submitted_at': submission_row.submitted_at,
        'answers': [
            {
                'position': item.position,
                'prompt': item.prompt,
                'item_type': item.item_type.value,
                'answer': answer.value if hasattr(answer, 'value') else str(answer),
            }
            for item, answer in items
        ],
    }
