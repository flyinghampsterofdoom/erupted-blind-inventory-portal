# V1 to V2 feature parity ledger

Status date: 2026-07-22. This ledger describes the repository at schema head `20260720_0006`. It is the canonical implementation-status summary; detailed V1 discovery records remain authoritative for legacy behavior.

The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) still applies. Every legacy capability remains V1 canonical unless a module-specific cutover record contains explicit owner approval. No V1 module is retired. Percentages are engineering estimates of replacement readiness, including implementation, parity evidence, deployment validation, and cutover preparation; they are not project accounting or owner approval.

## Section A - Legacy Replacement

| V1 module | Current V1 status | V2 replacement status | Remaining work | Notes | Completion |
|---|---|---|---|---|---:|
| Authentication and sessions | Active and canonical | Existing authentication reused; V2 per-person actor contract implemented | Account rollout, shared-principal transition, production session validation | V2 does not migrate principals automatically | 45% |
| Security shell | Active and canonical | V2 shell, CSRF, security headers, responsive navigation, and access-denied UX implemented | Session-expiry UX parity and browser/visual regression evidence | V1 shell remains independent | 60% |
| Store home | Active and canonical | V2 Store Operations landing and Current Store context implemented | Replace each aggregated workflow status and validate parity | Only Daily Store Log status is V2-native | 30% |
| Management dashboard | Active and canonical | V2 Overview shell exists | Implement live attention queues and configurable dashboard parity | Current Overview is not a V1 dashboard replacement | 15% |
| Dashboard configuration | Active and canonical | Planned | Define V2 experience/navigation administration and migrate configuration safely | V1 GET paths may seed defaults | 0% |
| Rotating blind inventory counts | Active and canonical | Planned | Rebuild generation, autosave, submission, variance, locking, recount, and Square behavior | High-risk state machine | 0% |
| Full management store count | Active and canonical | Planned | Rebuild drafts, expected quantities, exports, submission, and Square push | Separate from rotating counts | 0% |
| Count groups and campaigns | Active and canonical | Planned | Configuration UI, rotation parity, campaign sync, and coverage audit | Ordering navigation does not cover this module | 0% |
| Recount closeout and count sync reporting | Active and canonical | Planned | Characterize transitions, idempotency, retries, audit, and reports | Defer Square writes until integration gate | 0% |
| Non-sellable stock | Active and canonical | Planned | Draft/submission/unlock/catalog parity and store-par dependency tests | Shared with replenishment | 0% |
| Change box counting | Active and canonical | Planned | Preserve denomination semantics, current-state synchronization, history, and delete policy | Cash ownership decision required | 0% |
| Change forms | Active and canonical | Planned | Preserve immutable forms and current inventory mutation atomically | Feeds master-safe reporting | 0% |
| Change box audit | Active and canonical | Planned | Rebuild audit/history and current-inventory replacement contract | Admin-only legacy workflow | 0% |
| Master safe | Active and canonical | Planned | Define singleton ownership, par/current state, audit, and reporting parity | Must move with cash domain | 0% |
| Store par reset and delivery | Active and canonical | Planned | Reconcile cash and non-sellable ownership; validate cross-domain transaction and rollback | Deferred until both dependencies are ready | 0% |
| Cash reconciliation | Active and canonical | Planned | Snapshot policy, Square fixture parity, verification history, and batch UI | Expected cash is currently recomputed live | 0% |
| Daily chores | Active and canonical | Coming Later navigation only | Rebuild task templates, draft/autosave/restart/delete/submit, and audit | Daily Store Logs do not replace chores | 5% |
| Opening checklists | Active and canonical | Planned | Rebuild hierarchy, defaults, submission, and management audit | Lazy V1 initialization must be characterized | 0% |
| Customer requests | Active and canonical | Coming Later navigation only | Decide catalog/count semantics; implement submit/history/admin parity | No V2 route exists | 5% |
| Exchanges and Returns | Active and canonical | V2 submit/history/detail implemented behind `exchanges_returns_v2` | PostgreSQL route verification, production parity evidence, canary, owner cutover decision | Shares append-only V1 table; no redirect | 80% |
| Employee logs | Active and canonical | Planned | Rebuild entries/history, lead visibility, category snapshots, and authorization | Separate from scheduling employee rows | 0% |
| Ordering dashboard and generation | Active and canonical | Phase 1 read-only Ordering Intelligence implemented behind `ordering_intelligence_v2`; V1 bridge and all operational generation remain canonical | PostgreSQL/canary validation; later merchandising controls and PO lifecycle | [Phase 1 record](./ordering/phase-1-implementation-record.md); no V2 write or PO creation | 20% |
| Vendor and SKU mappings | Active and canonical | V1 navigation bridge only; data ownership and architecture documented | Production data profile, owner decisions, V2 configuration workflow, import/sync parity | Critical shared reference data; identity ambiguity confirmed | 5% |
| Par levels and ordering settings | Active and canonical | Approved zero/null/manual-lock interpretation implemented read-only; V1 bridge remains | V2 merchandising-decision/configuration writes remain later scope | Existing V1 page remains canonical | 10% |
| Purchase order editing and PDF | Active and canonical | Proposed architecture only | Immutable approval snapshot, lifecycle, edit parity, PDF semantic/golden tests, durable file policy | Current PDF is mutable and not historically reproducible; no V2 PO route exists | 0% |
| Receiving and store allocation | Active and canonical | Proposed local-first architecture only | Explicit receipt/disposition model, duplicate protection, then separately approved Square gateway | Current workflow is non-atomic across PostgreSQL/Square; no V2 route exists | 0% |
| Emergency on-hand editor | Active and canonical | Planned | Common Square write gateway, dry run, idempotency, and reconciliation | Deliberately deferred | 0% |
| Reports hub | Active and canonical | V2 Reports navigation placeholders exist | Implement catalog, permissions, destinations, and export behavior | Placeholders are not report parity | 5% |
| Sales and COGS reports | Active and canonical | Discovery/architecture complete; no V2 implementation | Approve valuation/recognition policy; captured Square fixtures, versioned cost evidence, filters, exports | V1 recomputes historical COGS using current preferred cost | 0% |
| Inventory analytics reports | Active and canonical | Phase 1 store-level velocity, stockout evidence, and recommendation explanations implemented read-only | Remaining value/demand/coverage report parity, exports, snapshot/cache policy | V1 formulas remain unchanged and Coverage can still create V1 POs | 20% |
| Count and admin reports | Active and canonical | Planned | Historical projections, permission normalization decision, exports, and audit parity | Depends on count domain | 0% |
| Users and store credentials | Active and canonical | Planned | Individual-account administration, credential flows, authorization review | V2 assumes individual actors but has no admin UI | 0% |
| Access control | Active and canonical | Effective permissions reused by V2; V2 administration planned | Full role/override UI and formal privilege-regression review | Navigation permission remains separate from authorization | 35% |
| Audit logging | Active and canonical | V2 audit envelope and writer implemented | Generic viewer, retention policy, completeness review, external outcome conventions | Existing `audit_log` storage is reused | 45% |
| Square integration foundation | Active and canonical | Touchscreen cache plus an Ordering-specific allowlisted read gateway implemented; no V2 write gateway | Consolidated transport, read caching/retries/observability, and later dry-run/idempotent writes | Ordering gateway is read-only; `SQUARE_READ_ONLY` is not universal V1 protection | 30% |
| System setup and schema administration | Active and canonical | Alembic baseline, additive revisions, schema validation, and safe seed policy implemented | Execute PostgreSQL integration suite and validate target environment at `20260720_0006` | Runtime startup validates and does not mutate schema | 75% |
| Audit queue placeholder | Active V1 placeholder | No V2 capability | Usage evidence and explicit retain/consolidate/retire decision | No function exists in V1 | 0% |

