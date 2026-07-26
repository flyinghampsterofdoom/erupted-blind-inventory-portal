# Recommended V2 sequence

Status date: 2026-07-25. This roadmap begins from the repository's actual state at schema head `20260725_0009`. It does not authorize deployment, V1 cutover, redirects, external writes, destructive migration, or V1 retirement.

The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) governs every phase. Implementation status is canonical in the [feature parity ledger](./v1-v2-feature-parity-ledger.md), and intentional deferrals are canonical in the [technical debt register](./v2-technical-debt-register.md).

## Current baseline

| Capability | Current repository status |
|---|---|
| V1 discovery and preservation contracts | Complete enough to govern current work; V1 remains canonical |
| V2 foundation | Implemented: shell, navigation, exposure, permissions, scope, audit, result/status contracts, Alembic baseline |
| Exchanges and Returns | Implemented behind `exchanges_returns_v2`; no approved cutover |
| Daily Store Logs | Implemented behind `daily_store_logs_v2`; new capability, not a V1 replacement |
| Staff Scheduling and Store Shifts | Management weekly board and reusable definitions implemented behind `staff_scheduling_v2` |
| Digital Signage | Management and credentialed player implemented; infrastructure verification and runtime kill-switch debt remain |
| Customer Touchscreen | Management, device application, and Square read cache implemented; infrastructure verification and runtime kill-switch debt remain |
| Ordering | Read-only intelligence is live in an owner canary; lifecycle foundation/integration is implemented and PostgreSQL-verified locally; owner lifecycle canary approval is next, while the independent V1 bridge remains and V1 owns all operational actions |
| Other V1 replacement domains | Planned or represented by Coming Later navigation only |

## Required next phase: controlled canary readiness

This is the single highest-value milestone after documentation hardening because it converts repository confidence into operational evidence without adding another feature.

1. Review and approve the owner lifecycle canary checkpoint; local PostgreSQL verification is complete.
2. Validate the intended deployment environment, target prior revision, secrets, cookie policy, schema check, logging, and rollback target.
3. Reverify the existing owner principal and preserve principal-scoped `ordering_intelligence_v2` exposure.
4. Deploy the additive migration only after approval, then grant only that owner `ordering.lifecycle.manage` without a role fallback.
5. Complete the [production release checklist](./v2-production-release-checklist.md) and execute the [canary guide](./v2-canary-deployment-guide.md).
6. After the owner archives real products, repeat the baseline diagnostic and reconcile records, permissions, audit events, owner feedback, and rollback evidence before expanding exposure.

Recommended first operational canary: **Exchanges and Returns** for management read/history first, followed by an explicitly approved append-only submission. It has an existing V1 comparison surface, no Square or R2 dependency, and a bounded rollback path.

## Subsequent dependency sequence

| Order | Milestone | Why it follows | Exit gate |
|---:|---|---|---|
| 1 | Feature-exposure hardening | Digital Signage and Touchscreen lack full runtime kill switches | Admin and device runtimes independently disable; configuration validation is observable |
| 2 | Individual identity and store assignment | Shared principals and all-active management scope limit trustworthy self-service | Approved multi-store assignment model, linked employee accounts, authorization regression suite |
| 3 | Read-only history and reporting foundation | Produces useful parity evidence without external writes | Captured fixtures, snapshot policy, deterministic V1/V2 comparison, export parity |
| 4 | Store procedure replacements | Chores, opening checklists, customer requests, and employee logs are locally bounded | Field/state/audit parity, duplicate/recovery tests, module canaries |
| 5 | Cash Management foundation | Change box, master safe, reconciliation, and replenishment share current-state ownership | Reconciled current state/history, denomination/null decisions, failure runbook |
| 6 | Inventory and non-sellable reads | History/configuration can precede mutation | Count/session/report parity and accepted recount semantics |
| 7 | Purchasing reference data and PO lifecycle | Ordering needs stable vendors/mappings/pars before transactions | Production data audit, state model, PDF golden tests, durable snapshots |
| 8 | Common Square gateway and Receiving | First controlled V2 external write boundary | Idempotency, retry, partial failure, dry run, reconciliation, operator runbook |
| 9 | Inventory count and emergency Square writes | Highest-risk state machines follow a proven gateway | Full transition tests, external write lock, staged cutover and reconciliation |
| 10 | Per-module cutover and retirement review | Cutover occurs only after parity evidence | Route matrix, owner approval, rollback rehearsal; retirement decided separately |

## Decision gates retained

- Authorization semantics and privilege normalization.
- Authoritative employee/store assignments and inactive/non-Square store policy.
- Historical snapshot policy for catalog, costs, mappings, expected cash, and team identities.
- Single writers for change-box, master-safe, and non-sellable current state.
- Ordering model and unused receipt/status/contact fields.
- Common Square client, idempotency, retry, observability, and dry run.
- Production schema validation and additive migration procedure.
- Module-specific cutover, observation, rollback, and separate V1 retirement approval.

## Explicitly deferred

- Emergency on-hand and count Square writes.
- Rotating recount closeout migration.
- Store par reset delivery.
- Purchase-order receiving and retry.
- Employee self-service until identity linkage and store assignment are approved.
- Camera Integration, Budget and Cash Flow, and other new platform systems until current release and canary evidence are complete.
