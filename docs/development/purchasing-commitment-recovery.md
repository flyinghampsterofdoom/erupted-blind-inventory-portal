# Purchasing commitment-compatible recovery

This recovery is based on production `df191239cf48ba46e04c50cf06eed354201f6326`.
It retains the production category filters, pages, calculations, schema and cron
code. It does not include PR #13's category menu or creation route.

The runtime correction is restricted to four existing files: session decisions,
batch-save guards, existing PO detail/deletion context, and existing financing
initialization context. No schema/data migration or history rewrite is required.

The existing `purchase_commitment` decision stores the immutable PO identity,
quantity, four-store allocation map and intended financing account. Recovery
recognizes that evidence before legacy converted-group fallback. Supplier
finalization validates and orders only uncommitted products and saves the same
commitment contract, so a subsequent forward deployment recognizes decisions
made during recovery. Session row locking retains existing duplicate protection.

Committed lines reject direct and batch edits, transfer, remove and undo.
Refresh/discard leave their evidence intact. Remaining products retain the
existing edit and supplier-order workflow. Existing PO history and financing
lookups recognize every split draft; intended-account changes for remaining
products do not rewrite an earlier category draft's financing choice.

`test_purchasing_commitment_recovery.py` covers partial/full/mixed supplier
states, duplicate and concurrent finalization, four-store quantities, immutable
committed products, editable remaining quantities, refresh/discard/account
behavior, history deletion protection and unchanged financial/inventory rows.
The release qualification additionally restores the exact source artifacts and
executes production → candidate → recovery → candidate cycles against
disposable PostgreSQL, retaining original records across both directions.

Activation requires successful exact-commit CI for recovery and the final merged
web candidate, matching source/archive identities, actual advisory-artifact
installation through the complete configured Linux build command, a verified
recovery point and release-specific restoration evidence. The production cron
revision, environment and schedule remain pinned and unchanged. Do not use the
unmodified production source as recovery after category commitments exist.
