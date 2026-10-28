"""Narrow compatibility gate: no V4 engine import or schema mutation."""
from sqlalchemy import text


def require_legacy_purchase_order(db,purchase_order_id):
    if db.get_bind().dialect.name!='postgresql':return
    if not db.execute(text("SELECT to_regclass('purchasing_v4.po_ownership')")).scalar_one():return
    writer=db.execute(text('SELECT writer FROM purchasing_v4.po_ownership WHERE purchase_order_id=:id'),{'id':purchase_order_id}).scalar_one_or_none()
    if writer=='V4':raise ValueError('This purchase order is owned by Purchasing V4. Use its authorized document/workspace; legacy receiving and exports are unavailable.')


def financial_vendor_available(db,vendor):
    """Existing Square identities or explicitly bridged local suppliers only."""
    if vendor is None or not vendor.active:return False
    if str(vendor.square_vendor_id or '').strip():return True
    if db.get_bind().dialect.name!='postgresql':return False
    if not db.execute(text("SELECT to_regclass('purchasing_v4.supplier_financial_bridge')")).scalar_one():return False
    return bool(db.execute(text("SELECT EXISTS(SELECT 1 FROM purchasing_v4.supplier_financial_bridge WHERE legacy_vendor_id=:id AND origin='LOCAL')"),{'id':vendor.id}).scalar_one())
