# Ordering permission and scope matrix

Canonical owner: **V1 canonical**. No permission changes are authorized by this record.

## Effective capability model

Most Ordering routes depend on:

```text
management.admin
principal override > role override > fallback role
fallback roles: ADMIN, MANAGER
```

Because `require_capability` does not impose a literal-role ceiling, a LEAD or STORE principal explicitly allowed `management.admin` can pass an Ordering route dependency. The V1 management dashboard, however, hides the Ordering card from non-admin-role principals before checking capability-specific visibility. This produces a direct-route-versus-navigation inconsistency.

## Route/action matrix

| Capability | Route permission | Default ADMIN | Default MANAGER | Default LEAD | Default STORE | Granular override possible | Store/vendor scope |
|---|---|---:|---:|---:|---:|---:|---|
| Open Ordering Tool and all subpages | `management.admin` | Yes | Yes | No | No | Yes | All active vendors/stores |
| Sync vendors/mappings | `management.admin` | Yes | Yes | No | No | Yes | Optional vendor scope for mapping sync |
| Edit/import mappings | `management.admin` | Yes | Yes | No | No | Yes | Any vendor ID accepted after validation |
| View/edit/prefill pars | `management.admin` | Yes | Yes | No | No | Yes | All active stores for selected vendor |
| Manage PDF templates | `management.admin` | Yes | Yes | No | No | Yes | Generic/all selected vendors |
| Generate/edit/submit/delete POs | `management.admin` | Yes | Yes | No | No | Yes | All vendors/stores; no per-manager restriction |
| Save invoice/payment | `management.admin` | Yes | Yes | No | No | Yes | Any accessible PO ID |
| Receive/scan/push inventory | `management.admin` | Yes | Yes | No | No | Yes | All PO stores; no separate approval |
| Emergency inventory | `management.admin` | Yes | Yes | No | No | Yes | All active vendors/stores |
| Stock Coverage page/export/create-order | literal `ADMIN` | Yes | No | No | No | No | Optional report store; any selected vendor |
| V1 dashboard Ordering card | literal admin-role filter | Yes | Yes | No | No | No | Navigation only |

## Scope findings

- No Ordering route intersects access with `principal.store_id`.
- No manager-to-store assignment table or resolver is used.
- No vendor scope is assigned to a principal.
- There are no separate view, edit, generation, submit, receive, payment, PDF, delete, or Square-write capabilities.
- Opening an order by numeric ID is authorized only at the module level.
- CSRF protects browser mutations, but CSRF is not authorization.
- The V2 navigation permissions (`nav.inventory.*`) control only visibility and do not grant V1 `management.admin`.

## Approval and closing findings

- Submit, invoice, receive, retry, delete, mapping edits, template edits, and emergency pushes all share the same `management.admin` capability.
- No second approver, four-eyes check, vendor approval, receive approval, payment approval, closing permission, or explicit close action exists.
- The implemented lifecycle has no route for COMPLETED or CANCELLED.

## V2 navigation planning

| Child | Visibility permission | Existing V1 authorization | Proposed exposure |
|---|---|---|---|
| Ordering Tool | `nav.inventory.ordering_tool` | `management.admin` | Default-disabled link feature plus both checks |
| Par / Level Manager | `nav.inventory.par_levels` | `management.admin` | Same |
| Vendor SKU Mappings | `nav.inventory.vendor_skus` | `management.admin` | Same |
| PDF Templates | `nav.inventory.pdf_templates` | `management.admin` | Same |
| Current Orders | `nav.inventory.current_orders` | No dedicated V1 route | Remain unavailable |
| Order History | `nav.inventory.order_history` | No dedicated V1 route | Remain unavailable |
| Order Payments | `nav.inventory.order_payments` | No dedicated V1 route | Remain unavailable |

A V2 link must never treat its navigation permission as sufficient authorization. The unchanged V1 destination remains responsible for `management.admin`.

## Permission risks requiring owner decisions before write parity

1. Whether granular LEAD access is intentional or only technically possible.
2. Whether Stock Coverage should remain literal ADMIN-only.
3. Whether Square writes need a distinct high-risk capability.
4. Whether payments, submit, delete, and receiving need separate capabilities.
5. Whether management store or vendor scope is required.
6. Whether V2 navigation should expose a direct V1 link to principals who can call the route but lack the V1 dashboard card.
