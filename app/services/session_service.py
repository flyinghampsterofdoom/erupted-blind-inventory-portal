from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, and_, delete, select, update
from sqlalchemy.orm import Session

from app.auth import Principal, Role, assert_store_scope
from app.models import (
    AuditLog,
    Campaign,
    CountGroup,
    CountGroupCampaign,
    CountSession,
    Entry,
    Principal as PrincipalModel,
    PrincipalRole,
    SessionStatus,
    SnapshotLine,
    SnapshotSectionType,
    Store,
    StoreForcedCount,
    StoreRecountItem,
    StoreRecountState,
    StoreRotationState,
)
from app.security.passwords import hash_password, verify_password
from app.services.snapshot_provider import SnapshotProvider


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def get_active_campaigns(db: Session) -> list[Campaign]:
    campaigns = db.execute(select(Campaign).where(Campaign.active.is_(True)).order_by(Campaign.id.asc())).scalars().all()
    if not campaigns:
        raise ValueError('No active campaigns configured')
    return campaigns


def _active_groups(db: Session) -> list[CountGroup]:
    return db.execute(
        select(CountGroup)
        .join(CountGroupCampaign, CountGroupCampaign.group_id == CountGroup.id)
        .join(Campaign, Campaign.id == CountGroupCampaign.campaign_id)
        .where(
            CountGroup.active.is_(True),
            Campaign.active.is_(True),
        )
        .distinct()
        .order_by(CountGroup.position.asc(), CountGroup.id.asc())
    ).scalars().all()


def _next_group_id(group_ids: list[int], current_id: int) -> int:
    idx = group_ids.index(current_id)
    return group_ids[(idx + 1) % len(group_ids)]


def _resolve_group_for_store(db: Session, *, store_id: int) -> tuple[int, int | None]:
    forced = db.execute(
        select(StoreForcedCount)
        .where(
            StoreForcedCount.store_id == store_id,
            StoreForcedCount.active.is_(True),
            StoreForcedCount.consumed_at.is_(None),
        )
        .order_by(StoreForcedCount.created_at.asc())
    ).scalars().first()

    if forced:
        forced.active = False
        forced.consumed_at = _now()
        if forced.count_group_id:
            return forced.count_group_id, forced.id
        if forced.campaign_id:
            mapped_group_id = db.execute(
                select(CountGroupCampaign.group_id)
                .join(CountGroup, CountGroup.id == CountGroupCampaign.group_id)
                .where(
                    CountGroupCampaign.campaign_id == forced.campaign_id,
                    CountGroup.active.is_(True),
                )
            ).scalar_one_or_none()
            if mapped_group_id:
                return mapped_group_id, forced.id
            raise ValueError('Forced campaign is not assigned to any active group')

    groups = _active_groups(db)
    if not groups:
        raise ValueError('No count groups configured. Add groups under Manager > Groups.')

    group_ids = [g.id for g in groups]
    rotation = db.execute(select(StoreRotationState).where(StoreRotationState.store_id == store_id)).scalar_one_or_none()
    if not rotation:
        selected = group_ids[0]
        db.add(StoreRotationState(store_id=store_id, next_group_id=_next_group_id(group_ids, selected)))
        return selected, None

    selected = rotation.next_group_id if rotation.next_group_id in group_ids else group_ids[0]
    rotation.next_group_id = _next_group_id(group_ids, selected)
    rotation.updated_at = _now()
    return selected, None


def _campaigns_for_group(db: Session, *, group_id: int) -> list[Campaign]:
    return db.execute(
        select(Campaign)
        .join(CountGroupCampaign, CountGroupCampaign.campaign_id == Campaign.id)
        .where(CountGroupCampaign.group_id == group_id, Campaign.active.is_(True))
        .order_by(Campaign.id.asc())
    ).scalars().all()


def _get_recount_items(db: Session, *, store_id: int) -> list[StoreRecountItem]:
    state = db.execute(select(StoreRecountState).where(StoreRecountState.store_id == store_id)).scalar_one_or_none()
    if not state or not state.is_active:
        return []
    return db.execute(
        select(StoreRecountItem)
        .where(StoreRecountItem.store_id == store_id)
        .order_by(StoreRecountItem.item_name.asc(), StoreRecountItem.variation_name.asc())
    ).scalars().all()


