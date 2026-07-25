# Ordering business-rule decision register

Status date: 2026-07-25. Phase 1 owner review is complete. The repository contained 26 decisions before review; `ORD-DEC-027` is the one evidence-supported addition. Decisions not listed below remain unapproved and must not be implemented.

## Phase 1 owner approval record

| Decision | Owner decision | Final status |
|---|---|---|
| ORD-DEC-001 Zero par | Zero is a real numeric target, never an inferred exclusion. | APPROVED |
| ORD-DEC-002 Null par | Preserve unknown; calculate from valid demand evidence when possible and warn. | APPROVED |
| ORD-DEC-003 Manual lock | Freeze the named manual input; never infer do-not-order. | APPROVED |
| ORD-DEC-009 Cross-store inventory | Calculate each store independently and show other-store stock only as context. | APPROVED |
| ORD-DEC-011 Velocity window | Use trailing 28 days as primary with trailing 7- and 56-day comparisons. | APPROVED |
| ORD-DEC-012 Stockout treatment | Exclude confirmed zero-stock days from eligible selling days and show observed and adjusted velocity. | APPROVED |
| ORD-DEC-013 New products | Show insufficient history with no fabricated quantity unless an explicit manual target exists. | APPROVED |
| ORD-DEC-014 Discontinued products | Suppress actionable quantity only from confirmed discontinued/deleted status; otherwise warn. | APPROVED |
| ORD-DEC-015 Unreceived orders | Count positive `IN_TRANSIT` V1 allocations separately and flag orders older than 30 days. | APPROVED |
| ORD-DEC-016 Non-sellable inventory | Subtract only fresh, product-resolved confirmed quantities; otherwise warn. | APPROVED |
| ORD-DEC-018 Data freshness | Fresh 0–24 hours is actionable. Stale over 24 through 72 hours continues full calculation/display, is marked `STALE DATA`, and is informational only. Critical over 72 hours or unavailable required Square data suppresses actionable quantities, retains the SKU, and states the blocking reason. | APPROVED WITH MODIFICATION |
| ORD-DEC-019 Confidence indicator | Add deterministic `HIGH`, `MEDIUM`, and `LOW` confidence based only on data quality/completeness. It is informational and never changes calculations. | APPROVED WITH MODIFICATION |
| ORD-DEC-027 Established zero-sales | Accept zero demand only with complete fresh data and at least 14 eligible in-stock observation days; otherwise show insufficient information. | APPROVED |

Approval source: explicit owner instruction received 2026-07-25. The approved Phase 1 policies are implemented behind disabled-by-default `ordering_intelligence_v2`; this record does not authorize deployment, exposure, or any deferred policy.

