# Approved Phase 1 Ordering policy baseline

Status date: 2026-07-25. Required calculation policies are owner-approved and implemented in the repository as recorded below. The implementation is not deployed or production-exposed and remains subject to implementation review. Policies marked `UNRESOLVED — DISABLED` are not included.

## Required calculation policies

| Policy | Inputs | Deterministic rule | Missing-data behavior | Warning behavior | Confidence effect | Required explanation | Owner approval status |
|---|---|---|---|---|---|---|---|
| P1-POL-001 Zero par (`ORD-DEC-001`) | Manual par/target and source | Treat numeric zero as a real zero target, never as exclusion | Preserve missing separately | Warn when legacy zero may have encoded exclusion | No penalty for confirmed zero; block if meaning cannot be established | State zero source and that no exclusion was inferred | APPROVED |
| P1-POL-002 Null par (`ORD-DEC-002`) | Par value/source and demand inputs | Preserve null; use demand-derived target only when its required inputs are valid | Do not substitute zero | “Par missing; demand-derived target used” or blocked reason | MEDIUM when inference is valid; LOW when reliability is reduced further | Identify absent par and fallback evidence | APPROVED |
| P1-POL-003 Manual lock (`ORD-DEC-003`) | Lock and named manual fields | Lock freezes the named value; it does not exclude the product | Unknown lock semantics block use of locked value | Explain locked input | LOW for manual assumptions; otherwise determined by evidence | Name locked field and value | APPROVED |
| P1-POL-004 Store isolation (`ORD-DEC-009`) | Inventory/sales by Square location | Calculate each store independently; other-store stock is context only | Missing selected-store inventory is critical for that store | Warn that transfers/cross-store offset are not modeled | LOW or blocked presentation when required inventory is unavailable | Show exact store and excluded other-store stock | APPROVED |
| P1-POL-005 Velocity window (`ORD-DEC-011`) | Daily completed sales | Trailing 28 days primary; trailing 7/56 days comparison; exact UTC/local-day contract fixed in implementation spec | Missing primary history prevents an actionable demand result | Warn on insufficient eligible days or large comparison divergence | MEDIUM for limited sufficient history; LOW for sparse history | Show dates, units, eligible days, primary/comparison rates | APPROVED |
| P1-POL-006 Stockout adjustment (`ORD-DEC-012`) | Daily sales and confirmed inventory-change evidence | Remove confirmed zero-stock days from eligible selling days; do not invent lost units | If stockout state is unavailable, use observed rate only and warn | Show observed versus adjusted rate and removed days | MEDIUM for valid stockout adjustment; LOW if supporting evidence is incomplete | State adjustment evidence and formula | APPROVED |
| P1-POL-007 New product (`ORD-DEC-013`) | First-seen/history, manual target, fresh inventory | With insufficient history, produce an insufficient-history record and no fabricated demand quantity; an explicit manual target remains visible | Missing age/history stays unknown | New-product/manual-target warning | LOW | State history available and why no demand quantity exists | APPROVED |
| P1-POL-008 Discontinued status (`ORD-DEC-014`) | Square status, mapping state, future explicit status | Suppress actionable quantity only for a confirmed discontinued/deleted status | Ambiguous or missing status does not suppress | Show inactive/unavailable/status-conflict warning | LOW when eligibility is uncertain | Name every status source used | APPROVED |
| P1-POL-009 Open V1 supply (`ORD-DEC-015`) | V1 `IN_TRANSIT` allocations, status, age | Include positive open allocation separately; flag orders older than 30 days instead of silently trusting | Unreadable open-order data prevents reliable net need | Show PO IDs, quantities, ages, and stale flag | LOW for aged or incomplete incoming-supply evidence | Show gross need, each incoming quantity, and net effect | APPROVED |
| P1-POL-010 Non-sellable stock (`ORD-DEC-016`) | Resolved non-sellable quantity, state, timestamp | Subtract only fresh, confirmed quantity mapped to the same durable product/store | Unresolved identity or freshness leaves inventory unchanged | Warn that non-sellable stock could not be applied | LOW when supporting quantity is unresolved | Show quantity, source, age, or exclusion reason | APPROVED |
| P1-POL-011 Square freshness/completeness (`ORD-DEC-018`) | Read status and source timestamps | 0–24h: actionable. Over 24–72h: calculate/display fully but mark `STALE DATA` and informational only. Over 72h or required source unavailable: suppress actionable quantity, retain SKU, state reason | Unavailable required data is critical | Show source age for stale; show exact missing/failed source for critical | Stale or critical conditions are LOW; confidence never changes calculations/actionability | State each source timestamp, freshness state, actionability, and blocking reason | APPROVED WITH MODIFICATION |
| P1-POL-012 Established zero-sales (`ORD-DEC-027`) | Product age/history, eligible in-stock days, fresh sales/inventory | Zero demand is allowed only with complete fresh data and at least 14 eligible in-stock days | Otherwise return insufficient information | Explain zero sales versus unavailable/missing evidence | LOW when evidence threshold fails | Show eligible days, observed units, and why zero was accepted/rejected | APPROVED |

