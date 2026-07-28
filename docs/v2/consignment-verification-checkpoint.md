# V2 consignment PostgreSQL and backfill verification checkpoint

Date: 2026-07-28

## Readiness classification

**INTERNAL ORDER-PAYMENT/REPLENISHMENT PREVIEW READY AFTER MIGRATION; SQUARE-DERIVED COGS REPORTING NOT
READY FOR OWNER SIGN-OFF.**

The local PostgreSQL, synthetic source-data, accounting, authorization-contract, CSRF, and captured-email
checks pass after the corrections described below. The V1-backed payment and receipt-replenishment subset
does not depend on Square sandbox evidence and is ready for a named-owner preview after migration. Final
COGS sales/report sign-off remains blocked because no disposable clone of representative production data
and no Square sandbox or controlled non-production account were available. The configured database is the
Render production endpoint and the configured Square base URL is the production API; neither was used for
destructive or controlled-transaction verification.

No deployment, migration of the configured database, Square request, email delivery, or feature exposure
change was performed.

## Environment and migration evidence

- Database: disposable local PostgreSQL 16.12 cluster on `127.0.0.1:55439`.
- Revisions exercised in order: `20260725_0009` → `20260728_0010` → `20260728_0011`.
- `0011` downgrade to `0010` removed the immutable-fact tables while retaining the `0010` ledger, then
  re-upgrade to `0011` succeeded.
- Alembic has one head: `20260728_0011`.
- Fresh-head versus stepped-upgrade schema comparison: exact match, no differences and no ORM warnings.
- PostgreSQL migration integration suite: 1 passed; it also exercises fresh upgrade, supported downgrades,
  re-upgrades, baseline stamping, schema comparison, and startup without runtime schema mutation.
- Consignment schema inspection found 35 foreign keys, 10 check constraints, four required source/link
  unique constraints, and both partial unique ledger indexes.
- Direct duplicate inserts proved the database rejects a second `COGS_GENERATED` and a second
  `VOID_REVERSAL` for one report.
- `order_payments_v2` is absent from global and principal feature configuration. No exposure was added.
- The production-revision direct-upgrade variant could not be tested because the live database was not
  queried and no disposable production clone was supplied.

## Historical and synchronization rehearsal

Synthetic controlled range: 2025-01-01 through 2026-06-30, three stores, 12 variations, three Square-style
pages, renamed product/SKU state, a changed current cost, an archived catalog identity, discounts, later
updates, and late offline activity.

The controlled source set contained 1,237 orders: 1,200 sale lines, 32 itemized return lines, and five
unitemized refunds. It produced 1,200 sale facts and 37 return/refund facts. Thirty returns were attributed
to original sales. Seven facts were blocked as designed: one cumulative over-return, one unmatched return,
and five unitemized refunds. The controlled clean-report step explicitly excluded those seven fixtures with
a reason; it did not fabricate attribution or COGS.

A separately measured 1,200-sale insertion segment completed in 5.079 seconds, used three source pages,
executed 8,406 SQL statements, and peaked at 17.35 MiB of traced Python memory. The exact second run
completed in 1.233 seconds with 2,408 SQL statements: 0 sales created, 0 returns created, 1,200 existing
facts observed, and unchanged row counts and economic COGS totals.

The query count confirms a line-level lookup pattern in the historical importer. At this measured scale the
five-second runtime is acceptable for an owner-triggered preview, so no speculative query rewrite was made.
Production-scale acceptability remains unproved until a representative clone is available.

Failure injection after one yielded page left zero rows from the interrupted page sequence, retained the
previous successful watermark, and recorded `FAILED`. A later successful run restored `COMPLETE`.
An incremental overlap run containing one unchanged order and one new order completed in 0.0632 seconds
and 15 SQL statements: the existing fact remained singular and the new fact was inserted once. A late
offline order inside the overlap window was also imported once.

Catalog rename, SKU rename, vendor/current-cost mutation, and a second synchronization did not alter the
existing sale fact's product, vendor, unit-cost, or extended-COGS snapshots.

## Controlled reconciliation and owner accounting example

No Square API was called. The following is a PostgreSQL-backed Square-shaped fixture result, not Square
sandbox evidence.

