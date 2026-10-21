from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Campaign, CountGroup, CountGroupCampaign
from app.services.sort_utils import item_variation_sort_key
from app.sync_square_campaigns import SquareClient, fetch_catalog_items, fetch_categories


@dataclass(frozen=True)
class _CatalogVariation:
    variation_id: str
    item_name: str
    variation_name: str
    reporting_category_name: str

    @property
    def searchable_text(self) -> str:
        return f'{self.item_name} {self.reporting_category_name}'.lower().strip()

    @property
    def reporting_category_key(self) -> str:
        return self.reporting_category_name.strip().lower()


def _normalize_campaign_category(raw: str | None) -> str | None:
    category = raw.strip().lower() if raw else None
    if category and category.endswith(' rotation'):
        category = category.removesuffix(' rotation').strip()
    return category


def _matches_campaign(variation: _CatalogVariation, campaign_row: dict) -> bool:
    category_filter = campaign_row['category_filter']
    brand_filter = campaign_row['brand_filter']
    if category_filter and variation.reporting_category_key != category_filter:
        return False
    if brand_filter and brand_filter not in variation.searchable_text:
        return False
    return True


def _build_square_client() -> SquareClient:
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required to run count group coverage audit.')

    headers = {
        'Authorization': f'Bearer {settings.square_access_token}',
        'Content-Type': 'application/json',
    }
    if settings.square_api_version:
        headers['Square-Version'] = settings.square_api_version

    return SquareClient(
        base_url=settings.square_api_base_url.rstrip('/'),
        headers=headers,
        timeout_seconds=settings.square_timeout_seconds,
    )


def _load_catalog_variations() -> tuple[list[_CatalogVariation], int]:
    client = _build_square_client()
    categories_by_id = fetch_categories(client)
    items = fetch_catalog_items(client)

    by_variation_id: dict[str, _CatalogVariation] = {}
    for item in items:
        if item.get('is_deleted'):
            continue
        item_data = item.get('item_data', {}) or {}
        item_name = (item_data.get('name') or item.get('name') or '').strip()
        reporting_category_id = (item_data.get('reporting_category') or {}).get('id')
        reporting_category_name = (categories_by_id.get(reporting_category_id) or '').strip()
        for variation in (item_data.get('variations') or []):
            if variation.get('is_deleted'):
                continue
            variation_id = str(variation.get('id') or '').strip()
            if not variation_id or variation_id in by_variation_id:
                continue
            variation_data = variation.get('item_variation_data') or {}
            variation_name = str(variation_data.get('name') or 'Default').strip()
            by_variation_id[variation_id] = _CatalogVariation(
                variation_id=variation_id,
                item_name=item_name or variation_id,
                variation_name=variation_name or 'Default',
                reporting_category_name=reporting_category_name,
            )

    return sorted(
        by_variation_id.values(),
        key=lambda row: (
            *item_variation_sort_key(item_name=row.item_name, variation_name=row.variation_name),
            row.variation_id,
        ),
    ), len(items)


