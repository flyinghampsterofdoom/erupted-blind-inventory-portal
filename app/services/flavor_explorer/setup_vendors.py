"""Read existing supplier mappings solely for the administrator Explorer Setup."""
from sqlalchemy import select
from app.models import Vendor,VendorSkuConfig


class SetupVendorLoader:
    """Exact variation associations, including explicitly marked retired mappings."""
    def __init__(self,engine):
        self.engine=engine

    def __call__(self,variation_ids):
        identities=sorted(set(variation_ids))
        if not identities:
            return {}
        query=select(
            Vendor.id.label("id"),Vendor.name.label("name"),Vendor.active.label("active"),
            VendorSkuConfig.square_variation_id.label("variation_id"),
            VendorSkuConfig.active.label("mapping_active"),
        ).join(VendorSkuConfig,VendorSkuConfig.vendor_id==Vendor.id).where(
            VendorSkuConfig.square_variation_id.in_(identities)
        ).order_by(Vendor.id,VendorSkuConfig.id)
        result={}
        with self.engine.connect() as connection:
            for row in connection.execute(query).mappings():
                vendors=result.setdefault(row["variation_id"],{})
                previous=vendors.get(row["id"])
                vendors[row["id"]]={
                    "id":row["id"],"name":row["name"],"active":bool(row["active"]),
                    "historical":not bool(row["mapping_active"]) and (
                        previous is None or previous["historical"]),
                }
        return {identity:list(vendors.values()) for identity,vendors in result.items()}