- Vendor: Verification Consignment Vendor
- Stores: 3
- Active source links in the scaled report: 2,431 (2,401 sales and 30 attributed returns)
- Net units: 2,452.000
- Period COGS: $17,129.60
- Sum of immutable report-fact link snapshots: $17,129.60
- Reconciliation difference: $0.00
- Opening unreplenished COGS and every in-period ledger component were snapshotted separately.
- Replenishment applied, cash settlements, approved credits, and void reversals are shown as explicit signed
  components; closing unreplenished COGS is reconstructed from opening balance plus period COGS minus those
  typed components.
- Available replenishment credit remains separate from unreplenished COGS.
- Inventory snapshot: 36 store/variation rows with timestamp, signed quantity, unit cost, signed value,
  product/variation/SKU snapshots, and an explicit negative-inventory warning.
- Draft generation: 0.523 seconds and 84 SQL statements.
- Attribution queue read: 0.017 seconds and seven SQL statements while loading 500 candidate sales.
- Vendor inventory summary read: 0.005 seconds and three SQL statements for 36 store/variation rows.
- Finalization: 0.011 seconds; two service calls produced exactly one `COGS_GENERATED` row.
- Database uniqueness independently rejected a duplicate finalization ledger row.
- Void: 0.005 seconds; exactly one `VOID_REVERSAL`; original plus reversal net to $0.00.
- Void audit metadata contains actor/time/reason plus original and reversal ledger entry IDs.
- The voided report and its fact links remain preserved; regeneration can use the facts again.
- Repeated generation of the same unfinalized period replaces that preview and its links instead of leaving
  parallel draft economic records.

Return reconciliation uses the original sale's vendor and unit-cost snapshots. Partial return COGS is
rounded from returned quantity times original unit cost. Cumulative partial returns that exceed original
quantity now remain `SOURCE_INCOMPLETE` with an explicit reason and cannot enter a report. Unmatched and
unitemized refunds remain blocked.

## Captured email, access, and mutation safety

- Captured email performs no network operation and reads its recipient only from `VendorPaymentSetting`.
- Missing vendor email blocks capture.
- Subject includes vendor and report period.
- Captured body includes period, opening balance, current COGS, replenishment, cash settlements, approved
  credits, void reversals, closing unreplenished COGS, available credit, inventory timestamp, quantity, and
  value.
- Two captures created two delivery events. Successful body content is stored separately from failure text.
- Capture changes no settlement/payment state; it only moves a finalized report to `EMAILED`.
- Owner/admin principal-scoped exposure and an authorized owner Payment Methods render path pass.
- Unexposed principals and store principals receive guarded denial through the feature/owner dependencies.
- Every module POST declares feature exposure, owner authorization, and CSRF verification dependencies.

## Defects discovered and corrected

1. Cumulative partial returns could exceed the original sale. Added capacity validation during import,
   retry, and owner linking, plus regression coverage.
2. Report balance evidence omitted explicit cash, approved-credit, and void-reversal period snapshots.
   Added typed snapshots, explicit closing formula, owner UI fields, and reconciliation coverage.
3. Captured email accepted a form-provided recipient and stored successful body text as an error. It now
   requires the vendor profile recipient and stores a dedicated immutable body snapshot.
4. Void audit metadata omitted ledger identifiers. It now records original and reversal entry IDs.
5. Repeated draft generation left parallel previews and duplicate links. Exact-period draft regeneration now
   replaces only the unfinalized preview and preserves finalized/voided history.
6. Payment Methods referenced an undefined `vendor_id` during render. The unused query was removed and a
   render regression was added.
7. The attribution queue treated already excluded/non-consignment facts as unresolved. It now selects only
   blocking statuses.

## Remaining COGS blockers and next checkpoint

The internal V1-order preview is not blocked by the following items. They apply specifically to the
Square-derived COGS sales/report workflow:

1. Provision a disposable clone of the production schema plus representative, sanitized production data;
   repeat the range and performance rehearsal and compare pre/post V1 order totals and assignments.
2. Configure Square sandbox or a controlled non-production Square account and execute/document the named
   order, return, refund, rename, reassignment, cost-change, pagination, retry, and rate-limit cases with real
   Square IDs.
3. Have the owner sign the example report against those real controlled transactions and confirm the explicit
   settlement components.
4. Independently deploy the V1-backed preview by migrating first, keeping global exposure off, exposing
   only the verified named owner principal, running a read-only order/detail smoke test plus a controlled
   partial receipt, and retaining the feature-disable rollback.

Do not deploy or enable the feature as part of this checkpoint.
