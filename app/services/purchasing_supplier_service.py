"""Purchasing identity comes from explicit mappings and catalog candidates, never financing."""
from sqlalchemy import select
from app.models import Vendor, VendorSkuConfig


class SupplierContext:
    def __init__(self, db, variation_ids, *, lock=False):
        vendors = select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.id)
        mappings = select(VendorSkuConfig).where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.square_variation_id.in_(variation_ids)).order_by(VendorSkuConfig.id)
        if lock:
            vendors = vendors.with_for_update(read=True)
            mappings = mappings.with_for_update(read=True)
        rows = db.scalars(vendors.execution_options(populate_existing=True)).all()
        self.vendors = {v.id: v for v in rows}
        self.catalog = {}
        for vendor in rows:
            if vendor.square_vendor_id:
                self.catalog.setdefault(vendor.square_vendor_id, set()).add(vendor.id)
        self.defaults = {}
        for mapping in db.scalars(mappings.execution_options(populate_existing=True)):
            if mapping.is_default_vendor and mapping.vendor_id in self.vendors:
                self.defaults.setdefault(mapping.square_variation_id, set()).add(mapping.vendor_id)

    def resolve(self, variation_id, catalog_vendor_ids):
        candidates = set()
        for key in set(catalog_vendor_ids or ()):
            candidates.update(self.catalog.get(key, ()))
        defaults = self.defaults.get(variation_id, set())
        if defaults:
            if len(defaults) != 1 or not defaults <= candidates:
                return None, 'CONFLICTING_DEFAULTS'
            return next(iter(defaults)), 'EXPLICIT_DEFAULT'
        if len(candidates) == 1:
            return next(iter(candidates)), 'SINGLE_CATALOG_SUPPLIER'
        return None, 'NEEDS_SUPPLIER'
