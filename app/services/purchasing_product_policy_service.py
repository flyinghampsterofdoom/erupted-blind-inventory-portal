"""Owner-defined merchandise/replenishment boundaries, separate from financing.

Verified 2026-10-26 against Square order and catalog evidence. Square represents
the loyalty marker as an ITEM, fully discounted, alongside the real discounted
merchandise. It is not a physical inventory product. Never exclude ordinary
merchandise merely because it is discounted or free.
"""
REWARD_VARIATIONS = frozenset({'323TZZ5L6DWHQBGXOQHEUKGD'})
KRATOM_CATEGORY_IDS = frozenset({'PKT4QIOH4GBOR52A62CTKQ4X'})
# The verified historical variant remains identifiable before catalog refresh.
KRATOM_HISTORICAL_VARIATIONS = frozenset({'MAURLGCJ6GNYBYKLDD42CAY5'})


def catalog_attributes(product, names=None):
    ids=set(getattr(product,'category_ids',()) or ())
    if getattr(product,'reporting_category_id',''):ids.add(product.reporting_category_id)
    return {'category_ids':sorted(ids),
            'category_names':{key:names[key] for key in ids if names and key in names},
            'product_type':getattr(product,'product_type','')}


def no_replenishment(variation_id, *, attributes=None, product=None, names=None):
    attributes=catalog_attributes(product,names) if product is not None else attributes or {}
    return (variation_id in KRATOM_HISTORICAL_VARIATIONS
            or bool(KRATOM_CATEGORY_IDS.intersection(attributes.get('category_ids',[])))
            or any(str(name).strip().casefold()=='kratom' for name in attributes.get('category_names',{}).values()))


def reward_marker(variation_id):
    return variation_id in REWARD_VARIATIONS


def excluded_replenishment_ids(db, variation_ids=None):
    """Local catalog evidence for legacy ordering; never changes saved orders."""
    from sqlalchemy import select
    from app.models import OrderingCatalogIdentity
    query=select(OrderingCatalogIdentity)
    if variation_ids is not None:query=query.where(OrderingCatalogIdentity.square_variation_id.in_(variation_ids))
    excluded=set(REWARD_VARIATIONS)|set(KRATOM_HISTORICAL_VARIATIONS)
    for identity in db.execute(query).scalars().all():
        if no_replenishment(identity.square_variation_id,attributes=identity.catalog_attributes):excluded.add(identity.square_variation_id)
    return excluded