| ID | Decision | Classification | Current V1 behavior | Risk | Recommended/approved V2 behavior | Approval requirement/status |
|---|---|---|---|---|---|---|
| ORD-DEC-001 | Zero par | Phase 1 blocker | A manual zero can suppress a recommendation | Zero may mean valid target, disable, or missing setup | Zero is a real numeric target only; exclusion/status remains separate | APPROVED |
| ORD-DEC-002 | Null par | Phase 1 blocker | Falls through to derived/default behavior depending on row/source | Missing data can look intentional | Treat as unknown; calculate from approved evidence when possible and warn | APPROVED |
| ORD-DEC-003 | Manual lock vs par | Phase 1 blocker | `locked_manual` preserves manual levels | Lock is not an exclusion and semantics are unclear | Lock freezes the named manual input; it does not mean do-not-order | APPROVED |
| ORD-DEC-004 | Do not reorder duration | Later-phase policy decision | No explicit model | Permanent intent may be lost or approximated with zero | Support permanent status and dated temporary exclusion separately | Yes, before Phase 2 |
| ORD-DEC-005 | Exclusion scope | Later-phase policy decision | No explicit scope | A global workaround may affect all stores/vendors | Explicit SKU plus optional store/vendor scope and effective dates | Yes, before Phase 2 |
| ORD-DEC-006 | Preferred-vendor fallback | Phase 1 display-only decision | One active default per SKU; no documented fallback | Missing/unavailable vendor can silently mislead | Display current preference and alternatives; never silently substitute in Phase 1 | Yes for fallback behavior |
| ORD-DEC-007 | MOQ rounding | Phase 1 input-policy decision | Apply MOQ before pack rounding | Rule may overbuy and vendor meaning may differ | Label units; apply `max(raw, MOQ)` only to an optional rounded display quantity | Yes |
| ORD-DEC-008 | Case-pack rounding | Phase 1 input-policy decision | Round order units up to pack size | Mapping changes affect future results and scans | Label units and apply pack ceiling after MOQ to optional rounded display quantity | Yes |
| ORD-DEC-009 | Cross-store inventory | Phase 1 blocker | Aggregated availability is used in ordering math | Stock at another store may not be transferable | Calculate each store independently; never offset with another store by default | APPROVED |
| ORD-DEC-010 | Store transfers | Later-phase policy decision | Reports observe transfers; ordering does not reserve pending moves | Double-counting or shortages | Defer transfer adjustments until explicit transfer states exist | Yes, later phase |
| ORD-DEC-011 | Velocity window | Phase 1 blocker | Configured lookback is averaged across the full period | Promotions and sparse history distort demand | Use trailing 28 days as primary and show trailing 7/56-day comparisons | APPROVED |
| ORD-DEC-012 | Stockouts in velocity | Phase 1 blocker | Velocity report adjusts stockouts; order generation uses a simpler average | Two recommendations disagree and stockouts can appear as low demand | Exclude confirmed out-of-stock days from eligible selling days and disclose adjustment | APPROVED |
| ORD-DEC-013 | New products | Phase 1 blocker | Little/no sales yields low or zero demand | New items are suppressed as if proven low demand | Return insufficient-history/low-confidence, using no fabricated demand baseline | APPROVED |
| ORD-DEC-014 | Discontinued products | Phase 1 blocker | Mapping active and Square status are imperfect proxies | Discontinued goods may be recommended | Suppress actionable quantity only when a confirmed status says discontinued; otherwise warn | APPROVED |
| ORD-DEC-015 | Unreceived orders | Phase 1 blocker | In-transit allocations can count toward current total | Stale POs can suppress orders indefinitely | Include positive `IN_TRANSIT` allocations separately and flag age over 30 days | APPROVED |
| ORD-DEC-016 | Non-sellable inventory | Phase 1 blocker | Separate workflow; not subtracted from Ordering sellable quantity | Recommendations may overstate usable stock | Subtract only fresh, product-resolved confirmed non-sellable quantity; otherwise warn | APPROVED |
| ORD-DEC-017 | Pending transfers | Later-phase policy decision | Not a consistent recommendation input | Source and destination may both count stock | Ignore in Phase 1 calculation and disclose that transfers are not modeled | Yes, later phase |
| ORD-DEC-018 | Incomplete or stale Square data | Phase 1 blocker | Missing reads/mappings often omit or weaken rows; freshness is not retained consistently | Silence or stale values appear as reliable “no need” | Fresh 0–24h actionable; stale over 24–72h calculated/displayed informationally; critical over 72h or unavailable suppresses actionable quantity and states reason | APPROVED WITH MODIFICATION |
| ORD-DEC-019 | Confidence thresholds | Phase 1 display-only decision | Current score weights sufficiency 50%, stability 30%, activity 20% | An unvalidated score can imply approval | Use deterministic HIGH/MEDIUM/LOW levels from data quality and completeness only; never modify calculations | APPROVED WITH MODIFICATION |
| ORD-DEC-020 | Seasonal adjustment | Later-phase policy decision | No explicit owner or durable factor | Hidden adjustments are unauditable | Do not seasonally adjust Phase 1; disclose exclusion | Yes, advanced phase |
| ORD-DEC-021 | Maximum inventory | Phase 1 input-policy decision | No explicit maximum | MOQ/pack rounding can create excess | Do not silently cap raw need; optionally warn above an approved days/units threshold | Yes |
| ORD-DEC-022 | Manual quantity override | Later-phase policy decision | PO quantity is editable | Calculation and human decision are conflated | Preserve recommendation and chosen quantity separately | Yes, before drafts |
| ORD-DEC-023 | Override reason | Later-phase policy decision | Not generally required | Decisions cannot be explained later | Require a reason for policy exceptions or material variance | Yes, before drafts |
| ORD-DEC-024 | Ignored recommendation recurrence | Later-phase policy decision | No independent ignored state | Same item may reappear without context | Record a decision with expiry/material-change recurrence | Yes, before Phase 2 |
| ORD-DEC-025 | Payments spanning POs | Later-phase blocker | Unsupported | One-to-one storage cannot represent real remittance | Payment with explicit allocations across invoices/POs | Yes, before finance phase |
| ORD-DEC-026 | COGS recognition | Later-phase blocker | Live sales multiplied by current preferred cost | Historical values mutate and accounting meaning is unclear | Approve valuation/recognition policy before finance implementation | Yes, before finance phase |
| ORD-DEC-027 | Established product with no recent sales | Phase 1 blocker | Full-window averaging yields zero/low demand; no state distinguishes established zero-sales from missing history | A real demand decline, unavailable item, and missing data can look identical | Return zero only with fresh complete data and at least 14 eligible in-stock observation days; otherwise insufficient-information warning | APPROVED |

## Approval record convention

When resolved, record decision, approver, effective date, rationale, examples, and specification version. Do not delete the prior question or retroactively relabel proposed behavior as confirmed.
