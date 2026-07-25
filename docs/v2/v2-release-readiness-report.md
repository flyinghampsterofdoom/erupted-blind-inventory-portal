# V2 release readiness report

Assessment date: 2026-07-25
Repository state assessed: Phase 1 Ordering pre-deployment checkpoint working tree based on `676b648`; implementation review approved
Schema contract head: `20260720_0006`

## Executive assessment

V2 has a mature additive foundation and several implemented owner-preview modules, but the repository is **not ready for broad production exposure or any V1 cutover**. The complete suite now passes against PostgreSQL 16.12 and documentation is reconciled to current implementation. Deployment confidence is still constrained by an unverified target production database/environment, unverified real R2 behavior for media scopes, and partial disable semantics for device-facing Digital Signage and Touchscreen runtimes.

Phase 1 read-only Ordering Intelligence has since been implemented behind a disabled-by-default independent key. V1 behavior, feature parsing semantics, authorization, and canonical ownership remain unchanged. No deployment or production exposure has occurred.

## Architecture

The application remains one FastAPI/Jinja/PostgreSQL service with additive V2 routes, shared authentication, independent capability checks, server-resolved store scope, CSRF protection, structured audit envelopes, and environment-backed feature exposure. V1 routes and services remain directly available and do not depend on V2 navigation, Current Store, or exposure.

The schema is a linear Alembic chain from immutable V1 baseline `20260715_0001` through Daily Store Logs, Staff Scheduling, Store Shifts, Digital Signage, and Customer Touchscreen to `20260720_0006`. Startup validates the exact supported revision and performs no schema mutation.

## Infrastructure

| Area | State | Evidence or gap |
|---|---|---|
| Repository | Healthy | Clean and synchronized at audit start |
| Schema tooling | Implemented | Baseline, validation, stamping, compatibility profile, additive upgrade, startup check |
| Target database | Unverified | Configured database unavailable; revision `20260720_0006` not confirmed |
| PostgreSQL integration | Verified locally | All 59 opt-in cases passed on PostgreSQL 16.12 using disposable databases |
| R2 | Unconfigured/unverified locally | One real integration test skipped; required for media release scope |
| Square | Read-only in audited environment | No real Square request performed; no V2 Square write gateway exists |
| Deployment controls | Documented | Release checklist and canary guide now define evidence and rollback |

## Implemented modules

- V2 shell, responsive navigation, presentation contracts, feature exposure, audit, and store scope.
- Exchanges and Returns submit/history/detail behind `exchanges_returns_v2`.
- Daily Store Logs, Current Store, management actions, and completion dashboard behind `daily_store_logs_v2`.
- Staff Scheduling weekly board, warnings, revision lifecycle, labor summaries, shift editing, and reusable Store Shifts behind `staff_scheduling_v2`.
- Digital Signage administration, private media model, advertisement groups, display credentials, and player behind `digital_signage_v2` for administration.
- Customer Touchscreen management, device authentication, flavor/catalog management, inventory-aware Square read cache, media, and customer application behind `touchscreen_v2` for administration.
- Ordering compatibility links to unchanged V1 pages behind `ordering_v1_links_v2`.
- Read-only, deterministic store-level Ordering Intelligence behind `ordering_intelligence_v2`, with no PO creation or Square write.

Implemented means present in the repository, not necessarily deployed, exposed, infrastructure-verified, or owner-approved.

## Remaining V1 work

V1 remains canonical for authentication/account administration, dashboards, chores, opening checklists, customer requests, employee logs, inventory counts/recounts, non-sellable stock, cash/change/master-safe/reconciliation, reporting, vendor/mapping/par configuration, purchase orders, PDFs, receiving, and all Square write workflows. Ordering Intelligence is advisory only; every operational Ordering action remains V1. Detailed estimates and remaining work are in the [feature parity ledger](./v1-v2-feature-parity-ledger.md).

## Feature exposure audit

`exchanges_returns_v2`, `daily_store_logs_v2`, `staff_scheduling_v2`, `ordering_v1_links_v2`, and `ordering_intelligence_v2` independently enable and disable their complete implemented user/management surfaces, subject to independent permissions and scope.

