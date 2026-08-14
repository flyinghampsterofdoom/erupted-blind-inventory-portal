# V2 documentation index

Status date: 2026-08-14. Current release schema head: `20260814_0020`. Production revision must be confirmed before this Reporting/Replenishment release is deployed.

## Canonical current-state documents

- [Definitive data-source map](./data-source-map.md): mandatory provenance registry, source classifications,
  legacy/fallback audit, and the source-declaration gate for future V2 work.
- [Feature parity ledger](./v1-v2-feature-parity-ledger.md): legacy replacement and V2-native implementation status.
- [Release readiness report](./v2-release-readiness-report.md): objective architecture, infrastructure, testing, risk, and confidence assessment.
- [Recommended sequence](./v2-recommended-sequence.md): current roadmap and highest-value next milestone.
- [Technical debt register](./v2-technical-debt-register.md): authoritative intentional-debt backlog.
- [Test verification](./v2-test-verification.md): most recent available-suite result and skip classification.
- [Feature exposure contract](./v2-feature-exposure-and-cutover.md): implemented keys, dependencies, and disable limitations.
- [Schema and environment contract](./v2-schema-baseline-and-environment.md): migration chain and operational rules.
- [Deployment and rollback plan](./v2-deployment-and-rollback-plan.md), [release checklist](./v2-production-release-checklist.md), and [canary guide](./v2-canary-deployment-guide.md).

## Supporting implementation records

- [Exchanges and Returns](./exchanges-returns-v2-implementation.md)
- [Daily Store Logs](./daily-store-log-v2-implementation.md)
- [Store Operations completion dashboard](./store-operations-completion-dashboard.md)
- [Staff Scheduling foundation](./staff-scheduling-v2-foundation.md) and [weekly board](./staff-scheduling-v2-weekly-board.md)
- [Digital Signage](./digital-signage.md)
- [Unified Reporting Workbench](./reporting-workbench.md)
- [Customer Touchscreen](./touchscreen-flavor-finder.md)
- [Ordering V1 navigation bridge](./ordering-v2-milestone-plan.md)
- [Ordering, Purchasing, Receiving, Payment, and Replenishment](./ordering/README.md): current owner-canary implementation, completed evidence, and proposed phased design.
- [Ordering Phase 0 owner decisions](./ordering/phase-0-owner-decision-packet.md): all 12 calculation blockers resolved; Phase 1 planning is READY and runtime implementation still awaits approval.
- [Ordering Phase 1 implementation plan](./ordering/phase-1-implementation-plan.md): approved scope and acceptance gates, now implemented but not deployed.
- [Ordering Phase 1 implementation record](./ordering/phase-1-implementation-record.md): verified read-only runtime slice, tests, V1 parity, deviations, debt, and rollback impact; not deployed.
- [Navigation architecture](./v2-navigation-architecture.md), [store scope](./v2-store-scope-contract.md), [audit](./v2-audit-event-contract.md), [results](./v2-error-and-result-contract.md), and [statuses](./v2-status-contract.md)

## Historical and planning evidence

Documents named `v1-*`, `*-discovery`, `*-proposal`, `*-test-plan`, risk registers, and the product/UX blueprint preserve discovery or design evidence. They do not override current implementation status, current schema head, release readiness, or the technical-debt register. Module cutover records remain authoritative for explicit canonical-owner decisions; none currently records V1 retirement.