def run_count_group_coverage_audit(db: Session) -> dict:
    catalog_variations, catalog_item_count = _load_catalog_variations()

    campaign_rows = [
        {
            'campaign_id': row.id,
            'campaign_label': row.category_filter or row.label,
            'category_filter': _normalize_campaign_category(row.category_filter),
            'brand_filter': row.brand_filter.strip().lower() if row.brand_filter else None,
        }
        for row in db.execute(select(Campaign).where(Campaign.active.is_(True)).order_by(Campaign.id.asc())).scalars().all()
    ]

    group_rows = db.execute(
        select(
            CountGroup.id.label('group_id'),
            CountGroup.name.label('group_name'),
            CountGroup.position.label('group_position'),
            CountGroupCampaign.campaign_id.label('campaign_id'),
        )
        .select_from(CountGroupCampaign)
        .join(CountGroup, CountGroup.id == CountGroupCampaign.group_id)
        .where(CountGroup.active.is_(True))
        .order_by(CountGroup.position.asc(), CountGroup.id.asc())
    ).all()

    campaign_by_id = {row['campaign_id']: row for row in campaign_rows}
    grouped_campaigns: list[dict] = []
    for row in group_rows:
        campaign = campaign_by_id.get(row.campaign_id)
        if campaign is None:
            continue
        grouped_campaigns.append(
            {
                **campaign,
                'group_id': int(row.group_id),
                'group_name': str(row.group_name),
                'group_position': int(row.group_position),
            }
        )

    grouped_campaign_id_set = {row['campaign_id'] for row in grouped_campaigns}
    ungrouped_campaigns = [row for row in campaign_rows if row['campaign_id'] not in grouped_campaign_id_set]

    group_hit_variations: dict[int, set[str]] = defaultdict(set)
    category_totals: dict[str, int] = defaultdict(int)
    category_covered: dict[str, int] = defaultdict(int)
    uncovered_rows: list[dict] = []
    overlap_rows: list[dict] = []

    for variation in catalog_variations:
        category_name = variation.reporting_category_name or '(No Reporting Category)'
        category_totals[category_name] += 1

        matched_campaigns = [campaign for campaign in grouped_campaigns if _matches_campaign(variation, campaign)]

        if matched_campaigns:
            category_covered[category_name] += 1
            for campaign in matched_campaigns:
                group_hit_variations[campaign['group_id']].add(variation.variation_id)
        else:
            uncovered_rows.append(
                {
                    'variation_id': variation.variation_id,
                    'item_name': variation.item_name,
                    'variation_name': variation.variation_name,
                    'reporting_category_name': variation.reporting_category_name or '-',
                }
            )

        if len(matched_campaigns) > 1:
            overlap_rows.append(
                {
                    'variation_id': variation.variation_id,
                    'item_name': variation.item_name,
                    'variation_name': variation.variation_name,
                    'reporting_category_name': variation.reporting_category_name or '-',
                    'matches': [f"{c['group_position']} - {c['group_name']} / {c['campaign_label']}" for c in matched_campaigns],
                }
            )

    grouped_campaigns_by_group: dict[int, list[dict]] = defaultdict(list)
    for campaign in grouped_campaigns:
        grouped_campaigns_by_group[campaign['group_id']].append(campaign)

    summary_by_group: list[dict] = []
    seen_groups: set[int] = set()
    for row in group_rows:
        group_id = int(row.group_id)
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        campaigns = grouped_campaigns_by_group.get(group_id, [])
        summary_by_group.append(
            {
                'group_id': group_id,
                'group_name': str(row.group_name),
                'group_position': int(row.group_position),
                'campaign_count': len(campaigns),
                'coverage_count': len(group_hit_variations.get(group_id, set())),
                'campaign_labels': [c['campaign_label'] for c in campaigns],
            }
        )

    category_rows = [
        {
            'category_name': category_name,
            'total_count': total_count,
            'covered_count': category_covered.get(category_name, 0),
            'uncovered_count': total_count - category_covered.get(category_name, 0),
        }
        for category_name, total_count in sorted(category_totals.items(), key=lambda row: (-row[1], row[0].lower()))
    ]

    uncovered_limit = 200
    overlap_limit = 200
    uncovered_rows.sort(
        key=lambda row: (
            *item_variation_sort_key(item_name=row.get('item_name'), variation_name=row.get('variation_name')),
            str(row.get('variation_id') or ''),
        )
    )
    overlap_rows.sort(
        key=lambda row: (
            *item_variation_sort_key(item_name=row.get('item_name'), variation_name=row.get('variation_name')),
            str(row.get('variation_id') or ''),
        )
    )
    return {
        'summary': {
            'catalog_item_count': catalog_item_count,
            'variation_count': len(catalog_variations),
            'active_campaign_count': len(campaign_rows),
            'grouped_campaign_count': len(grouped_campaigns),
            'ungrouped_campaign_count': len(ungrouped_campaigns),
            'covered_variation_count': len(catalog_variations) - len(uncovered_rows),
            'uncovered_variation_count': len(uncovered_rows),
            'overlap_variation_count': len(overlap_rows),
            'missing_reporting_category_count': sum(
                1 for variation in catalog_variations if not variation.reporting_category_name
            ),
        },
        'group_rows': summary_by_group,
        'category_rows': category_rows,
        'ungrouped_campaign_rows': ungrouped_campaigns,
        'uncovered_rows': uncovered_rows[:uncovered_limit],
        'uncovered_remaining_count': max(0, len(uncovered_rows) - uncovered_limit),
        'overlap_rows': overlap_rows[:overlap_limit],
        'overlap_remaining_count': max(0, len(overlap_rows) - overlap_limit),
    }
