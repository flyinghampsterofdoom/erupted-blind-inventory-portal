# V2 Ordering security and authorization review

Status: proposed capability design. No permissions or exposure are changed in this milestone.

## Capability matrix

| Capability | Proposed key | Scope / sensitivity |
|---|---|---|
| View recommendations | `ordering.recommendations.view` | Authorized stores only |
| Edit controls | `ordering.controls.edit` | Authorized stores/SKUs; reasoned audit |
| Create/edit drafts | `ordering.po.create_draft` | Authorized stores/vendors |
| Approve POs | `ordering.po.approve` | Separation from draft author where policy requires |
| View vendor pricing | `ordering.vendor_pricing.view` | Sensitive commercial data; not implied by recommendation view |
| View COGS | `ordering.cogs.view` | Sensitive financial reporting |
| Record vendor payments | `ordering.payments.record` | Sensitive; amount/account access independently checked |
| Receive inventory | `ordering.receiving.record` | Only stores/order allocations in scope |
| Reconcile receipts | `ordering.receiving.reconcile` | Separate exception authority |
| Approve Square writes | `ordering.square_write.approve` | Highly restricted; cannot be implied by receive |
| Reconcile failed writes | `ordering.square_write.reconcile` | Highly restricted and fully audited |
| Administer mappings/vendors | `ordering.configuration.admin` | Global or explicitly bounded principal scope |

Names are proposed and must be reconciled with the canonical capability registry during implementation.

## Store and principal scope

Every query begins with authenticated principal and server-resolved authorized store IDs. Detail routes re-check aggregate scope to prevent IDOR. Multi-store recommendations disclose only stores in scope; totals must not leak excluded-store values. Vendor/global configuration requires an explicit non-store capability. Exports and PDFs apply the same checks as HTML views.

## Mutation controls

- POST/PUT/PATCH/DELETE require the established CSRF protection and reject missing/invalid tokens before side effects.
- Server-side validation covers quantity units, money, dates, state transition, expected row version, reason requirements, scope, and capability.
- Approval, payment, receiving reconciliation, and Square send are discrete commands; access to one never implies another.
- Optional two-person separation for PO approval and mandatory explicit separation for future Square approval should be an owner decision.

## Sensitive data and audit

Vendor prices, COGS, payment details, and funding-account references are not embedded in broad navigation payloads or logs. Store only non-secret account references; never Square credentials or full card/bank data. Audit success and denied attempts for approvals, payments, reconciliation, exports, configuration, and Square commands with actor, principal, scope, correlation, reason, and safe changes.

## Feature exposure

Early rollout is principal-scoped owner preview. Exposure is not authorization: both must pass. The V1 bridge flag `ordering_v1_links_v2` must remain separate from any future native feature key. A disabled V2 feature prevents its routes/navigation while leaving V1 behavior unchanged.

## Threat-focused tests

Test guessed IDs, mixed authorized/unauthorized store lists, forged scope fields, stale row versions, replayed operation tokens, CSRF failure, privilege separation, sensitive export/PDF access, log redaction, and disabled-feature direct route access.
