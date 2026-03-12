from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import DashboardCardAssignment, DashboardCategory

DEFAULT_CATEGORY_NAMES = [
    'Store Counts',
    'Settings',
    'Ordering',
    'Audits',
    'Change',
    'Reports',
    'Forms',
    'Uncategorized',
]

DEFAULT_CARD_CATEGORY_BY_KEY = {
    'manage-count-groups': 'Store Counts',
    'current-previous-counts': 'Store Counts',
    'store-count-full': 'Store Counts',
    'users': 'Settings',
    'ordering-tool': 'Ordering',
    'daily-chore-sheet-audit': 'Audits',
    'store-opening-checklist-audit': 'Audits',
    'daily-chore-task-editor': 'Audits',
    'change-box-count': 'Change',
    'master-safe-audit': 'Change',
    'change-box-audit': 'Change',
    'cogs-report': 'Reports',
    'stock-value-on-hand': 'Reports',
    'reports-exports': 'Reports',
    'change-forms': 'Forms',
    'exchange-return-forms': 'Forms',
}


def dashboard_card_catalog() -> list[dict[str, Any]]:
    return [
        {'key': 'manage-count-groups', 'href': '/management/groups', 'label': 'Manage Count Groups', 'requires_admin': True},
        {'key': 'current-previous-counts', 'href': '/management/sessions', 'label': 'Current / Previous Counts', 'requires_admin': False},
        {'key': 'users', 'href': '/management/users', 'label': 'Users', 'requires_admin': True},
        {'key': 'ordering-tool', 'href': '/management/ordering-tool', 'label': 'Erupted Ordering Tool', 'requires_admin': True},
        {'key': 'daily-chore-sheet-audit', 'href': '/management/daily-chore-lists', 'label': 'Daily Chore Sheet Audit', 'requires_admin': False},
        {'key': 'daily-chore-task-editor', 'href': '/management/daily-chore-tasks', 'label': 'Daily Chore Task Editor', 'requires_admin': True},
        {'key': 'store-opening-checklist-audit', 'href': '/management/opening-checklists', 'label': 'Store Opening Checklist Audit', 'requires_admin': False},
        {'key': 'change-box-count', 'href': '/management/change-box-count', 'label': 'Change Box Count', 'requires_admin': False},
        {'key': 'store-count-full', 'href': '/management/store-count', 'label': 'Store Count (Full)', 'requires_admin': True},
        {'key': 'change-forms', 'href': '/management/change-forms', 'label': 'Change Forms', 'requires_admin': False},
        {'key': 'exchange-return-forms', 'href': '/management/exchange-return-forms', 'label': 'Exchange/Return Forms', 'requires_admin': False},
        {'key': 'change-box-audit', 'href': '/management/change-box-audit', 'label': 'Change Box Audit', 'requires_admin': True},
        {'key': 'master-safe-audit', 'href': '/management/master-safe-audit', 'label': 'Master Safe Audit', 'requires_admin': True},
        {'key': 'non-sellable-stock-take', 'href': '/management/non-sellable-stock-take', 'label': 'Non-sellable Stock Take', 'requires_admin': False},
        {'key': 'customer-requests', 'href': '/management/customer-requests', 'label': 'Customer Requests', 'requires_admin': False},
        {'key': 'audit-queue', 'href': '/management/audit-queue', 'label': 'Audit Queue', 'requires_admin': False},
        {'key': 'reports-exports', 'href': '/management/reports', 'label': 'Reports & Exports', 'requires_admin': False},
        {'key': 'cogs-report', 'href': '/management/reports/cogs', 'label': 'COGS Report', 'requires_admin': False},
        {'key': 'stock-value-on-hand', 'href': '/management/reports/stock-value-on-hand', 'label': 'Stock Value On Hand', 'requires_admin': True},
        {'key': 'store-par-reset-tool', 'href': '/management/store-par-reset', 'label': 'Store Par Reset Tool', 'requires_admin': True},
        {'key': 'cash-reconciliation', 'href': '/management/cash-reconciliation', 'label': 'Cash Reconciliation', 'requires_admin': True},
    ]


