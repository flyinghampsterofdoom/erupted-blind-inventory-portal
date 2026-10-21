# Immutable V2 exchange and return submissions

- Status: Accepted
- Date: 2026-07-15

## Context

V1 stores exchange/return forms as submitted facts and exposes no active edit/delete/correction workflow. The first V2 slice needs reliable attribution and duplicate protection without widening scope.

## Decision

Milestone 4 V2 submissions are immutable. V2 provides create, scoped history, and read-only detail only. It reuses `exchange_return_forms`, derives actor/store/server time, preserves required V1 fields, and uses a principal-scoped signed token plus audit fingerprint/advisory lock for idempotency.

## Consequences

No edit, overwrite, delete, void, correction, refund processing, or inventory effect exists. Corrections require a future explicit product decision. Duplicate retries return the original record. V1 routes remain active, and historical records are not modified.
