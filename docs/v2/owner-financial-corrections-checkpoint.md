# V2 owner financial-corrections checkpoint

Status date: 2026-08-03

Decision: **READY FOR PRINCIPAL-SCOPED OWNER PREVIEW**. This checkpoint does not authorize a global enable or deployment.

## Scope boundary

The correction model is vendor-agnostic. It accepts any active vendor already present in the Square-backed `vendors` registry and does not create local vendors. No real vendor name is encoded in models, migrations, routes, services, UI labels, or tests. PostgreSQL workflow fixtures use Source Vendor A (`square_vendor_id` `V-1`) and Target Vendor B (`square_vendor_id` `V-3`).

`purchase_orders.vendor_id` remains the immutable V1 source vendor. `order_payments.vendor_id` is the independently correctable financial vendor. A one-order or selected-order correction does not change vendor defaults.

## Migration evidence

- PostgreSQL 16.12 disposable database: `20260729_0013 -> 20260801_0014` succeeded.
- Alembic reports one head: `20260801_0014`.
- ORM-to-database schema comparison matched after upgrade and after re-upgrade.
- Foreign keys, indexes, checks, enum/check values, and the schema's uniqueness contract matched the ORM definition.
- The four new tables were empty after upgrade; no payment, adjustment, reassignment, transfer, or audit facts were synthesized.
- Seeded V1 and V2 row counts and values were unchanged.
- `20260801_0014 -> 20260729_0013` succeeded on the clean migration fixture, removed the four new tables, and preserved existing rows. Re-upgrade succeeded and matched again.
- Full PostgreSQL-enabled suite: 351 passed, 1 unrelated private-R2 integration skipped, 0 PostgreSQL-gated skips. The two warnings are existing FastAPI startup-event deprecations.

The migration is additive: it adds four correction/event tables, extends the consignment ledger type check with typed assignment-transfer events, and permits the two legacy single-vendor summary pointers on an initialization operation to be null when one queue batch spans original vendors. Per-order result rows retain each source vendor. It does not perform destructive data conversion.

## Guarded production inspection

- The authenticated production UI was inspected read-only; no production mutation was made.
- The deployed application matches commit `023a6c6ef9e183c65ac02c9a7d4162397076b6e2` (`Polish order payment owner copy`).
- That commit starts only when the schema revision is `20260729_0013`, so the successful running application gate confirms the current supported production revision is `20260729_0013`. Direct database SQL was unavailable from this workstation because the configured Render database hostname is private.
- Rollback commit: `cd0698e727886f516eb64c159e76833e3f0b635a` (`Polish consignment owner summary`).
- Intended migration path: `20260729_0013 -> 20260801_0014`; no revision conflict was observed.

## Square vendor registry evidence

The configured production Square account was queried through `POST /v2/vendors/search`. Code inspection confirms this is a read-only Square call; `sync_vendors_from_square` writes only local `vendors` rows.

- Square response: 40 distinct stable Square vendor IDs; 17 active and 23 inactive.
- First disposable-registry sync: 40 created, 0 updated, 0 deactivated.
- Second sync: 0 created, 0 updated, 0 deactivated.
- Stable identities and names were unchanged; no financial assignments, mappings, or payment defaults were created or modified.
- No exact normalized duplicate group or possible near-duplicate pair (normalized-name similarity threshold 0.88) was found.
- At least two distinct active Square-backed vendors are confirmed, and generic reassignment between two eligible fixture vendors is verified.
- Missing, inactive, or non-Square targets remain blocked with the existing owner guidance to add the vendor in Square.

## Financial workflow evidence

- Single and bulk assignment changes preserve the V1 vendor, update only selected `order_payments` rows, preserve unselected orders and defaults, share one bulk operation ID, and record an individual immutable change per order.
- Assignment history freezes source, prior, and target local IDs, names, Square IDs, prior states, downstream impact, actor, reason, effective date, and transfer IDs. A repeat assignment to the current target is rejected without another correction.
- Manual payment fixtures cover partial, multiple, paid, overpaid, reversal, and replacement states. Original events remain present; reversal and replacement links reconcile to the derived paid and remaining amounts.
- Amount-correction fixtures cover charge, credit, reversal, replacement, and post-finalization adjustment. Original calculated amounts and finalized report totals remain unchanged.
- Consignment transfer preserves posted receipt, replenishment, and credit facts. Equal typed transfer-out and transfer-in rows move the vendor effect with net-zero combined value and reject an unintended repeat.
- Owner/principal feature gating, OWNER-only mutation authorization, CSRF dependencies, separate external-COGS gating, and V1 non-mutation tests pass.
- One queue bulk save spanning Source Vendor A and the fixture consignment vendor creates one initialization operation ID, links exactly the selected results to it, leaves the unselected order untouched, and preserves each purchase order's original vendor.

## Recommended preview path

After review, deploy only to the existing principal-scoped owner canary, migrate from `20260729_0013` to `20260801_0014`, retain the current principal allowlist, and keep global enablement off. Verify the revision and read-only list/detail pages immediately after deployment before the owner confirms any correction.
