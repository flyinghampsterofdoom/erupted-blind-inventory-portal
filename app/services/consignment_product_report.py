"""Configured vendor products and period observations; never funded capacity."""

import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, select

from app.models import (
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    FundingReportFactLink,
    FundingReportLine,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    PurchaseOrderLine,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.v2_ordering_inventory_repository import FRESH, effective_freshness
from app.services.vendor_product_ownership import single_default_mapping

SEMANTICS = "CONFIGURED_VENDOR_PRODUCTS_V1"
ZERO = Decimal(0)


def sku_key(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def number(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def amount(value):
    return value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def product_matches(mapping, identity, text):
    if not text.strip():
        return True
    return (
        sku_key(text) == sku_key(mapping.sku)
        or text.strip().casefold()
        in " ".join(
            str(getattr(identity, key, "") or "")
            for key in ("product_name", "item_name", "variation_name")
        ).casefold()
    )


def _stored_product_labels(db, variations):
    """Presentation only: exact-ID snapshots never establish product membership."""
    labels = {}
    # Prefer the latest sale label, then return, then saved PO label. A PO is
    # optional evidence for display only; its funding, cost and vendor are unused.
    for model, variation, name, detail in (
        (ConsignmentSaleFact, ConsignmentSaleFact.square_variation_id,
         ConsignmentSaleFact.product_name_snapshot, ConsignmentSaleFact.variation_name_snapshot),
        (ConsignmentReturnFact, ConsignmentReturnFact.square_variation_id,
         ConsignmentReturnFact.product_name_snapshot, ConsignmentReturnFact.variation_name_snapshot),
        (PurchaseOrderLine, PurchaseOrderLine.variation_id,
         PurchaseOrderLine.item_name, PurchaseOrderLine.variation_name),
    ):
        missing = set(variations) - labels.keys()
        if not missing:
            break
        latest = select(
            variation.label("variation"), name.label("name"), detail.label("detail"),
            func.row_number().over(partition_by=variation, order_by=model.id.desc()).label("rank"),
        ).where(variation.in_(missing), func.trim(name) != "").subquery()
        for row in db.execute(select(latest).where(latest.c.rank == 1)):
            labels[row.variation] = SimpleNamespace(
                product_name=row.name, item_name=row.name, variation_name=row.detail,
            )
    return labels


def product_scope(db, *, vendor_id, product_filter=""):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or not vendor.active:
        raise ValueError("Choose an active vendor for this Consignment report.")
    catalog = list(
        db.scalars(
            select(OrderingCatalogIdentity).where(
                OrderingCatalogIdentity.square_is_deleted.is_(False)
            )
        ).all()
    )
    identities = {str(row.square_variation_id): row for row in catalog}
    by_sku = defaultdict(set)
    for row in catalog:
        if sku_key(row.sku):
            by_sku[sku_key(row.sku)].add(str(row.square_variation_id))
    mappings = list(
        db.scalars(
            select(VendorSkuConfig)
            .join(Vendor)
            .where(VendorSkuConfig.active.is_(True), Vendor.active.is_(True))
        ).all()
    )
    paths = defaultdict(list)
    unresolved = []
    for mapping in mappings:
        variation = str(mapping.square_variation_id or "").strip()
        matches = by_sku.get(sku_key(mapping.sku), set())
        if not variation and len(matches) == 1:
            variation = next(iter(matches))
        if variation:
            paths[variation].append(mapping)
        elif mapping.vendor_id == vendor_id:
            # We cannot use a missing catalog name to prove this configured
            # product is outside a text filter. Keep the problem vendor-scoped.
            names = [
                " — ".join(filter(None, (identities[v].product_name or identities[v].item_name,
                                         identities[v].variation_name)))
                for v in sorted(matches)
            ]
            label = "; ".join(filter(None, names)) or "Product name unavailable"
            reason = ("SKU matches multiple Square variations" if matches
                      else "SKU has no unique catalog match")
            unresolved.append(
                f"{label} — SKU {mapping.sku or 'unavailable'} — "
                f"Square Variation ID missing; {reason}"
            )
    if unresolved:
        raise ValueError(
            f"Cannot match {len(unresolved)} {vendor.name} product(s) to Square sales. "
            + ". ".join(unresolved)
            + ". Open Inventory → Vendor SKU Mappings, select " + vendor.name
            + ", and set the correct Square Variation ID for each listed SKU."
        )
    missing_labels = {
        v for v, rows in paths.items()
        if v not in identities and any(m.vendor_id == vendor_id for m in rows)
    }
    identities.update(_stored_product_labels(db, missing_labels))
    for variation in missing_labels - identities.keys():
        identities[variation] = SimpleNamespace(
            product_name="Product name unavailable", item_name=None, variation_name=None,
        )
    products = {}
    for variation, candidates in paths.items():
        selected = [m for m in candidates if m.vendor_id == vendor_id]
        if not selected:
            continue
        relevant = [
            m
            for m in selected
            if product_matches(m, identities[variation], product_filter)
        ]
        if not relevant:
            continue
        owner = single_default_mapping(candidates)
        if owner is None:
            raise ValueError(
                "Selected vendor ownership is ambiguous for product "
                + (identities[variation].product_name or identities[variation].item_name or "Product name unavailable")
                + f" — SKU {selected[0].sku} (Square variation {variation}). "
                "Open Inventory → Vendor SKU Mappings and resolve the conflicting default vendor mappings."
            )
        if owner.vendor_id == vendor_id:
            products[variation] = owner
    # Include configured SKU aliases in global ambiguity checks, even where the
    # other variation belongs to a different vendor.
    for variation, candidates in paths.items():
        for mapping in candidates:
            if sku_key(mapping.sku):
                by_sku[sku_key(mapping.sku)].add(variation)
    fallback = {
        sku: next(iter(variations))
        for sku, variations in by_sku.items()
        if len(variations) == 1 and next(iter(variations)) in products
    }
    ambiguous_skus = {
        sku
        for sku, variations in by_sku.items()
        if len(variations) > 1 and variations.intersection(products)
    }
    return products, identities, fallback, ambiguous_skus


def scope_signature(products, fallback, ambiguous_skus):
    return {
        "products": [
            {
                "variation": variation,
                "mapping": mapping.id,
                "vendor": mapping.vendor_id,
                "cost": str(number(mapping.unit_cost).normalize())
                if number(mapping.unit_cost) is not None
                else None,
            }
            for variation, mapping in sorted(products.items())
        ],
        "fallback": dict(sorted(fallback.items())),
        "ambiguous_skus": sorted(ambiguous_skus),
    }


def assert_scope_current(db, *, report):
    products, _identities, fallback, ambiguous = product_scope(
        db, vendor_id=report.vendor_id, product_filter=report.sku_filter or ""
    )
    saved = (
        (report.warning_summary or {})
        .get("purchase_order_scope", {})
        .get("scope_signature")
    )
    if saved != scope_signature(products, fallback, ambiguous):
        raise ValueError(
            "Configured vendor ownership, identity, or cost changed after calculation. Calculate a fresh report before financial finalization."
        )


def populate(db, *, report, account, store_ids, product_filter):
    products, identities, fallback, ambiguous_skus = product_scope(
        db, vendor_id=account.vendor_id, product_filter=product_filter
    )
    stores = list(db.scalars(select(Store.id).where(Store.active.is_(True))).all())
    if set(store_ids) - set(stores):
        raise ValueError("Choose valid active stores for this report.")
    selected_stores = sorted(set(store_ids or stores))
    groups = {}

    def group(variation, store):
        return groups.setdefault(
            (variation, store), {"sold": ZERO, "returned": ZERO, "links": []}
        )

    dialect = db.get_bind().dialect.name
    for model, quantity_field, is_return in (
        (ConsignmentSaleFact, "quantity_sold", False),
        (ConsignmentReturnFact, "quantity_returned", True),
    ):
        # Query by owned identity first. Ambiguous identity-less SKU candidates
        # are included only to surface a scoped error, never to guess ownership.
        normalized = (
            func.upper(
                func.regexp_replace(
                    func.coalesce(model.sku_snapshot, ""), r"\s+", "", "g"
                )
            )
            if dialect == "postgresql"
            else func.upper(
                func.replace(
                    func.replace(
                        func.replace(
                            func.replace(
                                func.coalesce(model.sku_snapshot, ""), " ", ""
                            ),
                            "\t",
                            "",
                        ),
                        "\n",
                        "",
                    ),
                    "\r",
                    "",
                )
            )
        )
        missing_id = or_(
            model.square_variation_id.is_(None),
            func.trim(model.square_variation_id) == "",
        )
        query = (
            select(model)
            .where(
                model.business_date >= report.sales_start_date,
                model.business_date <= report.sales_end_date,
                or_(
                    model.square_variation_id.in_(products),
                    and_(missing_id, normalized.in_(set(fallback) | ambiguous_skus)),
                ),
            )
            .order_by(model.id)
        )
        if store_ids:
            query = query.where(
                or_(model.store_id.in_(selected_stores), model.store_id.is_(None))
            )
        for fact in db.scalars(query).all():
            variation = str(fact.square_variation_id or "").strip()
            if not variation:
                sku = sku_key(fact.sku_snapshot)
                if sku in ambiguous_skus:
                    raise ValueError(
                        f"Selected vendor activity has ambiguous SKU {sku}: fact {fact.id}."
                    )
                variation = fallback.get(sku)
            if variation not in products:
                continue
            if fact.store_id is None and store_ids:
                raise ValueError(
                    f"Selected vendor activity has unknown store: fact {fact.id}."
                )
            quantity = number(getattr(fact, quantity_field))
            if quantity is None:
                raise ValueError(
                    f"Selected vendor {'return' if is_return else 'sale'} fact {fact.id} has unknown or invalid quantity."
                )
            row = group(variation, fact.store_id)
            row["returned" if is_return else "sold"] += quantity
            row["links"].append((fact, is_return, quantity))

    # Zero-activity rows and informational inventory use exactly the same product
    # and store universe as activity; absence of a snapshot remains UNKNOWN.
    for variation in products:
        for store in selected_stores or [None]:
            group(variation, store)
    inventory = {
        (row.square_variation_id, row.store_id): row
        for row in db.scalars(
            select(OrderingCurrentInventory).where(
                OrderingCurrentInventory.square_variation_id.in_(products),
                OrderingCurrentInventory.store_id.in_(selected_stores),
            )
        ).all()
    }
    now = datetime.now(timezone.utc)
    report.units_sold = report.units_returned = report.net_units = ZERO
    costs, quantities, values, snapshot_times, unknown_costs = [], [], [], [], []
    for (variation, store), row in sorted(
        groups.items(), key=lambda x: (x[0][0], x[0][1] or -1)
    ):
        mapping = products[variation]
        identity = identities[variation]
        cost = number(mapping.unit_cost)
        net = row["sold"] - row["returned"]
        total = amount(net * cost) if cost is not None else (ZERO if net == 0 else None)
        if total is None:
            unknown_costs.append(variation)
        observed = inventory.get((variation, store))
        quantity = None
        if (
            observed is not None
            and observed.freshness_state == FRESH
            and effective_freshness(observed.refreshed_at, now=now) == FRESH
        ):
            quantity = number(observed.counted_quantity)
            if quantity is not None:
                snapshot_times.append(observed.refreshed_at)
        value = (
            amount(quantity * cost)
            if quantity is not None and cost is not None
            else None
        )
        line = FundingReportLine(
            report_id=report.id,
            normalized_sku=sku_key(mapping.sku),
            sku_snapshot=mapping.sku or "",
            square_variation_id=variation,
            product_name_snapshot=identity.product_name
            or identity.item_name
            or "Configured product",
            variation_name_snapshot=identity.variation_name,
            store_id=store,
            units_sold=row["sold"],
            units_returned=row["returned"],
            net_units=net,
            unit_cost_snapshot=cost,
            extended_cogs=total,
            inventory_units_snapshot=quantity,
            inventory_value_snapshot=value,
            mapping_effective_date_snapshot=report.sales_end_date,
            source_transaction_count=len(row["links"]),
            warning_state=f"VENDOR_SKU_CONFIG:{mapping.id}",
        )
        db.add(line)
        db.flush()
        for fact, returned, count in row["links"]:
            db.add(
                FundingReportFactLink(
                    report_id=report.id,
                    report_line_id=line.id,
                    sale_fact_id=None if returned else fact.id,
                    return_fact_id=fact.id if returned else None,
                    allocated_quantity=count,
                    cogs_amount_snapshot=amount(count * cost * (-1 if returned else 1))
                    if cost is not None
                    else None,
                )
            )
        report.units_sold += row["sold"]
        report.units_returned += row["returned"]
        report.net_units += net
        costs.append(total)
        quantities.append(quantity)
        values.append(value)
    report.calculated_cogs = None if None in costs else sum(costs, ZERO)
    report.inventory_units_snapshot = (
        None if None in quantities else sum(quantities, ZERO)
    )
    report.inventory_value_snapshot = None if None in values else sum(values, ZERO)
    report.inventory_snapshot_at = min(snapshot_times, default=None)
    return {
        "allocation_semantics": SEMANTICS,
        "scope_signature": scope_signature(products, fallback, ambiguous_skus),
        "allocation_method": "VENDOR_PERIOD_ACTIVITY",
        "message": "Configured vendor products matched to sales and returns in the selected period.",
        "purchase_order_ids": [],
        "assigned_purchase_order_count": 0,
        "eligible_sku_count": len(products),
        "unknown_cost_variations": sorted(set(unknown_costs)),
        "source_lines": [
            {
                "vendor_id": account.vendor_id,
                "mapping_id": m.id,
                "sku": m.sku,
                "square_variation_id": v,
                "product": identities[v].product_name or identities[v].item_name,
                "variation": identities[v].variation_name,
                "unit_cost": str(m.unit_cost)
                if number(m.unit_cost) is not None
                else None,
            }
            for v, m in sorted(products.items())
        ],
        "setup_issues": [],
        "fifo_exception_count": 0,
    }