def create_count_session(
    db: Session,
    *,
    principal: Principal,
    employee_name: str,
    snapshot_provider: SnapshotProvider,
) -> CountSession:
    if principal.role != Role.STORE or principal.store_id is None:
        raise PermissionError('Only store principals can create count sessions')

    group_id, forced_id = _resolve_group_for_store(db, store_id=principal.store_id)
    campaigns = _campaigns_for_group(db, group_id=group_id)
    if not campaigns:
        raise ValueError('Selected count group has no active campaigns')

    deduped_items: dict[str, dict] = {}
    for campaign in campaigns:
        for item in snapshot_provider.list_count_items(store_id=principal.store_id, campaign_id=campaign.id):
            deduped_items.setdefault(
                item.variation_id,
                {
                    'variation_id': item.variation_id,
                    'sku': item.sku,
                    'item_name': item.item_name,
                    'variation_name': item.variation_name,
                    'source_catalog_version': item.source_catalog_version,
                },
            )

    recount_items = _get_recount_items(db, store_id=principal.store_id)
    recount_ids = {item.variation_id for item in recount_items}
    category_items = [item for vid, item in deduped_items.items() if vid not in recount_ids]

    count_session = CountSession(
        store_id=principal.store_id,
        campaign_id=campaigns[0].id,
        count_group_id=group_id,
        employee_name=employee_name,
        created_by_principal_id=principal.id,
        status=SessionStatus.DRAFT,
        source_forced_count_id=forced_id,
        includes_recount=bool(recount_items),
    )
    db.add(count_session)
    db.flush()

    db.add_all(
        [
            SnapshotLine(
                session_id=count_session.id,
                variation_id=item['variation_id'],
                sku=item['sku'],
                item_name=item['item_name'],
                variation_name=item['variation_name'],
                section_type=SnapshotSectionType.CATEGORY,
                expected_on_hand=Decimal('0'),
                source_catalog_version=item['source_catalog_version'],
            )
            for item in category_items
        ]
    )

    db.add_all(
        [
            SnapshotLine(
                session_id=count_session.id,
                variation_id=item.variation_id,
                sku=item.sku,
                item_name=item.item_name,
                variation_name=item.variation_name,
                section_type=SnapshotSectionType.RECOUNT,
                expected_on_hand=Decimal('0'),
                source_catalog_version='recount-queue',
            )
            for item in recount_items
        ]
    )

    return count_session


def get_session_for_principal(db: Session, *, session_id: int, principal: Principal) -> CountSession:
    count_session = db.execute(select(CountSession).where(CountSession.id == session_id)).scalar_one_or_none()
    if not count_session:
        raise ValueError('Session not found')
    assert_store_scope(principal, count_session.store_id)
    return count_session


def _editable_session_guard(count_session: CountSession) -> None:
    if count_session.status != SessionStatus.DRAFT:
        raise ValueError('Session is locked and cannot be edited')


def save_draft_entries(
    db: Session,
    *,
    principal: Principal,
    session_id: int,
    quantities_by_variation: dict[str, Decimal],
) -> CountSession:
    count_session = get_session_for_principal(db, session_id=session_id, principal=principal)
    _editable_session_guard(count_session)

    valid_variations = {
        row[0]
        for row in db.execute(select(SnapshotLine.variation_id).where(SnapshotLine.session_id == session_id)).all()
    }

    for variation_id, qty in quantities_by_variation.items():
        if variation_id not in valid_variations:
            continue
        existing = db.execute(
            select(Entry).where(and_(Entry.session_id == session_id, Entry.variation_id == variation_id))
        ).scalar_one_or_none()
        if existing:
            existing.counted_qty = qty
            existing.updated_by_principal_id = principal.id
            existing.updated_at = _now()
        else:
            db.add(
                Entry(
                    session_id=session_id,
                    variation_id=variation_id,
                    counted_qty=qty,
                    updated_by_principal_id=principal.id,
                    updated_at=_now(),
                )
            )

    return count_session


