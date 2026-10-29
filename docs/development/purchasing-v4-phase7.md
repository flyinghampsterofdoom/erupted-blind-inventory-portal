# Purchasing V4 Phase 7 — isolated receiving

This development module is not mounted in the production application. It uses an explicit loopback disposable PostgreSQL database and has no live Square transport by default. No production migration, receiving, deployment, new cron, vendor delivery, or subsequent phase is authorized.

## Workflow

Open an existing V4 purchase order and select **Receive**. That confirmation is the split workflow. HWY 99 is fixed; no receiving-location setting exists. Actual quantities and all splits are individual units. Original pack sizes and approved allocations remain visible. **Save** retains an actor-scoped draft only. **Save & Submit** commits an immutable receipt decision, then starts bounded inventory work outside the database transaction.

A full receipt uses the original approved store split automatically. A shortage without an owner-approved split stays unassigned at HWY 99. The owner adjusts only changed prefilled quantities in the same confirmation. There is no separate dispatch screen or destination arrival acknowledgment. Successful inventory posting does not attest a later physical arrival.

Unknown actual quantities remain blank. Damaged quantities are explicitly reviewed. Owner exceptions handle wrong products, damaged goods, overages with an explicit separate valuation, verified corrections and closing an outstanding commercial balance. No supplier identity, Square variation, numerical policy, or financial valuation is fabricated. Original PO costs and financial totals are immutable.

## Integrity and supply

Private additive revisions p7_0001 through p7_0008 retain the accepted foundation. The receiving order pins canonical PO lines and original store/location identities. Commands have an immutable UUID, payload digest, actor and expected receiving version. Database guards enforce conservation, nonnegative custody, predecessor verification and matching remote evidence. Reusing an identity with different input fails. V3 purchase orders are not automatically adopted or replayed.

The inventory worker cannot authorize purchases or read vendor quotation secrets. It durably records IN_FLIGHT before making a remote call, with no network activity while holding receipt locks. A successful response or inventory count alone does not establish completion. Exact adjustment references, variation, locations, states, units and timestamps must match. Ambiguous results remain visible and retain the original identity; no compensating inventory adjustment is invented.

The pinned Square contract is 2026-09-16. Good goods enter IN_STOCK at HWY 99; each outward split uses a native cross-location IN_STOCK adjustment. Damaged goods use one atomic two-change batch to receive and quarantine them in WASTE. No IN_TRANSIT writes or transfer-order lifecycle is introduced. Requests pass the existing centralized Square policy and injected established-client interface. The standalone --simulate-square option uses a durable SQL simulator without HTTP or real credentials.

One receiving_supply view feeds V4 evidence and the existing incoming reader. Adopted V4 records are excluded from the older projection before union, preventing duplicate incoming. Unknown posting or allocation states do not become zero. A separately refreshed verified inventory observation is required before affected on-hand evidence can be considered current; posting success does not synthesize on-hand counts.

## Isolated operation and recovery

Use the accepted explicit isolated URL and source namespace. Run migrations only in a transaction with `migrate_receiving(connection)`. Application startup does not migrate. Preserve cluster roles and both public/private schemas when backing up; quotation signing keys and evidence bytes are recovery material. The qualified historical rollback is transactional migration rollback followed by forward retry.

Operational rollback uses the current compatible build with both `--disable-receiving` and `--disable-authorization`, without an inventory adapter. Stop only owned workers and app processes after their state is durable. Read-only history remains available and ordinary unsealed worksheet editing remains supported. Do not downgrade or erase receiving facts. An older application binary against the new receiving schema is not qualified.

Restore a cold database to a new disposable target, compare all record fingerprints and schema/sequence definitions, then reconcile IN_FLIGHT commands against independently retained provider history. Confirm an interrupted worker before releasing its ownership. Retry only the same immutable command and idempotency key after explicit owner review. Missing history never proves non-execution. Never reset original facts to repeat a test.

Qualification evidence, desktop screenshots, database dump, exact source bundle, performance samples and recovery procedures are delivered in the Phase 7 output package. Square behavior is protocol-simulated in this phase; live Sandbox and production cutover require separate authorization.
