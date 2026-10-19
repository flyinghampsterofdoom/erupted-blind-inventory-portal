# Square order version compatibility — local release preparation

No production deployment, migration, configuration change, or job execution is
approved by this document. Obtain owner authorization for the corrected revision
and migration 20261019_0040 before execution.

## Compatibility correction

Square Order.version is optional. Orders not created through the Orders API can
omit it (https://developer.squareup.com/reference/square/objects/Order).
The importer retains null; it never substitutes a counter. Existing known
financial fact versions are not erased by an absent field.

The new purchasing_order_source_state contains only order ID, location ID,
optional actual version, actual source update timestamp, SHA-256 of the source
payload, and observation time. It is an update checkpoint, not a financial ledger.
It also protects cancellations with no lines or previous financial facts.

When both observations have versions, the explicit counter orders them; equal
versions use the source update timestamp. Unversioned observations use updated_at.
Exact rational RFC3339 comparison reuses the tested deployment timestamp utility,
including nanoseconds and timezone offsets. An identical response is a no-op;
older source observations cannot undo a correction. Conflicting content at the
same version/time or a changed location fails with an order-specific diagnostic.
Missing identities, malformed timestamps, invalid versions and quantities fail
without certifying the interval. Failed transactions do not retain a checkpoint.

## Schema 39 forward compatibility

Migration 39 is unchanged. Migration 40 makes the correction's version nullable
and adds the small source checkpoint table. It does not update existing rows or
historical money. Startup continues to fail closed on an unmigrated database:
the new writer requires schema 40, not bare schema 39. The tested forward path is
38 → 39 → 40, including populated schema 39 preservation, interrupted DDL rollback,
and safe repeated upgrades. Do not describe this as a migration-free release.

A rollback must retain schema 40 and its evidence. Never downgrade a populated
checkpoint table or delete unversioned corrections to make old code start.
Before any production migration, prepare/verify a recovery artifact from the last
verified production baseline with ONLY schema 40 added to its accepted revisions.
Verify its read-only access against the restored schema 40 database. The existing
2b549889 recovery artifact accepts only 38/39 and cannot be reused unchanged.

## Operational invocation after separate deployment authorization

Keep the existing fresh database backup, isolated restore and protected-record
fingerprints; preserve the 50 missing historical PDF references without claiming
missing bytes are backed up. Verify recovery artifact and web/cron schema
compatibility before migration. Deploy/verify the compatibility bridge first,
then migrate once, then deploy/verify the corrected candidate web.

Use the committed finite operational runner, not the old compressed wrapper kept
in output/performance-production as incident evidence. A future one-off Render
job's command is:

```
ERUPTED_EXPECTED_REVISION=<exact-approved-40-character-revision> ERUPTED_EXPECTED_SOURCE_SHA256=<verified-64-character-source-digest> python scripts/release/refresh_purchasing_history.py
```

Obtain both values from independently verified build/runtime evidence. Do not
change persistent service environment or add a recurring job for this invocation.
The runner binds the source tree to those values before any refresh, restricts
Square endpoints to reads, retains the request/time budgets, and prints the
original refresh failure with credentials redacted. It preserves an already
recorded source error instead of replacing it with a generic parity failure.
Retrieve provider logs using the one-off job's resource ID, not the web service ID.
Never retry automatically after a failed gate.

Only after full history, financial/source parity, preserved order #1 (896 lines /
3,842 saved units), owner access and web checks pass may the previously approved
coordinated cron procedure proceed, with its unchanged hourly schedule and matched
successful execution evidence. Keep the 90-second legacy ordering render gate,
2 GB web allocation, existing authentication, immutable PDFs and all existing
Square/payment/purchasing write restrictions. No regeneration or owner edits are
part of verification.

On failure restore the prepared schema-40-compatible previous application
revision using the tested web and supported exact-commit cron recovery procedures;
retain additive schema/evidence. Prove running revisions and cron execution, not
just accepted requests. Database restoration remains a separately controlled
recovery step, never an automatic way to erase legitimate concurrent records.

## Rehearsal boundaries

The production-compatible restored database's actual initial Purchasing V3
bootstrap is 84 completed days plus the current partial day for source freshness.
It is not a lifetime historical re-import. The existing new/empty financial-state
120-day default and the 84-day recommendation policy are unchanged.
The full four-store source was captured through historical READ endpoints only,
then replayed without network, including sales and movement interruption/resume
and repeated initial intervals. Original financial values and protected records
remain unchanged. Source-return discrepancies are retained for review, never
silently rewritten. Captures with customer/payment source detail remain in the
owner-only private directory, outside Git and reports.

Local macOS RSS and captured-source execution do not certify future Render
latency, Linux memory or provider availability. Retain hosted verification gates;
there is no production activation under the local implementation authorization.