def _variance_signature(non_zero_rows: list[dict]) -> str:
    normalized = [f"{row['variation_id']}|{Decimal(row['variance']).quantize(Decimal('0.001'))}" for row in non_zero_rows]
    normalized.sort()
    return hashlib.sha256(';'.join(normalized).encode('utf-8')).hexdigest()


def _replace_recount_items(db: Session, *, store_id: int, rows: list[dict]) -> None:
    db.execute(delete(StoreRecountItem).where(StoreRecountItem.store_id == store_id))
    db.add_all(
        [
            StoreRecountItem(
                store_id=store_id,
                variation_id=row['variation_id'],
                sku=row['sku'],
                item_name=row['item_name'],
                variation_name=row['variation_name'],
                last_variance=row['variance'],
                updated_at=_now(),
            )
            for row in rows
        ]
    )


def _apply_recount_state(db: Session, *, store_id: int, non_zero_rows: list[dict]) -> dict:
    state = db.execute(select(StoreRecountState).where(StoreRecountState.store_id == store_id)).scalar_one_or_none()
    if not state:
        state = StoreRecountState(store_id=store_id, is_active=False, rounds=0)
        db.add(state)
        db.flush()

    if not non_zero_rows:
        state.is_active = False
        state.previous_signature = None
        state.rounds = 0
        state.updated_at = _now()
        db.execute(delete(StoreRecountItem).where(StoreRecountItem.store_id == store_id))
        return {'stable': False, 'signature': None, 'rounds': 0, 'square_stub': False}

    signature = _variance_signature(non_zero_rows)
    if not state.is_active:
        state.is_active = True
        state.previous_signature = signature
        state.rounds = 1
        state.updated_at = _now()
        _replace_recount_items(db, store_id=store_id, rows=non_zero_rows)
        return {'stable': False, 'signature': signature, 'rounds': state.rounds, 'square_stub': False}

    if state.previous_signature == signature:
        state.is_active = False
        state.previous_signature = signature
        state.rounds = state.rounds + 1
        state.updated_at = _now()
        db.execute(delete(StoreRecountItem).where(StoreRecountItem.store_id == store_id))
        return {'stable': True, 'signature': signature, 'rounds': state.rounds, 'square_stub': True}

    state.previous_signature = signature
    state.rounds = state.rounds + 1
    state.updated_at = _now()
    _replace_recount_items(db, store_id=store_id, rows=non_zero_rows)
    return {'stable': False, 'signature': signature, 'rounds': state.rounds, 'square_stub': False}


def submit_session(
    db: Session,
    *,
    principal: Principal,
    session_id: int,
    quantities_by_variation: dict[str, Decimal],
    snapshot_provider: SnapshotProvider,
) -> tuple[CountSession, list[dict], dict]:
    count_session = get_session_for_principal(db, session_id=session_id, principal=principal)
    _editable_session_guard(count_session)

    save_draft_entries(
        db,
        principal=principal,
        session_id=session_id,
        quantities_by_variation=quantities_by_variation,
    )
    db.flush()

    lines = db.execute(select(SnapshotLine).where(SnapshotLine.session_id == session_id)).scalars().all()
    variation_ids = [line.variation_id for line in lines]
    on_hand_by_variation = snapshot_provider.fetch_current_on_hand(
        store_id=count_session.store_id,
        variation_ids=variation_ids,
    )

    for line in lines:
        line.expected_on_hand = on_hand_by_variation.get(line.variation_id, Decimal('0'))

    count_session.submit_inventory_fetched_at = _now()
    db.flush()

    variance_rows = get_management_variance_lines(db, session_id=session_id)
    non_zero_rows = [row for row in variance_rows if row['variance'] != 0]
    recount_result = _apply_recount_state(db, store_id=count_session.store_id, non_zero_rows=non_zero_rows)

    if recount_result['signature']:
        count_session.variance_signature = recount_result['signature']
    count_session.stable_variance = bool(recount_result['stable'])

    count_session.status = SessionStatus.SUBMITTED
    count_session.submitted_at = _now()
    count_session.submitted_by_principal_id = principal.id
    count_session.updated_at = _now()

    return count_session, variance_rows, recount_result


