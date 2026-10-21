# Ordering migration risk register

No defect in this register is fixed by discovery. Security-critical findings require owner review before implementation.

| Priority | Risk/defect | Evidence and consequence | Required planning response |
|---|---|---|---|
| Critical | Ordinary administration and Square writes share `management.admin` | Mapping, generation, delete, receiving adjustments, and emergency physical counts all use the same capability | Decide whether a dedicated Square-write approval is required |
| Critical | Emergency partial retry can replay successes | Partial failure leaves DRAFT; next push uses fresh UUID keys for every target | Do not migrate writer until replay semantics are explicitly fixed/tested |
| Critical | DB transaction and Square write are not atomic | Network succeeds before DB commit can fail; status/event may not reflect external result | Reconciliation and rollback design required |
| High | V1/V2 dual writes would corrupt ownership | PO, allocation, mapping, par, payment, and sync tables lack multi-writer protocol | V1-only writes until explicit cutover |
| High | No optimistic concurrency or row locks | Two editors can overwrite allocations, received quantities, mappings, pars, or payment fields | Characterize current last-write behavior; add concurrency gate before V2 write parity |
| High | Historical PDF is mutable/non-reproducible | IN_TRANSIT edits mark output stale; regeneration uses current template/mapping pack sizes and local filesystem | Initial V2 must preserve behavior; immutable artifact enhancement requires decision |
| High | Local PDF storage is instance-local | No object storage/shared volume/backup/cleanup | Do not make V2 canonical until storage/rollback is resolved |
| High | PDF regeneration from GET may not durably persist new path | Download route flushes but does not explicitly commit; missing/stale files can regenerate repeatedly | Verify transaction lifecycle and add a regression test before parity |
| High | Old PDF files can accumulate | Replacement generation does not remove prior files; only current referenced file is deleted with PO | Inventory generated files before storage migration |
| High | “Current,” “History,” and “Payments” do not exist as separate V1 resources | V2 navigation labels overstate current V1 architecture | Keep those children unavailable initially |
| High | Unused enum states and receipt tables may hold production history | No current code path proves they are safe to ignore | Read-only production distinct-value/row-count discovery before design |
| High | Mapping table mixes Square cache with local operating rules | Sync and manual edits share identifiers, cost, pack, MOQ, default, active | Field-level ownership and single writer required |
| High | Duplicate SKU resolution is order-dependent | Catalog-by-SKU keeps first response entry | Golden duplicate fixture and explicit ambiguity handling required |
| High | Stale mappings are not deactivated | Catalog absence/deletion is not reconciled | Do not assume active mapping means active Square item |
| High | Detail failures look like zero inventory | On-hand exceptions become empty map, then zero; no stale/error label | Parity must capture current behavior; enhancement should disclose unavailable data |
| High | Live Square data makes generation non-reproducible | Source responses and watermarks are not stored | Capture fixtures for tests; decide future snapshot evidence separately |
| High | Timezone semantics are UTC-date based | Sales history groups `closed_at` date without per-location timezone | Establish expected business-day behavior before changing math |
| High | In-transit orders remain editable | Allocations/costs/PDF may change after external vendor handling | Preserve until owner decides lock/version behavior |
| High | Hard delete is allowed for IN_TRANSIT | Cascades operational graph and best-effort file deletion; audit metadata only keeps ID | Do not alter without retention decision; test production expectations |
| High | Barcode cancellation subtracts one, not scan pack increment | A pack scan can add N units but cancel removes one | Owner review and defect test before receiving parity |
| High | Unexpected barcode creates synthetic product identity | `SKU::{barcode}::{uuid}` line may be sent only after real mapping/variation exists; current line lacks it | Preserve as known behavior; define correction path before rewrite |
| Medium | Dashboard and route permissions disagree | Capability override can allow route, but literal role filter hides dashboard card | V2 link matrix must use both visibility and route authorization |
| Medium | Stock Coverage uses literal ADMIN | MANAGER with normal Ordering access cannot use report handoff | Preserve and document; no silent permission widening |
| Medium | Payment display disappears after SENT_TO_STORES in overview | Template shows invoice only for IN_TRANSIT | Do not infer unpaid/paid history from overview |
| Medium | No payment history | Current fields overwrite prior values | Preserve; event ledger is enhancement |
| Medium | Template name does not select layout | Only disclaimer affects output layout | Do not promise configurable template formats |
| Medium | Mapping CSV is partially successful | Each valid row writes in one transaction while errors are accumulated | Preserve exact import semantics and error counts |
| Medium | Vendor sync can deactivate absent vendors | Temporary incomplete Square response could alter local availability | Reconciliation/fixture tests required |
| Medium | Square API clients lack retry/rate-limit policy | 429 and transient failures abort long synchronous requests | Keep manual behavior for parity; shared client is post-parity |
| Medium | Configured timeout is one hour | Requests can tie up worker capacity | Production-readiness capacity review |
| Medium | No background retry or reconciliation | Failed receive requires user action | Preserve operational runbook; automation is enhancement |
| Medium | Audit is incomplete/free-form | No mapping/par/PO before/after snapshots | Define minimum audit parity before writer migration |
| Medium | Ordering settings are lazily created | Calculation can write default singleton | Test/read ownership before read-only adapter |
| Low | No safe return parameter to V2 | V1 pages lead back to V1 Ordering/dashboard | Initial link bridge should rely on ordinary browser navigation |

## Security review threshold

No newly discovered unauthenticated route, credential exposure, or missing CSRF protection was found. The broad high-risk capability and emergency replay behavior are serious operational risks but are existing V1 behavior; they require owner review, not an unapproved discovery-phase code change.
