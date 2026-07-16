# Proposed V2 Ordering milestone plan

Status: navigation bridge implemented locally

Canonical owner: **V1 canonical**

Governed by the [V1 Preservation Guarantee](./v1-preservation-guarantee.md).

Implementation scope: navigation bridge only; no Ordering cutover

## Recommended narrowest first slice

The default-disabled V2 Inventory navigation bridge connects these four truthful, existing V1 destinations:

- Ordering Tool
- Par / Level Manager
- Vendor SKU Mappings
- PDF Templates

No V2 Ordering reads or writes were implemented.

Keep Current Orders, Order History, and Order Payments as unavailable placeholders because V1 has no dedicated route or truthful filtered destination for those concepts.

## Why this is the safest useful slice

- It immediately makes the planned V2 information architecture useful without pretending the Ordering Tool has migrated.
- It preserves the complete working V1 workflow and Square behavior.
- The bridge itself performs no Ordering database reads or writes and makes no Square calls; it resolves only to unchanged V1 GET destinations.
- It introduces no shared-table writes, schema changes, PDF storage changes, or state translation.
- Rollback is disabling one V2 link-exposure feature; V1 is unaffected.
- It permits owner observation of the proposed Inventory hierarchy before high-risk write work.

## Proposed exposure and authorization contract

- Feature key: `ordering_v1_links_v2`
- Default: disabled
- Canonical owner state: V1 canonical
- V2 visibility: corresponding `nav.inventory.*` permission
- Destination authorization: unchanged V1 `management.admin`
- No V1 redirect, route alias, template replacement, or navigation removal
- No link for a child without a truthful V1 destination

The key is implemented in the navigation registry and remains absent from default exposure configuration.

## Slice acceptance criteria

1. Four V2 children resolve to unchanged V1 GET routes only when exposed.
2. Direct V1 routes remain unchanged and available without V2.
3. V2 navigation permission does not bypass V1 `management.admin`.
4. Unauthorized deep links retain existing V1 403 behavior.
5. Current Orders, Order History, and Order Payments remain visibly unavailable or omitted according to the established navigation rule.
6. The bridge adds no POST, Square call, database read or write, migration, or V1 redirect.
7. Automated tests cover feature disabled, permission denied, permission allowed, route target, and V1 independence.
8. Browser verification later covers desktop/mobile navigation and return behavior; it is not part of this repository-only discovery.

## Later candidate slices

These are sequenced options, not approvals:

1. **Read-only PO index adapter** — only after product definitions for Current Orders, History, and Payments; reads V1 tables, performs no writes.
2. **Read-only PO detail adapter** — preserve scope, line snapshots, allocations, payment, sync failures, and PDF link to V1.
3. **V2 wrapper around existing V1 services** — only after service transaction boundaries and route-independent authorization are characterized.
4. **Draft generation/edit parity** — requires Square fixtures, concurrency design, PDF decision, and single-writer cutover.
5. **Receiving/Square writes** — last; requires idempotency, reconciliation, permission, rollback, and partial-failure approval.
6. **Emergency on-hand** — separate critical-risk module, not bundled into ordinary Ordering cutover.

## Cutover gates for any V2 writer

- production read-only table/status/distinct-value discovery;
- approved data-write owner;
- no dual writes;
- Square request fixture and idempotency parity;
- route/permission/store/vendor scope decision;
- concurrency test plan;
- PDF artifact/storage/retention plan;
- audit action and before/after evidence plan;
- active draft and IN_TRANSIT ownership plan;
- migration and rollback plan;
- automated and browser verification;
- explicit owner approval for V2 canonical ownership.

V1 retirement remains a separate later approval.

## Expected eventual implementation files

The implemented link-only slice touches:

- `app/v2/navigation.py`
- `tests/test_v2_shell.py`
- navigation, exposure, parity, milestone, discovery, permission, and cutover documentation

It adds no Ordering router, service, model, template, static asset, migration, or database query.

A later read-only adapter would likely add:

- `app/routers/v2_ordering.py`
- `app/services/v2_ordering_read_service.py`
- V2 Ordering list/detail templates
- focused authorization and PostgreSQL integration tests

A later writer parity milestone would additionally involve existing Ordering services, Square integration boundaries, PDF storage, schema/migration review, idempotency, audit, and extensive fixtures. Those files must not be changed until that milestone is explicitly approved.

## Rollback guarantee

For the implemented link-only slice, rollback disables `ordering_v1_links_v2` or restores the prior application version. The unchanged V1 dashboard, routes, services, data, PDFs, and Square behavior remain operational throughout.
