from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    TouchscreenFlavor,
    TouchscreenFlavorCategoryLink,
    TouchscreenFlavorRecommendation,
    TouchscreenFlavorSkuLink,
    TouchscreenFlavorStoreOverride,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def slugify(value: str) -> str:
    clean = re.sub(r'[^a-z0-9]+', '-', str(value or '').strip().lower()).strip('-')
    return clean[:120]


def save_flavor(
    db: Session, *, flavor_id: int | None, principal: Principal, ip: str | None,
    brand_name: str, display_name: str, short_description: str, long_description: str,
    display_order: int, is_active: bool, is_touchscreen_visible: bool,
    category_ids: set[int], mappings: list[dict], recommendation_ids: list[int],
    store_overrides: list[dict], publish_requested: bool | None = None,
) -> TouchscreenFlavor:
    brand = str(brand_name or '').strip()
    name = str(display_name or '').strip()
    short = str(short_description or '').strip()
    if not brand or not name or not short:
        raise ValueError('Brand, flavor name, and short description are required.')
    flavor = db.get(TouchscreenFlavor, flavor_id) if flavor_id else None
    created = flavor is None
    if flavor is None:
        base_slug = slugify(f'{brand}-{name}')
        if not base_slug:
            raise ValueError('Enter a valid flavor name.')
        slug = base_slug
        suffix = 2
        while db.scalar(select(TouchscreenFlavor.id).where(TouchscreenFlavor.slug == slug)) is not None:
            slug = f'{base_slug}-{suffix}'; suffix += 1
        flavor = TouchscreenFlavor(
            brand_name=brand, display_name=name, slug=slug, short_description=short,
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        )
        db.add(flavor); db.flush()
    before = {'brand_name': flavor.brand_name, 'display_name': flavor.display_name, 'is_published': flavor.is_published}
    old_mapping_rows = db.execute(select(TouchscreenFlavorSkuLink).where(
        TouchscreenFlavorSkuLink.touchscreen_flavor_id == flavor.id
    )).scalars().all()
    old_mappings = {(row.square_variation_id, row.format, row.cooling_type) for row in old_mapping_rows}
    old_recommendations = set(db.execute(select(TouchscreenFlavorRecommendation.recommended_flavor_id).where(
        TouchscreenFlavorRecommendation.source_flavor_id == flavor.id
    )).scalars())
    old_override_rows = db.execute(select(TouchscreenFlavorStoreOverride).where(
        TouchscreenFlavorStoreOverride.touchscreen_flavor_id == flavor.id
    )).scalars().all()
    old_overrides = {
        row.store_id: (row.is_hidden, row.inventory_display_threshold, row.reason or '') for row in old_override_rows
    }
    flavor.brand_name = brand
    flavor.display_name = name
    flavor.short_description = short
    flavor.long_description = str(long_description or '').strip() or None
    flavor.display_order = int(display_order)
    flavor.is_active = bool(is_active)
    flavor.is_touchscreen_visible = bool(is_touchscreen_visible)
    flavor.updated_by_principal_id = principal.id
    flavor.updated_at = _now()
    if publish_requested is not None:
        flavor.is_published = bool(publish_requested)

    db.execute(delete(TouchscreenFlavorCategoryLink).where(TouchscreenFlavorCategoryLink.touchscreen_flavor_id == flavor.id))
    db.add_all([TouchscreenFlavorCategoryLink(touchscreen_flavor_id=flavor.id, category_id=category_id) for category_id in sorted(category_ids)])

    db.execute(delete(TouchscreenFlavorSkuLink).where(TouchscreenFlavorSkuLink.touchscreen_flavor_id == flavor.id))
    seen_variations: set[str] = set()
    new_mappings: set[tuple[str, str, str]] = set()
    for row in mappings:
        variation_id = str(row.get('square_variation_id') or '').strip()
        item_format = str(row.get('format') or '').upper()
        cooling = str(row.get('cooling_type') or 'UNKNOWN').upper()
        if not variation_id or variation_id in seen_variations:
            continue
        if item_format not in {'SALT', 'FREEBASE'} or cooling not in {'ICED', 'NON_ICED', 'UNKNOWN'}:
            raise ValueError('Every mapped variation needs a valid format and cooling classification.')
        seen_variations.add(variation_id)
        new_mappings.add((variation_id, item_format, cooling))
        db.add(TouchscreenFlavorSkuLink(
            touchscreen_flavor_id=flavor.id, square_variation_id=variation_id, format=item_format,
            cooling_type=cooling, is_active=True, created_by_principal_id=principal.id,
        ))

    db.execute(delete(TouchscreenFlavorRecommendation).where(TouchscreenFlavorRecommendation.source_flavor_id == flavor.id))
    new_recommendations = {item_id for item_id in recommendation_ids if item_id != flavor.id}
    for order, recommendation_id in enumerate(dict.fromkeys(recommendation_ids)):
        if recommendation_id == flavor.id:
            continue
        db.add(TouchscreenFlavorRecommendation(
            source_flavor_id=flavor.id, recommended_flavor_id=recommendation_id, sort_order=order,
            relationship_type='SIMILAR', is_active=True, created_by_principal_id=principal.id,
        ))

    db.execute(delete(TouchscreenFlavorStoreOverride).where(TouchscreenFlavorStoreOverride.touchscreen_flavor_id == flavor.id))
    new_overrides: dict[int, tuple[bool, int | None, str]] = {}
    for row in store_overrides:
        threshold = row.get('inventory_display_threshold')
        if not row.get('is_hidden') and threshold in {None, ''}:
            continue
        clean_threshold = int(threshold) if threshold not in {None, ''} else None
        clean_reason = str(row.get('reason') or '').strip()
        new_overrides[int(row['store_id'])] = (bool(row.get('is_hidden')), clean_threshold, clean_reason)
        db.add(TouchscreenFlavorStoreOverride(
            store_id=int(row['store_id']), touchscreen_flavor_id=flavor.id, is_hidden=bool(row.get('is_hidden')),
            inventory_display_threshold=clean_threshold,
            reason=clean_reason or None, created_by_principal_id=principal.id,
        ))

    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='FLAVOR_CREATED' if created else 'FLAVOR_EDITED', domain='TOUCHSCREEN',
        entity_type='flavor', entity_id=flavor.id, before=None if created else before,
        after={'brand_name': brand, 'display_name': name, 'is_published': flavor.is_published,
               'mapping_count': len(seen_variations), 'category_count': len(category_ids)},
    ), ip=ip)
    if old_mappings != new_mappings:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='MAPPINGS_CHANGED', domain='TOUCHSCREEN',
            entity_type='flavor', entity_id=flavor.id,
            before={'mappings': sorted(old_mappings)}, after={'mappings': sorted(new_mappings)},
        ), ip=ip)
    if old_recommendations != new_recommendations:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='RECOMMENDATIONS_CHANGED', domain='TOUCHSCREEN',
            entity_type='flavor', entity_id=flavor.id,
            before={'recommended_flavor_ids': sorted(old_recommendations)},
            after={'recommended_flavor_ids': sorted(new_recommendations)},
        ), ip=ip)
    if old_overrides != new_overrides:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='STORE_OVERRIDES_CHANGED', domain='TOUCHSCREEN',
            entity_type='flavor', entity_id=flavor.id,
            store_ids=tuple(sorted(set(old_overrides) | set(new_overrides))),
            before={'overrides': old_overrides}, after={'overrides': new_overrides},
        ), ip=ip)
    return flavor


def set_flavor_published(
    db: Session, *, flavor_id: int, published: bool, principal: Principal, ip: str | None
) -> TouchscreenFlavor:
    flavor = db.get(TouchscreenFlavor, flavor_id)
    if flavor is None or flavor.deleted_at is not None:
        raise ValueError('Flavor was not found.')
    if published:
        mapping_count = db.scalar(select(TouchscreenFlavorSkuLink.id).where(
            TouchscreenFlavorSkuLink.touchscreen_flavor_id == flavor.id,
            TouchscreenFlavorSkuLink.is_active.is_(True),
        ).limit(1))
        if mapping_count is None:
            raise ValueError('Add at least one Square variation mapping before publishing.')
    before = flavor.is_published
    flavor.is_published = published
    flavor.updated_by_principal_id = principal.id
    flavor.updated_at = _now()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='FLAVOR_PUBLISHED' if published else 'FLAVOR_UNPUBLISHED',
        domain='TOUCHSCREEN', entity_type='flavor', entity_id=flavor.id,
        before={'is_published': before}, after={'is_published': published},
    ), ip=ip)
    return flavor
