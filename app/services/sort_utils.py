from __future__ import annotations

import re
from decimal import Decimal

_MG_PATTERN = re.compile(r'(\d+(?:\.\d+)?)\s*mg\b', re.IGNORECASE)


def normalize_sort_text(value: str | None) -> str:
    return (value or '').strip().lower()


def extract_mg_value(value: str | None) -> Decimal | None:
    match = _MG_PATTERN.search(value or '')
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except Exception:
        return None


def variation_sort_key(variation_name: str | None) -> tuple[int, Decimal, str]:
    normalized = normalize_sort_text(variation_name)
    mg = extract_mg_value(variation_name)
    if mg is None:
        return (1, Decimal('0'), normalized)
    return (0, mg, normalized)


def item_variation_sort_key(*, item_name: str | None, variation_name: str | None) -> tuple[str, int, Decimal, str]:
    return (normalize_sort_text(item_name), *variation_sort_key(variation_name))