def unlock_session(db: Session, *, principal: Principal, session_id: int) -> CountSession:
    if principal.role != Role.MANAGER:
        raise PermissionError('Only managers can unlock sessions')

    count_session = db.execute(select(CountSession).where(CountSession.id == session_id)).scalar_one_or_none()
    if not count_session:
        raise ValueError('Session not found')
    count_session.status = SessionStatus.DRAFT
    count_session.updated_at = _now()
    return count_session


def create_forced_count(
    db: Session,
    *,
    manager_principal_id: int,
    store_id: int,
    group_id: int | None,
    campaign_id: int | None,
    reason: str,
    source_session_id: int | None = None,
) -> StoreForcedCount:
    if not group_id and not campaign_id:
        raise ValueError('Provide group_id or campaign_id')

    if group_id:
        group_exists = db.execute(
            select(CountGroup.id).where(CountGroup.id == group_id, CountGroup.active.is_(True))
        ).scalar_one_or_none()
        if not group_exists:
            raise ValueError('Selected count group is not active')

    if campaign_id:
        campaign_exists = db.execute(
            select(Campaign.id).where(Campaign.id == campaign_id, Campaign.active.is_(True))
        ).scalar_one_or_none()
        if not campaign_exists:
            raise ValueError('Selected campaign is not active')

    forced = StoreForcedCount(
        store_id=store_id,
        count_group_id=group_id,
        campaign_id=campaign_id,
        source_session_id=source_session_id,
        reason=reason,
        created_by_principal_id=manager_principal_id,
        active=True,
    )
    db.add(forced)
    db.flush()
    return forced


def create_count_group(db: Session, *, name: str, campaign_ids: list[int]) -> CountGroup:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError('Group name is required')
    if not campaign_ids:
        raise ValueError('Select at least one campaign')

    existing = db.execute(select(CountGroup).where(CountGroup.name == clean_name)).scalar_one_or_none()
    if existing:
        raise ValueError('Group name already exists')

    max_position = db.execute(select(CountGroup.position).order_by(CountGroup.position.desc())).scalars().first()
    group = CountGroup(name=clean_name, position=(max_position + 1 if max_position is not None else 0), active=True)
    db.add(group)
    db.flush()

    # Campaign should belong to one group only. Move selected campaigns from old groups into this one.
    db.execute(delete(CountGroupCampaign).where(CountGroupCampaign.campaign_id.in_(campaign_ids)))
    for campaign_id in campaign_ids:
        db.add(CountGroupCampaign(group_id=group.id, campaign_id=campaign_id))

    return group


def update_count_group(db: Session, *, group_id: int, name: str, campaign_ids: list[int]) -> CountGroup:
    group = db.execute(select(CountGroup).where(CountGroup.id == group_id, CountGroup.active.is_(True))).scalar_one_or_none()
    if not group:
        raise ValueError('Group not found')

    clean_name = name.strip()
    if not clean_name:
        raise ValueError('Group name is required')
    if not campaign_ids:
        raise ValueError('Select at least one campaign')

    conflict = db.execute(
        select(CountGroup).where(
            CountGroup.name == clean_name,
            CountGroup.id != group_id,
        )
    ).scalar_one_or_none()
    if conflict:
        raise ValueError('Group name already exists')

    group.name = clean_name

    # Keep one-group-per-campaign invariant by removing selected campaigns from all groups first.
    db.execute(delete(CountGroupCampaign).where(CountGroupCampaign.campaign_id.in_(campaign_ids)))
    db.execute(delete(CountGroupCampaign).where(CountGroupCampaign.group_id == group_id))
    for campaign_id in campaign_ids:
        db.add(CountGroupCampaign(group_id=group_id, campaign_id=campaign_id))
    db.flush()
    return group


