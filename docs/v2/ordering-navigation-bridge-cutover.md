# Ordering V1 navigation bridge cutover record

Governed by the [V1 Preservation Guarantee](./v1-preservation-guarantee.md).

Cutover status: not approved and not performed.

- Slice: V2 Inventory navigation bridge to existing V1 Ordering tools
- Feature key: `ordering_v1_links_v2`
- Default exposure: disabled
- Canonical-owner state: **V1 canonical**
- V2 Ordering implementation: none
- V1 route changes: none
- V1 redirects: none
- Ordering database reads/writes by bridge: none
- Square calls by bridge: none
- Schema migrations: none
- Production deployment or exposure: none

## Linked V1 destinations

- Ordering Tool → `/management/ordering-tool`
- Par / Level Manager → `/management/ordering-tool/par-levels`
- Vendor SKU Mappings → `/management/ordering-tool/mappings`
- PDF Templates → `/management/ordering-tool/pdf-templates`

Each link requires its effective V2 navigation permission, effective `management.admin`, and feature exposure. The unchanged V1 route independently enforces its existing `management.admin` dependency.

## Unavailable entries

Current Orders, Order History, and Order Payments remain unavailable `Coming Later` entries when authorized. They have no destination because V1 has no dedicated truthful route for those concepts.

## Ownership and rollback

This bridge is not Ordering parity, side-by-side Ordering operation, V2 canonical ownership, or V1 retirement. V1 remains the sole operational writer and owns all Ordering routes, services, data, PDFs, audit behavior, and Square integration.

Rollback disables `ordering_v1_links_v2` or restores the prior application version. V1 requires no recovery work.
