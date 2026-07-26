# V2 release readiness report

Assessment date: 2026-07-25
Repository state assessed: local Ordering-owned Product Lifecycle catalog-identity correction; not committed, deployed, or newly exposed
Schema contract head: `20260725_0008`

## Executive assessment

V2 has a mature additive foundation and several implemented owner-preview modules, but the repository is **not ready for broad production exposure or any V1 cutover**. The complete suite passes against isolated PostgreSQL 16.12 and documentation is reconciled to current implementation. The Product Lifecycle catalog-identity correction is ready for owner implementation review, not deployment. Deployment confidence remains constrained by an unverified target environment, unverified real R2 behavior for media scopes, and partial disable semantics for device-facing Digital Signage and Touchscreen runtimes.

Phase 1 read-only Ordering Intelligence has since been implemented behind a disabled-by-default independent key. V1 behavior, feature parsing semantics, authorization, and canonical ownership remain unchanged. No deployment or production exposure has occurred.

## Architecture

The application remains one FastAPI/Jinja/PostgreSQL service with additive V2 routes, shared authentication, independent capability checks, server-resolved store scope, CSRF protection, structured audit envelopes, and environment-backed feature exposure. V1 routes and services remain directly available and do not depend on V2 navigation, Current Store, or exposure.

The schema is a linear Alembic chain from immutable V1 baseline `20260715_0001` through Daily Store Logs, Staff Scheduling, Store Shifts, Digital Signage, Customer Touchscreen, Ordering lifecycle, and the additive Ordering-owned catalog identity tables to `20260725_0008`. Startup validates the exact supported revision and performs no schema mutation.

## Infrastructure

| Area | State | Evidence or gap |
|---|---|---|
| Repository | Verified checkpoint candidate | Focused lifecycle changes are local, undeployed, and unpushed |
| Schema tooling | Implemented | Baseline, validation, stamping, compatibility profile, additive upgrade, startup check |
| Target database | Unverified | Production revision and migration readiness remain a deployment gate; no production inspection or write occurred |
| PostgreSQL integration | Verified locally | Full suite: 280 passed, 0 failed, 1 optional real-R2 skip; includes `20260725_0008` migration, constraints, 824-mapping workspace, concurrency, audit, and rollback |
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
- Ordering lifecycle foundation and integration: sparse global states, owner capability, audited bulk transitions, archived recovery, archive pre-filtering, and No Future Reorder no-quantity policy.
- Locally implemented, undeployed Ordering catalog-identity correction: database-only lifecycle GETs, explicit owner bulk catalog refresh, completeness/freshness status, unknown-name handling, and strict separation from Customer Touchscreen caches.

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
7. Ordering Intelligence uses synchronous live Square reads without a last-known-good cache, durable read model, module timeout budget, or built-in metrics (TD-026). This blocks trustworthy long-horizon Stagnant Inventory last-sale evidence.
8. Generic `alembic check` cannot currently serve as the no-drift gate because legacy baseline/model differences include a PostgreSQL JSON default comparison failure (TD-027); the versioned upgrade and schema-contract suite remain the current gate.
9. The approved Stagnant Inventory cost fallback lacks a trustworthy “most recent valid purchase cost” source contract; values remain `Unknown` when preferred configured cost is unavailable until TD-028 is resolved.

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

## Ordering lifecycle planning addendum

Owner review approved all 15 Inventory Lifecycle, Ordering workspace, and Stagnant Inventory policy decisions: 12 approved and three approved with modification. `ORD-DEC-028` through `ORD-DEC-036` are recorded in the Ordering decision register. No runtime implementation, migration, exposure, or deployment is authorized by those decisions.

- Lifecycle foundation: ready for implementation approval.
- Ordering lifecycle integration and before/after performance measurement: ready for implementation approval.
- Full workspace UX: policy-ready but outside the focused implementation scope.
- Durable read-model work: technically open under TD-026.
- Stagnant Inventory: blocked by TD-026 and TD-028.
- Automatic archive: disabled and separately gated.

This planning status does not alter V1 ownership, current feature exposure, or the production release decision below.

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
- **Existing read-only Ordering canary:** Remains independently deployed on its prior schema-compatible release. The lifecycle checkpoint is ready for owner canary approval; deployment still requires target inspection, migration approval, reverified owner identity, and a separately approved owner-only capability grant. Ordering has no R2 dependency.

## Highest-value next milestone

Approve and prepare the **owner-only Inventory Lifecycle canary**. Reverify the existing owner principal and target prior revision, deploy the additive migration only after approval, preserve existing feature exposure, and grant only that principal `ordering.lifecycle.manage`. Empty-lifecycle parity is already verified; repeat performance diagnostics only after the owner archives real products. Do not broaden exposure.
