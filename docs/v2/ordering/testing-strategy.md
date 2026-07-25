# V2 Ordering testing strategy

Status: proposed. These are future acceptance requirements, not tests added in this discovery milestone.

## Test layers

| Layer | Required evidence |
|---|---|
| Pure domain | Deterministic calculations, state transitions, rounding, allocation invariants, explanation codes |
| Repository/PostgreSQL | Real PostgreSQL constraints, transactions, row versions, idempotency uniqueness, migration upgrade/downgrade policy |
| Router/security | capability, store scope, CSRF, validation, conflict/result contracts, sensitive-field visibility |
| Integration contract | captured Square fixtures/fake gateways, PDF renderer/storage, timeout/partial/outcome-unknown handling |
| End-to-end | principal canary workflow with external sends disabled until Phase 7 |
| Regression | full V1 suite and focused V1 Ordering/receiving/count/PDF behavior unchanged |

## Required scenarios

- Calculation byte-for-byte determinism for the same algorithm/input version.
- Velocity windows, zero eligible days, stockout correction, declining/no sales, new product, stale data, and conflicting inventory.
- MOQ then case rounding, unit conversion, maximum conflicts, and decimal/currency rounding.
- Permanent/dated exclusions, precedence, store/vendor/SKU scope, expiry, and ignored-recommendation recurrence.
- Preferred vendor, unavailable preference, fallback approval, duplicate/default mappings, and missing cost/lead time.
- Concurrent draft edit; stale approval; database-enforced duplicate approval rejection.
- Duplicate receipt submit, partial/multiple receipt, overage/short/damage/backorder/mis-ship/unknown item, and cross-store denial.
- Square batch partial success, retry of failures only, network failure after remote success, duplicate send, read-only mode, reconciliation before retry.
- Partial payment, multiple payments, one payment across POs, reversal/dispute, allocation balance, funding-account authorization, and period correction.
- COGS source snapshot/recalculation reproducibility and approved recognition/valuation policy.
- Audit actor, scope, reason, before/after/event, correlation and failure completeness.
- PDF semantic golden tests: immutable snapshot fields, totals, page repeatability, renderer version/hash, stale mutable mapping isolation.
- PostgreSQL migrations from current head on empty and representative databases; constraint/race tests must not rely on SQLite behavior.

## Square test boundary

Captured, scrubbed Square response fixtures cover catalog, inventory counts, orders, vendors, locations, pagination, missing fields, and API errors. A deterministic fake read gateway tests freshness and conflicts. A programmable fake write gateway simulates success, partial success, timeout-before-send, timeout-after-success, throttling, and reconciliation. No production Square writes are part of automated tests.

## PDF validation

Avoid fragile pixel-only assertions. Assert snapshot-to-document semantic content, calculated totals, stable metadata rules, file hash policy, and renderer-version behavior. Add a small reviewed visual/golden set for layout regressions.

## Release gate

Every phase publishes passed/failed/skipped counts, skip reasons, PostgreSQL evidence, external dependencies, and V1 regression results. A skipped external test does not become a pass; its production risk and required environment are stated in the release report.