def deactivate_count_group(db: Session, *, group_id: int) -> CountGroup:
    group = db.execute(select(CountGroup).where(CountGroup.id == group_id, CountGroup.active.is_(True))).scalar_one_or_none()
    if not group:
        raise ValueError('Group not found')

    group.active = False

    # Remove campaign mappings so the group is immediately excluded from future rotations.
    db.execute(delete(CountGroupCampaign).where(CountGroupCampaign.group_id == group_id))

    # Clear stale pointers that may still target this now-inactive group.
    for rotation in db.execute(
        select(StoreRotationState).where(StoreRotationState.next_group_id == group_id)
    ).scalars().all():
        rotation.next_group_id = None
        rotation.updated_at = _now()

    for forced in db.execute(
        select(StoreForcedCount).where(
            StoreForcedCount.count_group_id == group_id,
            StoreForcedCount.active.is_(True),
            StoreForcedCount.consumed_at.is_(None),
        )
    ).scalars().all():
        forced.active = False
        forced.consumed_at = _now()

    db.flush()
    return group


def list_count_groups(db: Session) -> list[dict]:
    groups = db.execute(
        select(CountGroup).where(CountGroup.active.is_(True)).order_by(CountGroup.position.asc(), CountGroup.id.asc())
    ).scalars().all()

    rows: list[dict] = []
    for group in groups:
        campaigns = db.execute(
            select(Campaign)
            .join(CountGroupCampaign, CountGroupCampaign.campaign_id == Campaign.id)
            .where(
                CountGroupCampaign.group_id == group.id,
                Campaign.active.is_(True),
            )
            .order_by(Campaign.id.asc())
        ).scalars().all()
        rows.append(
            {
                'group_id': group.id,
                'group_name': group.name,
                'position': group.position,
                'campaign_ids': [c.id for c in campaigns],
                'campaign_names': [c.category_filter or c.label for c in campaigns],
            }
        )
    return rows


def renumber_count_group_positions(db: Session) -> int:
    groups = db.execute(
        select(CountGroup).where(CountGroup.active.is_(True)).order_by(CountGroup.position.asc(), CountGroup.id.asc())
    ).scalars().all()
    changed = 0
    for idx, group in enumerate(groups):
        if group.position != idx:
            group.position = idx
            changed += 1
    db.flush()
    return changed


def group_management_data(db: Session) -> dict:
    groups = list_count_groups(db)
    active_campaigns = get_active_campaigns(db)
    mapped_campaign_ids = {
        campaign_id
        for campaign_id, in db.execute(select(CountGroupCampaign.campaign_id).distinct()).all()
    }
    ungrouped_campaigns = [c for c in active_campaigns if c.id not in mapped_campaign_ids]
    group_name_by_campaign_id = {
        campaign_id: group_name
        for campaign_id, group_name in db.execute(
            select(CountGroupCampaign.campaign_id, CountGroup.name)
            .join(CountGroup, CountGroup.id == CountGroupCampaign.group_id)
            .where(CountGroup.active.is_(True))
        ).all()
    }
    campaign_rows = [
        {
            'campaign_id': c.id,
            'campaign_label': c.category_filter or c.label,
            'group_name': group_name_by_campaign_id.get(c.id),
        }
        for c in active_campaigns
    ]
    return {
        'groups': groups,
        'ungrouped_campaigns': ungrouped_campaigns,
        'campaign_rows': campaign_rows,
        'all_campaigns': active_campaigns,
    }


def list_sessions_query() -> Select:
    return select(CountSession).order_by(CountSession.created_at.desc())


def get_store_session_lines(db: Session, *, session_id: int) -> list[dict]:
    rows = db.execute(
        select(
            SnapshotLine.variation_id,
            SnapshotLine.sku,
            SnapshotLine.item_name,
            SnapshotLine.variation_name,
            SnapshotLine.section_type,
            Entry.counted_qty,
        )
        .select_from(SnapshotLine)
        .outerjoin(
            Entry,
            and_(Entry.session_id == SnapshotLine.session_id, Entry.variation_id == SnapshotLine.variation_id),
        )
        .where(SnapshotLine.session_id == session_id)
        .order_by(SnapshotLine.section_type.asc(), SnapshotLine.item_name.asc(), SnapshotLine.variation_name.asc())
    ).all()

    return [
        {
            'variation_id': r.variation_id,
            'sku': r.sku,
            'item_name': r.item_name,
            'variation_name': r.variation_name,
            'section_type': r.section_type.value if hasattr(r.section_type, 'value') else str(r.section_type),
            'counted_qty': r.counted_qty,
        }
        for r in rows
    ]