## Optional Phase 1 policies

These do not block initial implementation if their affected outputs are disabled.

| Policy | Inputs | Deterministic rule | Missing-data behavior | Warning behavior | Confidence effect | Required explanation | Owner approval status |
|---|---|---|---|---|---|---|---|
| P1-POL-013 Vendor context (`ORD-DEC-006`) | Current preferred and eligible mappings | Display preference and alternatives; do not substitute | Display mapping incomplete | Missing/preferred-unavailable warning | Reduce context confidence only | Identify preferred source and alternatives | UNRESOLVED — DISABLED |
| P1-POL-014 MOQ display (`ORD-DEC-007`) | Raw units, MOQ and unit label | If enabled, `max(raw, MOQ)` for positive raw need | Omit adjusted value | Missing/ambiguous MOQ warning | No effect on raw need | Show before/after and units | UNRESOLVED — DISABLED |
| P1-POL-015 Pack display (`ORD-DEC-008`) | MOQ-adjusted units, pack size/unit | If enabled, `ceil(adjusted / pack) * pack` | Omit rounded value | Missing/stale/invalid pack warning | No effect on raw need | Show pack size and rounding delta | UNRESOLVED — DISABLED |
| P1-POL-016 Confidence display (`ORD-DEC-019`) | Freshness, completeness, inventory validity, warnings, history sufficiency, inference/adjustment/manual-assumption flags | LOW if any LOW condition; otherwise MEDIUM if any MEDIUM condition; otherwise HIGH only when all HIGH conditions hold. The level never changes calculations or actionability | Missing/incomplete supporting data produces LOW, while required-data availability still follows P1-POL-011 | Display every contributing condition next to the level | This policy is the confidence result; it does not feed calculation | Explain why the level was assigned using stable reason codes | APPROVED WITH MODIFICATION |
| P1-POL-017 Maximum warning (`ORD-DEC-021`) | Raw/rounded need and approved maximum | Never cap silently; if enabled, flag threshold breach | Omit warning when no approved threshold | Maximum exceeded warning | No effect on raw calculation | Show threshold and excess | UNRESOLVED — DISABLED |

### Approved confidence precedence

Confidence uses evidence flags, not predictive scoring:

1. `LOW` if any LOW condition exists: new product, fewer than 14 eligible history days, missing/incomplete supporting data, stale or critical required data, conflicting/invalid inventory, manual assumption, or another reliability-reducing warning.
2. Otherwise `MEDIUM` if any MEDIUM condition exists: a minor warning, null-par demand inference, stockout-adjusted velocity, or limited-but-sufficient history of 14–27 eligible days.
3. Otherwise `HIGH` only when required data is fresh, sales history has at least 28 eligible days, inventory is valid, and there are no warning, inference, adjustment, conflict, or manual-assumption flags.

For this policy, “stable calculation inputs” means no MEDIUM or LOW evidence flag. It does not introduce trend prediction or a hidden numeric stability score. Confidence is derived after calculation and cannot change formula selection, numeric results, freshness, or actionability.

## Explicit exclusions from Phase 1 policy

Do-not-reorder controls, exclusion scope, transfer adjustments, seasonal weighting, quantity overrides, ignored-decision recurrence, payments, and COGS recognition remain later-phase decisions. Phase 1 must disclose that transfers and explicit exclusions are not modeled; it must not invent these facts from zero or null values.
