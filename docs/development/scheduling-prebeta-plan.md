# Scheduling pre-beta implementation

Authorized October 13, 2026. Baseline: 83b247976ef99e9923496211ef1134c203244889.
Development is isolated; production deployment, records, configuration and automation activation require separate authorization.

## Approved sequence

1. Authoritative publication validation and deterministic fairness regression.
2. Version-bound Lead approval/publication without automation administration.
3. Complete-future-week horizon X/Y (defaults 9/8), catch-up generation, no automatic publication, durable generation outcomes.
4. Shared effective coverage, Call Out, Shift Swap and same-day Store Swap, preserving revisions and separate Lead duty.
5. Blind Inventory effective-working-store authorization, stale-session/draft protection and emergency Select Store.
6. Employee preference requests with Admin decisions, dates, expiry and preserved baseline.
7. Disposable PostgreSQL, permission, concurrency, audit, failure-recovery and browser acceptance of the full journey.

## Simplified continuity

The scheduling dashboard shows Red when the next operating week is unpublished;
otherwise Yellow when generated upcoming weeks await publication; otherwise Green
when all generated upcoming weeks are published. Leads review and publish new weeks.
No multistage notifications, incidents, acknowledgments or repeated alerts are added.
An approved draft is not a published schedule. Automation never publishes.

## Emergency Select Store

Normal inventory access follows the linked employee's effective published working
assignment. Emergency selection is hidden unless explicitly authorized by a Lead,
Manager or Admin for a named employee/store, reason and bounded expiry. Reuse the
existing session selection UI; selection alone grants no authority. Audit authorization and
selection; retain expired grants. Revalidate server-side, including stale sessions and
schedule-context changes. Never silently bypass a swap, change permanent identity
or move inventory work between stores. Preserve saved and browser-pending drafts.

## Acceptance and release boundaries

Generation → Lead review → editing → approval → publication → employee visibility
→ call-out / shift / store swap → fairness and inventory access update. Test both
employees, concurrent sessions, expiry/reversal, stale requests and submissions
during swaps; retain completed counts and all history. Migration/recovery rehearsals
use disposable local databases. No production credentials or provider writes.

## Reuse and bounded changes

Existing Scheduling periods, published revisions, shifts, coverage requirements,
attendance events, replacement commitments, generator snapshots, fairness units,
employee identity, permission overrides, WebSession store selection and Blind
Inventory count storage remain authoritative. No separate assignment database is
introduced. Published assignments are preserved when a Lead applies a swap;
revisions and correlated audit events describe the change.

New persistence is limited to version-bound approvals, generation outcomes,
preference requests and temporary inventory-access grants, plus employee context
versions and the Y threshold. Migration 0032 extends schema 0031. Explicit
permission overrides continue to take precedence over role defaults. Lead review,
approval, publication and swaps do not grant automation administration. Admins
manage X/Y and preference decisions.

Inventory scope is resolved per business day from effective published assignments,
including active coverage commitments. Authentication retains its canonical
account identity; inventory receives a validated working-store scope. Legacy
shared store accounts without a linked employee retain their established scope.
Session store selection alone never grants inventory access. Schedule changes
invalidate existing grants; a new emergency exception must be expressly authorized
against the current assignment version. Grants last at most twelve hours.

Saved counts remain attached to the store where they were created. Browser-pending
entries are buffered per account, store and count; a stale context pauses writes
and requires refreshed tasks. They are never transferred to the new store. Employee
locks serialize inventory writes with assignment changes, so a racing submission
either completes under its original valid authority or is rejected after the swap.

## Phase acceptance

| Phase | Acceptance gate |
| --- | --- |
| Publication | Changed eligibility, restrictions, PTO, overlap, hours and consecutive days block publication; only exact, audited availability exceptions survive unchanged revisions. |
| Lead review | Authorized Lead can edit, approve and publish without automation administration; edits invalidate approval; history identifies approval and publication actors. |
| Horizon | Default X/Y 9/8; current week excluded; initial and interrupted generation fill missing future slots, preserve existing weeks, and never publish. |
| Coverage and swaps | Call Out and direct/two-way Shift Swap are explicit actions; Store Swap validates both employees atomically, retains original records, supports reversal and updates effective coverage. |
| Fairness | Clean elapsed work is presumptive; replacements receive coverage credit, future commitments remain reservations, revisions do not double-count, and Lead duty remains separate. |
| Inventory | Logged-in employee follows swapped store; stale requests, concurrent sessions and submissions during transition revalidate; completed counts and pending work remain intact. |
| Emergency | Hidden until authorized; reason, authorizer, employee, store and times audited; expiry restores normal scope; an existing swap cannot be bypassed by an obsolete grant. |
| Preferences | Requests do not affect generation until Admin approval; temporary preferences expire back to permanent/baseline values; dated decisions and revocations retain historical meaning. |
| Recovery | Disposable PostgreSQL upgrade/downgrade rehearsal; downgrade refuses retained new evidence or a nine-week policy incompatible with the previous schema. |

## Release preparation and boundaries

Development and tests use the isolated `codex/scheduling-prebeta` branch, synthetic
fixtures and disposable local PostgreSQL databases. No production records,
configuration, automation schedules, provider inventory or Square identity/catalog
are changed by this work.

Before a separately authorized deployment:

1. Review the final diff, backup/recovery procedure and schema-0032 compatible web/job artifacts together.
2. Inspect existing production X/Y and coverage settings. The migration preserves
   existing X; explicitly configure the approved 9/8 horizon and one required
   employee per store per operating day. Preserve support for future double coverage.
3. Keep the external scheduling execution gate disabled while migrating and testing.
   Generate and obtain Lead approval/publication for the required operating weeks.
4. Verify live employee identity links, store scopes, scheduling timezone and the
   continuity indicator. Red requires Lead action; the system never manufactures
   approval or publishes automatically to remove a coverage gap.
5. Obtain separate authorization before activating recurring generation.

Rollback must preserve new audit and request/grant records. Once these exist, do
not run a destructive downgrade or restore the old auto-publication job. Retain
schema-compatible recovery code or apply a reviewed forward fix. Live provider,
production session and infrastructure acceptance remain deployment checks;
local tests cannot establish their configuration.