def get_management_variance_lines(db: Session, *, session_id: int) -> list[dict]:
    rows = db.execute(
        select(
            SnapshotLine.variation_id,
            SnapshotLine.sku,
            SnapshotLine.item_name,
            SnapshotLine.variation_name,
            SnapshotLine.section_type,
            SnapshotLine.expected_on_hand,
            Entry.counted_qty,
        )
        .select_from(SnapshotLine)
        .outerjoin(
            Entry,
            and_(Entry.session_id == SnapshotLine.session_id, Entry.variation_id == SnapshotLine.variation_id),
        )
        .where(SnapshotLine.session_id == session_id)
        .order_by(SnapshotLine.section_type.asc(), SnapshotLine.item_name.asc(), SnapshotLine.variation_name.asc())
    ).all()

    line_items: list[dict] = []
    for row in rows:
        counted = row.counted_qty if row.counted_qty is not None else Decimal('0')
        variance = counted - row.expected_on_hand
        line_items.append(
            {
                'variation_id': row.variation_id,
                'sku': row.sku,
                'item_name': row.item_name,
                'variation_name': row.variation_name,
                'section_type': row.section_type.value if hasattr(row.section_type, 'value') else str(row.section_type),
                'expected_on_hand': row.expected_on_hand,
                'counted_qty': counted,
                'variance': variance,
            }
        )
    return line_items


def list_stores_with_rotation(db: Session) -> list[dict]:
    stores = db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name.asc())).scalars().all()
    groups = _active_groups(db)
    group_name_by_id = {g.id: g.name for g in groups}

    by_store: list[dict] = []
    for store in stores:
        rotation = db.execute(select(StoreRotationState).where(StoreRotationState.store_id == store.id)).scalar_one_or_none()
        forced = db.execute(
            select(StoreForcedCount)
            .where(
                StoreForcedCount.store_id == store.id,
                StoreForcedCount.active.is_(True),
                StoreForcedCount.consumed_at.is_(None),
            )
            .order_by(StoreForcedCount.created_at.asc())
        ).scalars().first()

        by_store.append(
            {
                'store_id': store.id,
                'store_name': store.name,
                'next_group_id': rotation.next_group_id if rotation else None,
                'next_group_name': group_name_by_id.get(rotation.next_group_id) if rotation else None,
                'forced_group_id': forced.count_group_id if forced else None,
                'forced_group_name': group_name_by_id.get(forced.count_group_id) if forced else None,
                'forced_reason': forced.reason if forced else None,
                'groups': groups,
            }
        )

    return by_store


def set_store_next_group(db: Session, *, store_id: int, group_id: int) -> StoreRotationState:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')

    valid_group = db.execute(
        select(CountGroup.id)
        .join(CountGroupCampaign, CountGroupCampaign.group_id == CountGroup.id)
        .join(Campaign, Campaign.id == CountGroupCampaign.campaign_id)
        .where(
            CountGroup.id == group_id,
            CountGroup.active.is_(True),
            Campaign.active.is_(True),
        )
        .distinct()
    ).scalar_one_or_none()
    if not valid_group:
        raise ValueError('Selected group is not available for rotation')

    rotation = db.execute(select(StoreRotationState).where(StoreRotationState.store_id == store_id)).scalar_one_or_none()
    if not rotation:
        rotation = StoreRotationState(store_id=store_id, next_group_id=group_id)
        db.add(rotation)
    else:
        rotation.next_group_id = group_id
        rotation.updated_at = _now()

    # Remove pending force overrides so the new rotation pointer is what generates next.
    for forced in db.execute(
        select(StoreForcedCount).where(
            StoreForcedCount.store_id == store_id,
            StoreForcedCount.active.is_(True),
            StoreForcedCount.consumed_at.is_(None),
        )
    ).scalars().all():
        forced.active = False
        forced.consumed_at = _now()

    db.flush()
    return rotation


