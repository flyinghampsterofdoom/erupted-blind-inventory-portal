# Recommended V2 sequence

This sequence is dependency-driven. It does not authorize business-module implementation or schema changes.

The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) governs every milestone. Sequence order does not authorize V1 cutover, redirects, canonical-owner changes, or retirement.

## Sequencing principles

1. Preserve V1 as the system of record until each module passes a parity gate.
2. Build read-only projections before writes and external side effects.
3. Do not change permission semantics incidentally while moving routes.
4. Characterize current data and outputs before cleaning schema or consolidating modules.
5. Put Square writes behind one observable/idempotent boundary before moving any write workflow.
6. Require an individual authenticated employee account and actor attribution for every V2 operational event; preserve shared V1 principals until a separately approved account cutover.

## Recommended milestones

| Milestone | Scope | Why this order | Exit/decision gate |
|---|---|---|---|
| 2 (current) | Discovery inventory only | Establishes implementation ground truth | Stakeholders confirm module ownership, risks, and “unused” candidates |
| 3 | Foundation contracts: route registry, individual-account actor contract using existing auth, effective-permission matrix, store-scope contract, audit/event vocabulary, error/observability baseline | Every module depends on person identity, stores, permission, and audit | Automated ADMIN/MANAGER/LEAD/STORE + overrides matrix matches V1; shared-principal compatibility documented; no production user/data migration |
| 4 | Read-only operational history: exchange/return list/detail, customer-request history, chore/checklist audit, stored cash verification batches | Mostly append-only/isolated, proves layout/filter/table patterns without writes or Square | Row counts/filters/details match production snapshots; permission and store scope approved |
| 5 | Read-only count/session history and integration health | High-value but no count mutation yet; exercises snapshots/entries/sync events | Session detail/variance/export golden tests; recount semantics documented and accepted |
| 6 | Read-only reporting engine with captured Square fixtures: sales/COGS first, then inventory value/velocity/targeted demand/stock coverage | Safest useful Square work is read-only; shared report engine can be tested deterministically | V1/V2 output comparison within agreed rounding/timezone tolerance; cost/mapping snapshot decision |
| 7 | Independent append-only store forms: exchange/return, customer requests, opening checklist; then daily chores | Lower integration risk; introduces writes and autosave progressively | Dual-run/record comparison, audit event parity, recovery/duplicate-submit tests |
| 8 | Employee logs and user/access administration | Depends on mature permission framework; security-sensitive | Formal authorization review; override/navigation/backend tests; no privilege regression |
| 9 | Cash/change domain foundation: current-state ownership, change box count/forms/audit, master safe | Must be treated as one data ownership domain | Reconciliation of every current balance to history; denomination and null/zero decision |
| 10 | Non-sellable stock, then store par reset/delivery | Non-sellable alone is moderate; par delivery waits for cash/change and non-sellable readiness | Cross-domain transaction/failure tests; delivery reconciliation runbook |
| 11 | Ordering reference data: vendors, SKU/GTIN mappings, ordering settings, pars, PDF templates | Required foundation before transactional orders; provides cleanup checkpoint | Production distinct-value/orphan audit; mapping completeness thresholds; schema decision |
| 12 | Purchase-order read/edit/PDF without Square receiving | Rebuild durable order snapshot and status rules before side effects | State transition tests and PDF golden tests; disposition of receipt tables/statuses decided |
| 13 | Order generation and report→order creation | Depends on validated analytics and order model | Recommendation parity fixtures; exact selected-row snapshot validation |
| 14 | Square write gateway; PO receiving first | Receiving has best existing deterministic idempotency and event records | Failure-injection, retry, partial-success reconciliation, operator runbook |
| 15 | Admin full count, rotating counts/recount, emergency on-hand | Highest-risk Square writes/state machines; rotating count last | Staged cutover, external write lock, complete state-transition and reconciliation evidence |
| 16 | Redirect/cutover and candidate retirement review | Only after module parity | Route redirect matrix, telemetry/usage evidence, rollback rehearsal, stakeholder sign-off |

## Safest first read-only module

The recommended first slice is **exchange/return management list/detail**, followed by customer-request history:

- append-only local facts;
- no live Square dependency;
- simple store/date filters;
- existing `management.access` protection;
- no current-state mutation during reads;
- easy row/detail parity comparison.

Cash verification history is also useful but should initially read only stored batches; live expected calculation belongs with the later integration/report slice.

## Modules that can be rebuilt relatively independently

- Exchange/return forms.
- Opening checklist after default-template/versioning decision.
- Daily chores after task-template propagation semantics are characterized.
- Employee logs after permission/lead-visibility foundation.
- Read-only reports using captured Square fixtures.
- Customer request history; defer aggregate item-count editing until semantics are decided.

## Modules requiring prior schema/data cleanup decisions

- Ordering: vendor mappings, current-versus-historical cost, global/store par uniqueness, unused receipt/contact/email structures.
- Cash/change: shared writers to current inventory, denomination text codes, global master-safe singleton semantics.
- Counts: cyclic forced-count/session FK, two separate count systems, sync-event polymorphism.
- Access: literal-role checks versus capability overrides and cosmetic custom roles.
- Audit: free-form action/metadata/sync types and retention.

Do not perform cleanup before production distinct-value, orphan, and row-count audits. Compatibility views/adapters may be safer than early replacement.

## Modules to defer

1. Emergency on-hand Square writes.
2. Rotating counts and automatic recount closeout.
3. Full store count Square submission.
4. Store par reset delivery.
5. Purchase-order receiving/retry.
6. User/access-control writes until permission characterization is automated.

## Required decision gates

- **Authorization gate:** preserve literal role inconsistencies or normalize to capability checks?
- **Store identity gate:** authoritative store source and handling of local/non-Square stores.
- **Historical value gate:** snapshot catalog names, vendor mapping, costs, prices, expected cash, and team identities or continue live recomputation?
- **Data ownership gate:** single writers for current change-box/non-sellable state.
- **Ordering model gate:** disposition of unused receipt tables/statuses/contact/email fields.
- **Integration gate:** common Square client, idempotency semantics, retry limits, observability, and dry-run control.
- **Migration gate:** production schema baseline and versioned migration strategy.
- **Cutover gate:** redirect inventory, active-session/draft handling, external-write freeze, reconciliation, rollback.

Every cutover gate is module-specific and also requires written owner approval. Approval for V2 canonical ownership does not approve V1 retirement.

## Cutover ordering notes

- Keep legacy paths stable and directly accessible until a V2 destination has parity and the owner explicitly approves that module’s cutover.
- Default to no redirects. Any approved read-route redirect must preserve query parameters and export filenames.
- Do not split an in-progress draft/session/PO between versions. Route existing IDs/drafts to their owning version until completion or explicit migration.
- Never run V1 and V2 Square writers for the same command without a shared idempotency record.