def _default_sections(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible_cards = list(cards)
    by_name: dict[str, list[dict[str, Any]]] = {name: [] for name in DEFAULT_CATEGORY_NAMES}
    for card in visible_cards:
        category_name = DEFAULT_CARD_CATEGORY_BY_KEY.get(str(card['key']), 'Uncategorized')
        by_name.setdefault(category_name, [])
        by_name[category_name].append(card)
    sections: list[dict[str, Any]] = []
    for category_name in DEFAULT_CATEGORY_NAMES:
        section_cards = sorted(by_name.get(category_name, []), key=lambda item: str(item['label']).lower())
        if section_cards:
            sections.append({'name': category_name, 'cards': section_cards})
    return sections


def _ensure_default_categories(db: Session) -> None:
    existing_count = db.execute(select(DashboardCategory.id).limit(1)).scalar_one_or_none()
    if existing_count is not None:
        return
    for idx, name in enumerate(DEFAULT_CATEGORY_NAMES):
        db.add(
            DashboardCategory(
                name=name,
                position=(idx + 1) * 10,
                active=True,
            )
        )
    db.flush()


def build_dashboard_sections(db: Session, *, is_admin: bool) -> list[dict[str, Any]]:
    cards = [card for card in dashboard_card_catalog() if is_admin or not bool(card['requires_admin'])]
    if not cards:
        return []
    try:
        _ensure_default_categories(db)
        categories = db.execute(
            select(DashboardCategory)
            .where(DashboardCategory.active.is_(True))
            .order_by(DashboardCategory.position.asc(), DashboardCategory.name.asc(), DashboardCategory.id.asc())
        ).scalars().all()
        assignments = db.execute(select(DashboardCardAssignment)).scalars().all()
    except SQLAlchemyError:
        return _default_sections(cards)

    category_rows = [{'id': int(category.id), 'name': str(category.name)} for category in categories]
    category_id_by_name = {str(row['name']).strip().lower(): int(row['id']) for row in category_rows}
    category_ids = {int(category['id']) for category in category_rows}
    assignments_by_key = {str(row.card_key): row for row in assignments}

    cards_by_category: dict[int | None, list[tuple[int, dict[str, Any]]]] = {}
    for card in cards:
        key = str(card['key'])
        assignment = assignments_by_key.get(key)
        assigned_category_id = int(assignment.category_id) if assignment and assignment.category_id is not None else None
        position = int(assignment.position) if assignment else 9999
        if assigned_category_id is None:
            default_category_name = DEFAULT_CARD_CATEGORY_BY_KEY.get(key)
            if default_category_name:
                assigned_category_id = category_id_by_name.get(default_category_name.lower())
        if assigned_category_id not in category_ids:
            assigned_category_id = None
        cards_by_category.setdefault(assigned_category_id, []).append((position, card))

    sections: list[dict[str, Any]] = []
    for category in category_rows:
        category_id = int(category['id'])
        raw_cards = cards_by_category.get(category_id, [])
        ordered_cards = [card for _, card in sorted(raw_cards, key=lambda item: (item[0], str(item[1]['label']).lower()))]
        if ordered_cards:
            sections.append({'name': str(category['name']), 'cards': ordered_cards})

    uncategorized = cards_by_category.get(None, [])
    if uncategorized:
        ordered_cards = [card for _, card in sorted(uncategorized, key=lambda item: (item[0], str(item[1]['label']).lower()))]
        sections.append({'name': 'Uncategorized', 'cards': ordered_cards})
    return sections


def list_dashboard_layout_settings(db: Session) -> dict[str, Any]:
    cards = dashboard_card_catalog()
    _ensure_default_categories(db)
    categories = db.execute(select(DashboardCategory).order_by(DashboardCategory.position.asc(), DashboardCategory.name.asc())).scalars().all()
    assignments = db.execute(select(DashboardCardAssignment)).scalars().all()
    assignment_by_key = {str(row.card_key): row for row in assignments}
    category_rows = [
        {
            'id': int(category.id),
            'name': str(category.name),
            'position': int(category.position),
            'active': bool(category.active),
        }
        for category in categories
    ]
    active_by_id = {row['id']: row for row in category_rows if row['active']}
    active_id_by_name = {str(row['name']).strip().lower(): int(row['id']) for row in category_rows if row['active']}
    cards_out: list[dict[str, Any]] = []
    for card in cards:
        assignment = assignment_by_key.get(str(card['key']))
        category_id = int(assignment.category_id) if assignment and assignment.category_id is not None else None
        if category_id is None:
            default_category_name = DEFAULT_CARD_CATEGORY_BY_KEY.get(str(card['key']))
            if default_category_name:
                category_id = active_id_by_name.get(default_category_name.lower())
        if category_id is not None and category_id not in active_by_id:
            category_id = None
        cards_out.append(
            {
                **card,
                'category_id': category_id,
                'position': int(assignment.position) if assignment else 9999,
            }
        )
    cards_out.sort(key=lambda row: (str(row.get('label', '')).lower(), str(row.get('key', ''))))
    return {
        'categories': category_rows,
        'cards': cards_out,
    }


def create_dashboard_category(db: Session, *, name: str) -> DashboardCategory:
    clean_name = str(name or '').strip()
    if not clean_name:
        raise ValueError('Category name is required')
    if clean_name.lower() == 'uncategorized':
        raise ValueError('Uncategorized is reserved')
    existing = db.execute(select(DashboardCategory).where(DashboardCategory.name == clean_name)).scalar_one_or_none()
    if existing is not None:
        raise ValueError('Category already exists')
    max_position = db.execute(select(DashboardCategory.position).order_by(DashboardCategory.position.desc()).limit(1)).scalar_one_or_none()
    category = DashboardCategory(
        name=clean_name,
        position=(int(max_position) + 10) if max_position is not None else 10,
        active=True,
    )
    db.add(category)
    db.flush()
    return category


def save_dashboard_categories(db: Session, *, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    categories = db.execute(select(DashboardCategory)).scalars().all()
    by_id = {int(row.id): row for row in categories}
    for row in rows:
        category_id = int(row['id'])
        category = by_id.get(category_id)
        if category is None:
            continue
        name = str(row.get('name') or '').strip()
        if not name:
            raise ValueError(f'Category name is required for #{category_id}')
        if name.lower() == 'uncategorized':
            raise ValueError('Uncategorized is reserved')
        category.name = name
        category.position = int(row.get('position') or 0)
        category.active = bool(row.get('active'))
    db.flush()


def save_dashboard_card_assignments(
    db: Session,
    *,
    assignments: dict[str, int | None],
    positions: dict[str, int],
    principal_id: int,
) -> None:
    if not assignments:
        return
    valid_keys = {str(card['key']) for card in dashboard_card_catalog()}
    categories = db.execute(select(DashboardCategory.id, DashboardCategory.active)).all()
    valid_category_ids = {int(row.id) for row in categories if row.active}
    existing_rows = db.execute(select(DashboardCardAssignment)).scalars().all()
    existing_by_key = {str(row.card_key): row for row in existing_rows}
    for key, category_id in assignments.items():
        if key not in valid_keys:
            continue
        if category_id is not None and category_id not in valid_category_ids:
            category_id = None
        row = existing_by_key.get(key)
        if row is None:
            row = DashboardCardAssignment(
                card_key=key,
                category_id=category_id,
                position=int(positions.get(key, 9999)),
                updated_by_principal_id=principal_id,
            )
            db.add(row)
            continue
        row.category_id = category_id
        row.position = int(positions.get(key, 9999))
        row.updated_by_principal_id = principal_id
    db.flush()