def purge_count_sessions(db: Session, *, session_ids: list[int]) -> int:
    unique_ids = sorted({int(sid) for sid in session_ids if sid})
    if not unique_ids:
        return 0

    existing_ids = [
        sid
        for sid, in db.execute(
            select(CountSession.id).where(CountSession.id.in_(unique_ids))
        ).all()
    ]
    if not existing_ids:
        return 0

    db.execute(
        update(StoreForcedCount)
        .where(StoreForcedCount.source_session_id.in_(existing_ids))
        .values(source_session_id=None)
    )
    db.execute(
        update(AuditLog)
        .where(AuditLog.session_id.in_(existing_ids))
        .values(session_id=None)
    )
    db.execute(delete(Entry).where(Entry.session_id.in_(existing_ids)))
    db.execute(delete(SnapshotLine).where(SnapshotLine.session_id.in_(existing_ids)))
    db.execute(delete(CountSession).where(CountSession.id.in_(existing_ids)))
    db.flush()
    return len(existing_ids)


def list_store_login_rows(db: Session) -> list[dict]:
    stores = db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name.asc())).scalars().all()
    rows: list[dict] = []
    for store in stores:
        principal = db.execute(
            select(PrincipalModel)
            .where(
                PrincipalModel.role == PrincipalRole.STORE,
                PrincipalModel.store_id == store.id,
            )
            .order_by(PrincipalModel.active.desc(), PrincipalModel.id.asc())
        ).scalars().first()

        rows.append(
            {
                'store_id': store.id,
                'store_name': store.name,
                'principal_id': principal.id if principal else None,
                'username': principal.username if principal else '',
                'has_password': principal is not None,
                'active': principal.active if principal else False,
            }
        )
    return rows


def upsert_store_login_credentials(
    db: Session,
    *,
    store_id: int,
    username: str,
    new_password: str | None,
) -> tuple[PrincipalModel, bool]:
    clean_username = username.strip()
    if not clean_username:
        raise ValueError('User ID is required')

    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')

    existing_by_username = db.execute(
        select(PrincipalModel).where(PrincipalModel.username == clean_username)
    ).scalar_one_or_none()
    if existing_by_username and existing_by_username.store_id != store_id:
        raise ValueError('User ID is already in use by another account')

    principal = db.execute(
        select(PrincipalModel)
        .where(
            PrincipalModel.role == PrincipalRole.STORE,
            PrincipalModel.store_id == store_id,
        )
        .order_by(PrincipalModel.active.desc(), PrincipalModel.id.asc())
    ).scalars().first()

    created = False
    if not principal:
        if not new_password or not new_password.strip():
            raise ValueError('New store login requires a password')
        principal = PrincipalModel(
            username=clean_username,
            password_hash=hash_password(new_password.strip()),
            role=PrincipalRole.STORE,
            store_id=store_id,
            active=True,
        )
        db.add(principal)
        db.flush()
        created = True
        return principal, created

    if existing_by_username and existing_by_username.id != principal.id:
        raise ValueError('User ID is already in use by another account')

    principal.username = clean_username
    principal.active = True
    if new_password and new_password.strip():
        principal.password_hash = hash_password(new_password.strip())
    db.flush()
    return principal, created


def reset_manager_password(
    db: Session,
    *,
    manager_principal_id: int,
    current_password: str,
    new_password: str,
    confirm_password: str,
) -> PrincipalModel:
    principal = db.execute(
        select(PrincipalModel).where(
            PrincipalModel.id == manager_principal_id,
            PrincipalModel.role == PrincipalRole.MANAGER,
            PrincipalModel.active.is_(True),
        )
    ).scalar_one_or_none()
    if not principal:
        raise ValueError('Manager account not found')

    if not verify_password(current_password, principal.password_hash):
        raise ValueError('Current password is incorrect')

    if not new_password.strip():
        raise ValueError('New password is required')
    if new_password != confirm_password:
        raise ValueError('New password and confirmation do not match')

    principal.password_hash = hash_password(new_password)
    db.flush()
    return principal