`digital_signage_v2` and `touchscreen_v2` independently gate management navigation/routes but do **not** disable already provisioned `/display/*` or `/touchscreen/*` device runtimes. Those runtimes require display/device credential revocation for rollback. This is now explicitly documented as TD-003 and TD-004. No other undocumented module dependency was found.

## Known risks

1. Target production schema/environment remain unverified even though the full migration chain and database-backed suite passed against disposable PostgreSQL 16.12.
2. Real R2 behavior is unverified and R2 is not configured locally; this does not block an Ordering-only canary.
3. Device-facing Signage and Touchscreen lack feature-key runtime kill switches.
4. Shared V1 principals and an unresolved multi-store assignment model limit individual accountability at scale.
5. No common observable/idempotent V2 Square write gateway exists; V2 external writes must remain out of scope.
6. Visual/browser regression evidence is limited.
7. Ordering Intelligence uses synchronous live Square reads without a last-known-good cache, module timeout budget, or metrics (TD-026).
8. Generic `alembic check` cannot currently serve as the no-drift gate because legacy baseline/model differences include a PostgreSQL JSON default comparison failure (TD-027); the versioned upgrade and schema-contract suite remain the current gate.

## Outstanding technical debt

The authoritative backlog is [V2 technical debt register](./v2-technical-debt-register.md). Target production schema/environment verification remains a release concern; real R2 verification applies only when a media module is in scope. High-value P1 work includes runtime kill switches, exposure validation, identity/store assignment, touchscreen cache operations, Ordering read observability, schema-drift tooling, and the Square gateway before any V2 external write.

## Documentation status

Reconciled in this pass:

- Rewritten legacy/new-capability parity ledger with completion estimates.
- Current schema head and full migration chain.
- Scheduling foundation and weekly-board status.
- Current roadmap and dependency sequence.
- Feature-key inventory, dependencies, and disable limitations.
- Deployment/rollback plan, production checklist, and canary guide.
- Daily Store Log, Exchanges and Returns, Digital Signage, Touchscreen, scope, status, result, navigation, ordering bridge, and Render compatibility records.
- New technical debt and test-verification records.

Historical discovery documents remain snapshots of V1 behavior. Proposal/blueprint documents are explicitly subordinate to the current parity ledger and roadmap where implementation has advanced.

## Testing status

Command: `.venv/bin/python -m pytest -q -rs`

| Passed | Failed | Skipped | Warnings |
|---:|---:|---:|---:|
| 247 | 0 | 1 | 2 |

The sole skip requires real R2 credentials plus explicit opt-in and is outside the Ordering-only scope. The warnings are the pre-existing FastAPI startup-event deprecation. See [V2 test verification](./v2-test-verification.md).

## Readiness ratings

| Category | Rating | Assessment |
|---|---|---|
| Foundation | Strong | Core contracts and additive architecture are implemented and unit-tested |
| Infrastructure | Locally verified; target pending | PostgreSQL 16 suite and disposable upgrade pass; target environment is not yet verified and R2 remains conditional by scope |
| Documentation | Ready for continued development | Current-state documents, roadmap, backlog, release, and canary records are reconciled |
| Feature Parity | Early | One legacy slice is substantially implemented; most V1 domains remain canonical and unreplaced |
| Production Readiness | Owner canary pending identity/environment gate | Not ready for global exposure or cutover; a narrow Ordering canary is reasonable after exact owner exposure and target checks |

## Objective release decision

- **Broad V2 production enablement:** No-go.
- **Any V1 canonical-owner cutover:** No-go.
- **Narrow named-principal Ordering canary:** Conditional go after the exact owner principal/current exposure value are verified, target schema is confirmed at `20260720_0006`, and the environment/release checklist passes. PostgreSQL verification is complete; Ordering has no R2 dependency.

## Highest-value next milestone

Complete the **read-only Ordering owner canary** using the prepared checklist. Verify the production principal ID and existing feature strings, confirm the target schema/environment, expose only `ordering_intelligence_v2` to that individual owner, and collect natural read-only evidence without manufacturing records. Do not begin another feature milestone or broaden exposure until that evidence is reviewed.
