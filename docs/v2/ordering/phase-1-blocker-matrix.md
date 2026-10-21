# Phase 1 Ordering implementation-blocker matrix

Status date: 2026-07-25. All Phase 1 blockers are owner-approved. “Proceed if disabled” means the optional unapproved output remains outside Phase 1 without corrupting the core calculation.

| Decision | Classification | Recommended default | Owner response required | Phase 1 impact | Can implementation proceed? |
|---|---|---|---|---|---|
| ORD-DEC-001 Zero par | Phase 1 blocker | Zero is numeric; never infer exclusion | APPROVED | Determines target and eligibility | Yes |
| ORD-DEC-002 Null par | Phase 1 blocker | Preserve unknown; use valid demand evidence with warning | APPROVED | Determines fallback target | Yes |
| ORD-DEC-003 Manual lock | Phase 1 blocker | Freeze named input; not exclusion | APPROVED | Determines which par input is authoritative | Yes |
| ORD-DEC-004 Do-not-reorder duration | Later-phase policy decision | Permanent and dated controls | Before Phase 2 | No explicit Phase 1 source exists | Yes |
| ORD-DEC-005 Exclusion scope | Later-phase policy decision | SKU plus optional store/vendor and dates | Before Phase 2 | No explicit Phase 1 source exists | Yes |
| ORD-DEC-006 Preferred-vendor fallback | Phase 1 display-only decision | Show preference/alternatives; no substitution | Before enabling vendor fallback | Vendor context only | Proceed if fallback disabled |
| ORD-DEC-007 MOQ rounding | Phase 1 input-policy decision | Optional separate MOQ-adjusted value | Before enabling rounded display | Raw need remains valid | Proceed if disabled |
| ORD-DEC-008 Case-pack rounding | Phase 1 input-policy decision | Optional separate pack-rounded value | Before enabling rounded display | Raw need remains valid | Proceed if disabled |
| ORD-DEC-009 Cross-store inventory | Phase 1 blocker | Per-store calculation; no silent offset | APPROVED | Determines available inventory | Yes |
| ORD-DEC-010 Store transfers | Later-phase policy decision | No Phase 1 adjustment | Later replenishment phase | Disclose exclusion | Yes |
| ORD-DEC-011 Velocity window | Phase 1 blocker | Trailing 28 days; show 7/56 comparisons | APPROVED | Determines demand rate | Yes |
| ORD-DEC-012 Stockout treatment | Phase 1 blocker | Remove confirmed zero-stock days from eligible days | APPROVED | Determines adjusted demand | Yes |
| ORD-DEC-013 New products | Phase 1 blocker | Insufficient-history state; no fabricated demand | APPROVED | Determines output for sparse history | Yes |
| ORD-DEC-014 Discontinued products | Phase 1 blocker | Suppress only on confirmed discontinued/deleted status | APPROVED | Determines recommendation eligibility | Yes |
| ORD-DEC-015 Unreceived orders | Phase 1 blocker | Count positive `IN_TRANSIT` allocations; flag >30 days | APPROVED | Determines pending supply/net need | Yes |
| ORD-DEC-016 Non-sellable inventory | Phase 1 blocker | Subtract only fresh, product-resolved confirmed quantity | APPROVED | Determines sellable availability | Yes |
| ORD-DEC-017 Pending transfers | Later-phase policy decision | Ignore and disclose | Later replenishment phase | Transfer supply not modeled | Yes |
| ORD-DEC-018 Incomplete/stale Square data | Phase 1 blocker | Fresh 0–24h actionable; stale 24–72h calculated but informational; critical >72h/unavailable suppresses actionable quantity | APPROVED WITH MODIFICATION | Determines actionability and data status | Yes |
| ORD-DEC-019 Confidence thresholds | Phase 1 display-only decision | Deterministic HIGH/MEDIUM/LOW from data quality/completeness; never changes calculations | APPROVED WITH MODIFICATION | Required informational indicator | Yes |
| ORD-DEC-020 Seasonal adjustment | Later-phase policy decision | No Phase 1 adjustment | Advanced phase | Disclose exclusion | Yes |
| ORD-DEC-021 Maximum inventory | Phase 1 input-policy decision | No silent cap; optional warning after approval | Before enabling maximum warning | Calculation remains uncapped | Proceed if warning disabled |
| ORD-DEC-022 Manual quantity override | Later-phase policy decision | Preserve calculated and chosen quantities | Before draft POs | No Phase 1 mutation | Yes |
| ORD-DEC-023 Override reason | Later-phase policy decision | Require for material/policy exception | Before draft POs | No Phase 1 mutation | Yes |
| ORD-DEC-024 Ignored recurrence | Later-phase policy decision | Reappear on expiry/material change | Before Phase 2 decisions | No Phase 1 decision persistence | Yes |
| ORD-DEC-025 Payments spanning POs | Later-phase blocker | Payment with explicit allocations | Before finance phase | No Phase 1 effect | Yes |
| ORD-DEC-026 COGS recognition | Later-phase blocker | Accounting-approved policy and evidence snapshot | Before finance phase | No Phase 1 effect | Yes |
| ORD-DEC-027 Established zero-sales | Phase 1 blocker | Accept zero only with fresh complete data and 14 eligible in-stock days | APPROVED | Distinguishes zero demand from insufficient information | Yes |

## Readiness result

**READY — all 12 Phase 1 blockers are resolved and the confidence indicator is approved.**

Phase 1 implementation planning may proceed. Preferred-vendor fallback, MOQ display, pack display, and maximum-inventory warnings remain unapproved and must stay disabled. Runtime work still requires explicit implementation approval.
