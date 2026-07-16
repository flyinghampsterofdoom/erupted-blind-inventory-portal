# V1 Preservation Guarantee

## Non-negotiable guarantee

> V1 remains fully operational and canonical until the owner explicitly approves cutover for a specific module. V2 is additive. Cutover occurs per module. Retirement is a separate decision. Nothing in V1 is removed or disabled without explicit owner approval.

This guarantee applies to application behavior, routes, templates, services, navigation, authentication, data, integrations, migrations, deployment, rollback, and operational support.

## Canonical-owner states

Every existing business module has one explicit canonical-owner state:

1. **V1 canonical** — default for every existing tool. V1 owns the production workflow and behavior. V2 may be absent or local-only.
2. **V1 canonical with V2 shadow/read-only** — V2 may read approved sources or compare results, but V1 remains the only production writer and user-facing authority.
3. **V1 and V2 side by side** — both versions are directly available under their own routes. Writer ownership and record-version ownership must be explicit; dual writes are prohibited by default.
4. **V2 canonical after explicit approval** — V2 becomes the approved module owner only after the cutover gate passes and written owner approval is recorded. V1 remains present and directly recoverable.
5. **V1 retired after explicit approval** — V1 routes/code/navigation may be retired only through a second, separate written owner decision after V2 cutover and observation.

No implementation milestone, feature exposure, deployment, parity claim, or existence of a V2 route advances a module automatically. Missing state documentation means **V1 canonical**.

## Global prohibitions

There is no global V1 shutdown or application-wide cutover. Without module-specific written approval, do not:

- disable V1 or remove/redirect V1 routes
- replace V1 templates or services
- block existing V1 navigation or workflows
- make V1 depend on V2 availability, V2 session context, or V2 feature exposure
- require V2 for authentication or shared-data access
- silently substitute V2 when V1 fails
- describe a V1 module as deprecated merely because V2 exists

V1 failures remain visible as V1 failures. V2 failures, disabled features, or missing Current Store context must not trap users or prevent direct V1 access.

## Side-by-side route rules

- Existing V1 routes retain their paths and authorization.
- New V2 routes remain under `/v2/...`.
- V2 exposure is independently controlled per module and disabled by default unless specifically approved.
- No automatic redirects occur between versions.
- A V2 navigation item may deep-link to an unchanged V1 tool when no V2 replacement is approved. The label must not imply migration or V2 ownership.
- Keeping a V1 link in V2 does not permit removal of the original V1 navigation/access path.
- In-progress drafts, sessions, orders, or records remain pinned to the version that owns them unless an explicit migration plan says otherwise.

## Shared-data and integration safeguards

Every module plan must document:

- V1 read/write ownership before, during, and after cutover
- V2 read/write behavior
- shared tables, files, queues, current-state balances, and immutable facts
- external reads/writes and Square authority
- record/version ownership and reconciliation

V2 may read approved V1-owned data, but reading does not transfer ownership. Dual writes are prohibited by default. V2 must not change V1 table semantics, V1 Square behavior, historical V1 rows, integration idempotency, or current-state ownership without explicit approval. Ordinary V2 deployment performs no production backfill, historical rewrite, deletion, or destructive migration.

Square remains authoritative for catalog/SKU identity and sales unless a separate approved architecture decision changes that boundary.

## Deployment and rollback guarantee

Deploying code containing V2 must not activate, replace, or retire V1.

- V2 feature keys default to disabled.
- Schema revisions are additive unless a separate destructive-change approval exists.
- Deployment performs no automatic V1-to-V2 redirect, data migration, or backfill.
- V2 startup/exposure failure must not make V1 unusable.
- Application rollback restores the prior application version without requiring reconstruction or recovery of V1 routes, templates, services, data, navigation, or integrations.
- A rollback may disable V2 exposure first, but V1 recovery must not depend on V2 cleanup.

## Module cutover gate

Before a module may move to **V2 canonical after explicit approval**, all of the following are required:

- V1 discovery complete
- parity requirements documented
- V2 implementation complete
- automated tests passing
- browser verification complete
- production-readiness review complete
- data-write ownership decision recorded
- migration plan approved
- rollback plan rehearsed
- explicit written owner approval

Cutover approval does not approve V1 retirement. Retirement requires a later separate decision with observation evidence, route/client usage review, support readiness, and rollback-window completion.

## Governance

The parity ledger and each module cutover record must show the current canonical-owner state. Any proposal to change state must name the owner, approval date, evidence, writer ownership, route plan, migration plan, and rollback plan.

This policy can be changed only by an explicit architecture decision and written owner approval. Silence, code merge, deployment, or elapsed time is not approval.
