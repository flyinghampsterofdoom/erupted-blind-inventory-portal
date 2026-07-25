# V2 Ordering discovery and architecture index

Status date: 2026-07-25. Status: Phase 1 read-only Ordering Intelligence is implemented and live in a principal-scoped owner canary. V1 remains canonical for every operational Ordering workflow. Inventory Lifecycle Phase 1 and Ordering Integration Phase 2 are implemented and PostgreSQL-verified locally at schema head `20260725_0007`; they are ready for owner lifecycle canary approval but are not deployed or newly exposed.

Phase 0 owner-decision readiness is **READY** and the approved Phase 1 slice is implemented behind disabled-by-default `ordering_intelligence_v2`. See the [owner decision packet](./phase-0-owner-decision-packet.md), [Phase 1 policy baseline](./phase-1-policy-baseline.md), [blocker matrix](./phase-1-blocker-matrix.md), [implementation plan](./phase-1-implementation-plan.md), [verified implementation record](./phase-1-implementation-record.md), and [owner-canary checklist](./phase-1-owner-canary-checklist.md).

Labels used throughout:

- **Confirmed**: directly evidenced by current routes, services, models, templates, schema, or tests.
- **Inferred**: strongly suggested but not proven against production data or operations.
- **Proposed**: future V2 design requiring an approved implementation milestone.
- **Unresolved**: owner or policy decision required; discovery does not choose it.

## Deliverables

- [V1 Ordering discovery](./v1-ordering-discovery.md)
- [Current-domain data map](./current-domain-data-map.md)
- [Workflow and state-machine map](./workflow-state-machines.md)
- [Square integration audit](./square-integration-audit.md)
- [Purchase-order PDF audit](./purchase-order-pdf-audit.md)
- [Receiving audit](./receiving-audit.md)
- [Vendor payment and COGS audit](./vendor-payment-cogs-audit.md)
- [Business-rule decision register](./business-rule-decision-register.md)
- [Proposed V2 architecture](./proposed-v2-architecture.md)
- [Recommendation-engine specification](./recommendation-engine-specification.md)
- [Phased implementation roadmap](./phased-implementation-roadmap.md)
- [Coexistence and cutover strategy](./coexistence-cutover-strategy.md)
- [Testing strategy](./testing-strategy.md)
- [Security and authorization review](./security-authorization-review.md)
- [Risk register](./risk-register.md)
- [Phase 0 owner decision packet](./phase-0-owner-decision-packet.md)
- [Proposed Phase 1 policy baseline](./phase-1-policy-baseline.md)
- [Phase 1 blocker matrix](./phase-1-blocker-matrix.md)
- [Phase 1 implementation plan](./phase-1-implementation-plan.md)
- [Phase 1 implementation record](./phase-1-implementation-record.md)
- [Proposed inventory lifecycle, Ordering workspace, and stagnant inventory design](./inventory-lifecycle-and-stagnant-inventory-design.md)
- [Inventory lifecycle owner decision packet](./inventory-lifecycle-owner-decision-packet.md)
- [Inventory lifecycle implementation blocker matrix](./inventory-lifecycle-blocker-matrix.md)
- [Inventory lifecycle Phase 1/2 implementation plan](./inventory-lifecycle-phase-1-2-implementation-plan.md)
- [Inventory lifecycle Phase 1/2 implementation record](./inventory-lifecycle-phase-1-2-implementation-record.md)

Existing root-level Ordering documents remain supporting evidence, especially [V1 discovery](../ordering-v1-discovery.md), [data ownership](../ordering-data-ownership-map.md), [state record](../ordering-lifecycle-state-record.md), [Square authority](../ordering-square-source-of-truth.md), [permissions](../ordering-permission-matrix.md), and [migration risks](../ordering-migration-risk-register.md). This directory consolidates and extends that evidence for the full Ordering/Purchasing/Receiving/Payment/Replenishment domain.