## Section B - New Platform Capabilities

These rows distinguish V2-native capabilities from legacy replacement. “Implemented” means code exists in this repository; it does not mean globally exposed, production-validated, or cut over.

| V2-native capability | Status | Exposure or boundary | Remaining work | Completion |
|---|---|---|---|---:|
| V2 shell and navigation | Implemented | Authenticated `/v2/*`; implemented children remain permission/feature filtered | Visual/browser regression and production smoke evidence | 80% |
| Current Store context | Implemented | Used by `daily_store_logs_v2` employee workflow | Assignment model decision and canary validation | 80% |
| Daily Store Logs | Implemented | `daily_store_logs_v2`, disabled by default | PostgreSQL route suite, canary, operational owner sign-off | 85% |
| Exchanges and Returns | Implemented legacy replacement slice | `exchanges_returns_v2`, disabled by default | Parity reconciliation, canary, and cutover decision | 80% |
| Staff Scheduling | Implemented management foundation and weekly board | `staff_scheduling_v2`, disabled by default | PostgreSQL suite; time-off/configuration/self-service/month views remain out of scope | 70% |
| Store Shift Templates | Implemented as reusable Store Shifts | Shares `staff_scheduling_v2` | PostgreSQL suite and owner workflow validation | 80% |
| Digital Signage | Implemented management and authenticated display player | Admin: `digital_signage_v2`; device player uses display credentials | PostgreSQL/R2 verification; add a true runtime kill switch | 75% |
| Customer Touchscreen | Implemented management, device authentication, catalog, and Square read cache | Admin: `touchscreen_v2`; customer runtime uses device credentials | PostgreSQL/R2/Square-cache validation; add a true runtime kill switch | 75% |
| Ordering V1 navigation bridge | Implemented compatibility bridge | `ordering_v1_links_v2`, disabled by default | Keep labeled as Existing V1 until replacement exists | 100% of bridge scope |
| Camera Integration | Planned | No route, schema, flag, or service | Product, privacy, retention, security, and integration design | 0% |
| Intelligent Ordering | Phase 1 read-only capability implemented | `ordering_intelligence_v2`, disabled by default; effective `management.admin`; no writes | Implementation review, PostgreSQL/canary evidence; later controls/drafts remain separate | 30% |
| Budget and Cash Flow | Planned | No route, schema, flag, or service | Product scope, accounting sources, authorization, and audit design | 0% |
| Advanced Reporting | Planned | Navigation placeholders only | Reporting engine, fixtures, exports, authorization, and snapshots | 0% |
| Employee Administration | Planned | No V2 administration route | Identity linkage, account lifecycle, roles, employee/category administration | 0% |
| Inventory Counts | Planned | Navigation placeholder only | Full count/recount design and external-write safety | 0% |
| Receiving | Architecture complete; implementation planned | No V2 route | Implement local-only receipts/dispositions first; Square gateway only after separate approval | 0% |
| Purchasing | Architecture complete; implementation planned | V1 bridge only | Implement V2 drafts, immutable approvals/PDFs, coexistence guards, and owner-approved rules | 0% |
| Cash Management | Planned | Navigation placeholders only | Unified ownership of change box, master safe, reconciliation, and replenishment | 0% |

## Interpretation rules

- `Coming Later` and navigation placeholders are 0-5% and never count as implemented business behavior.
- Feature exposure is independent from permission and canonical ownership.
- New capabilities may be useful without replacing V1, but still require infrastructure verification and a controlled rollout.
- The [technical debt register](./v2-technical-debt-register.md) is the authoritative list of intentionally deferred engineering work.
- Release evidence and confidence are tracked in [V2 release readiness](./v2-release-readiness-report.md).
