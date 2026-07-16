# V1 preservation is mandatory and module-specific

- Status: Accepted
- Date: 2026-07-16

## Context

V1 contains the production routes, workflows, shared data behavior, and Square integration relied on by current operations. V2 is being built incrementally under `/v2/...`. The existence or deployment of a V2 module must not be interpreted as global replacement, route cutover, data migration, or permission to retire V1.

## Decision

Adopt the [V1 Preservation Guarantee](../v2/v1-preservation-guarantee.md) as a non-negotiable architecture and cutover principle.

Every existing module defaults to **V1 canonical**. Canonical ownership advances only through a module-specific state record and written owner approval. V2 remains additive and independently exposed. Cutover to V2 canonical and retirement of V1 are separate approval events.

No global V1 shutdown, automatic redirect, automatic migration, destructive deployment, V1 dependency on V2, or silent cross-version substitution is permitted.

## Consequences

- V1 and V2 can run concurrently in one Render application.
- V1 routes, authorization, navigation, services, templates, data semantics, and integrations remain operational until separately approved.
- Feature exposure does not change canonical ownership.
- Shared-data plans require explicit read/write ownership and prohibit dual writes by default.
- Deployments must be reversible without V1 recovery work.
- Every parity ledger entry, module implementation record, cutover plan, and retirement proposal must name its canonical-owner state.
