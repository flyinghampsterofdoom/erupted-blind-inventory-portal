"""Owner-readable descriptions of pinned purchasing decisions, independent of stock."""


def describe(state, reasons):
    reasons=tuple(reasons or ())
    if state=='EXCLUDED_NO_REORDER':return dict(state='NO_REORDER',explanation='No Reorder: intentionally excluded from purchasing.')
    if state not in ('UNRESOLVED_EVIDENCE','UNRESOLVED_PACK_CONFIGURATION'):
        return dict(state='RECOMMENDED',explanation='Calculated from the worksheet evidence and authoritative policy.')
    messages=[]
    groups=(
        (('NO_OBSERVATION','ON_HAND_UNAVAILABLE','ON_HAND_UNKNOWN'), 'Current inventory is unknown or unavailable; no zero was assumed.'),
        (('STALE_OBSERVATION','STALE_COVERAGE','INCOMPLETE_OR_STALE_SOURCE'), 'Source verification is too old or incomplete; refresh evidence.'),
        (('NEGATIVE_INVENTORY_CANDIDATE_GATE',), 'This saved calculation was blocked by negative stock. A new calculation uses zero and retains the discrepancy.'),
        (('UNRESOLVED_FROZEN_LEGACY_MANUAL_POLICY','UNRESOLVED_MANUAL_POLICY_BOUNDARY','UNRESOLVED_LEGACY_MANUAL_','INVALID_LEGACY_MANUAL_'), 'Manual PAR/LEVEL boundaries or product mapping require owner review; manual authority is preserved.'),
        (('SUPPLIER_EVIDENCE_EXCLUDED','UNRESOLVED_PRIMARY_SUPPLIER','UNRESOLVED_OWNER_DEFAULT_SUPPLIER','UNRESOLVED_OWNER_SUPPLIER','UNKNOWN_OR_INVALID_EXPECTED_COST','OWNER_OFFER_EXPECTED_COST_UNKNOWN'), 'A verified supplier selection or purchasing cost is missing.'),
        (('UNKNOWN_OR_INVALID_PACK_SIZE',), 'Supplier purchasing pack size needs verification.'),
        (('SUPPLY_UNKNOWN','SUPPLY_UNAVAILABLE','CONFLICTING_INCOMING','UNQUALIFIED_SUPPLY','UNRECONCILED_RECEIPT'), 'Incoming supply or receiving reconciliation is unresolved.'),
        (('INSUFFICIENT_CONFIRMED_AVAILABLE_DAYS',), 'Fewer than 14 confirmed-available days; automatic forecasting is unavailable. Known current inventory is still shown.'),
        (('INCOMPLETE_CALENDAR_SALES','UNKNOWN_AVAILABILITY_OR_SALES','SALES_EVIDENCE_'), 'Historical sales or availability evidence is incomplete; known current inventory is still shown.'),
    )
    for prefixes,message in groups:
        if any(reason.startswith(prefixes) for reason in reasons):messages.append(message)
    return dict(state='UNRESOLVED',explanation=' '.join(messages) or 'Purchasing evidence or policy requires review; no purchase quantity was inferred.')
