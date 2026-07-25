# Phase 0 V2 Ordering owner decision packet

Status date: 2026-07-25. V1 remains canonical. Phase 1 owner review is complete. Eleven blocker defaults were approved exactly, the freshness policy was approved with modification, and deterministic confidence was added as an approved Phase 1 display policy. Unlisted later and optional policies remain unresolved.

Allowed final statuses: `APPROVED`, `APPROVED WITH MODIFICATION`, `DEFERRED`, `NOT APPLICABLE`, `UNRESOLVED`.

## Decisions required before Phase 1

These 12 decisions determine whether a store-level read-only recommendation can be calculated honestly and consistently:

1. `ORD-DEC-001` — what zero par means.
2. `ORD-DEC-002` — what null par means.
3. `ORD-DEC-003` — what a manual lock controls.
4. `ORD-DEC-009` — whether another store’s inventory offsets need.
5. `ORD-DEC-011` — the primary sales-velocity window.
6. `ORD-DEC-012` — how confirmed stockout days affect velocity.
7. `ORD-DEC-013` — how new products are presented.
8. `ORD-DEC-014` — when discontinued status suppresses a recommendation.
9. `ORD-DEC-015` — how open, unreceived V1 orders count as incoming supply.
10. `ORD-DEC-016` — whether confirmed non-sellable quantity reduces availability.
11. `ORD-DEC-018` — how missing or stale Square data affects output.
12. `ORD-DEC-027` — how established products with no recent sales are treated.

All 12 blockers are resolved. Phase 1 readiness is **READY**. Unapproved optional policies remain disabled and later-phase decisions remain out of scope.

## Owner response template

Approval source: explicit owner instruction received 2026-07-25. The approved response is recorded under each affected decision. A recommendation outside those records is not approval.

## Phase 1 blockers

### ORD-DEC-001 — Zero par

- **Question:** Does a par of zero mean a real target of zero, “do not reorder,” or incomplete setup?
- **Why it matters:** The meanings produce opposite recommendations.
- **Confirmed V1 behavior:** Manual zero can suppress a recommendation.
- **Operational consequence:** A zero may silently stop replenishment.
- **Recommended V2 default:** Zero is a real numeric target only; exclusion is a separate status.
- **Alternatives:** Treat zero as do-not-reorder; treat it as missing; configure meaning by product.
- **Recommendation risk:** Existing users may have encoded exclusions as zero and need an exception report.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-002 — Null par

- **Question:** Should a missing par block a result or allow a demand-derived target?
- **Why it matters:** Null is currently ambiguous, not an explicit business decision.
- **Confirmed V1 behavior:** Behavior falls through to derived/default logic depending on row and source.
- **Operational consequence:** Missing setup may look deliberate.
- **Recommended V2 default:** Preserve null as unknown; calculate from other approved evidence when possible and show a warning.
- **Alternatives:** Block all results; substitute a global default; treat as zero.
- **Recommendation risk:** Derived results may be less familiar than current V1 output.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-003 — Manual ordering lock

- **Question:** Does a manual lock freeze a par input, or does it mean do not order?
- **Why it matters:** Phase 1 must know whether locked values remain inputs.
- **Confirmed V1 behavior:** `locked_manual` preserves manual levels; it is not an exclusion field.
- **Operational consequence:** Treating the lock as exclusion can suppress valid need.
- **Recommended V2 default:** Freeze the named manual input; never interpret the lock as do-not-order.
- **Alternatives:** Ignore locks in Phase 1; treat all locks as exclusion.
- **Recommendation risk:** Historical operational usage may differ from the implemented field meaning.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-009 — Cross-store inventory

- **Question:** Can inventory at another store reduce this store’s recommendation?
- **Why it matters:** V1 calculations can aggregate stock that may not be transferable.
- **Confirmed V1 behavior:** Ordering math uses aggregated availability in some workflows.
- **Operational consequence:** One store can appear covered while its shelf is empty.
- **Recommended V2 default:** Calculate each store independently; show other-store stock only as context.
- **Alternatives:** Aggregate all stores; use approved store groups; offset only with confirmed transfers.
- **Recommendation risk:** Total company recommendations may be higher until transfers are modeled.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-011 — Sales-velocity window

- **Question:** What sales period should drive the primary recommendation?
- **Why it matters:** Different windows produce materially different demand.
- **Confirmed V1 behavior:** The configured lookback is averaged over the full period; reports and generation differ.
- **Operational consequence:** Promotions, sparse history, and recent decline may be hidden.
- **Recommended V2 default:** Use trailing 28 days as primary; display trailing 7- and 56-day comparisons and the exact dates.
- **Alternatives:** 14, 30, 42, or 56 days; product-specific windows.
- **Recommendation risk:** A fixed window may react slowly to abrupt change.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-012 — Stockouts in velocity

- **Question:** Should confirmed out-of-stock days be removed from selling days?
- **Why it matters:** No sales while unavailable does not prove no demand.
- **Confirmed V1 behavior:** Velocity reporting estimates stockout-adjusted demand; order generation uses simpler averaging.
- **Operational consequence:** Stocked-out products may be recommended too low.
- **Recommended V2 default:** Exclude confirmed out-of-stock days from eligible selling days; show observed and adjusted velocity.
- **Alternatives:** No adjustment; cap the adjustment; substitute comparable-store demand.
- **Recommendation risk:** Imperfect inventory-change history may overcorrect demand.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-013 — New products

- **Question:** What should Phase 1 show when a product lacks sufficient sales history?
- **Why it matters:** A new product is not a proven low-demand product.
- **Confirmed V1 behavior:** Little or no sales can yield zero/low suggested demand.
- **Operational consequence:** Launch products may disappear from attention.
- **Recommended V2 default:** Show “insufficient history,” low confidence, and no fabricated demand quantity unless an explicit manual target exists.
- **Alternatives:** Owner-set launch baseline; category proxy; suppress until enough history.
- **Recommendation risk:** Reviewers must make a manual judgment without a quantity.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-014 — Discontinued products

- **Question:** Which confirmed status is sufficient to stop an actionable recommendation?
- **Why it matters:** Mapping inactivity and Square availability are not identical to an owner discontinuation decision.
- **Confirmed V1 behavior:** Mapping active and Square status are imperfect proxies.
- **Operational consequence:** A discontinued product may reorder, or an unavailable product may be incorrectly suppressed.
- **Recommended V2 default:** Suppress quantity only for a confirmed owner/Square discontinued or deleted status; otherwise show a status warning.
- **Alternatives:** Suppress any inactive mapping; suppress any unavailable variation; never suppress in Phase 1.
- **Recommendation risk:** Current data may not contain a trustworthy status, producing warnings instead of decisive output.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-015 — Open and unreceived purchase orders

- **Question:** Which open V1 order quantities count as incoming inventory, and for how long?
- **Why it matters:** Ignoring them duplicates orders; trusting stale orders suppresses needed stock.
- **Confirmed V1 behavior:** In-transit allocations can be included in current totals with no robust aging policy.
- **Operational consequence:** Recommendations can double-order or under-order.
- **Recommended V2 default:** Count positive allocations from active `IN_TRANSIT` V1 orders, show them separately, and flag rather than silently trust orders older than 30 days.
- **Alternatives:** Ignore all open orders; count all indefinitely; set another age threshold.
- **Recommendation risk:** Status may not reflect actual vendor placement or delivery expectation.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-016 — Non-sellable inventory

- **Question:** Should damaged/non-sellable stock reduce sellable availability?
- **Why it matters:** Physical stock is not always available for sale.
- **Confirmed V1 behavior:** Non-sellable records are separate and not subtracted by Ordering.
- **Operational consequence:** Available inventory may be overstated.
- **Recommended V2 default:** Subtract only fresh, product-resolved, confirmed non-sellable quantities; otherwise leave inventory unchanged and warn.
- **Alternatives:** Ignore entirely; subtract all open non-sellable records; block every affected result.
- **Recommendation risk:** Current non-sellable identity is not consistently SKU-based, so many rows may only produce warnings.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

### ORD-DEC-018 — Incomplete or stale Square data

- **Question:** How does source age change recommendation actionability?
- **Why it matters:** Missing data must not appear as zero inventory or zero demand.
- **Confirmed V1 behavior:** Some read failures are suppressed to blank/zero and freshness is not retained consistently.
- **Operational consequence:** Reviewers may trust a false recommendation.
- **Approved V2 policy:** Fresh 0–24 hours is fully actionable. Stale over 24 through 72 hours continues calculation and full supporting display, is marked `STALE DATA`, and is informational only. Critical over 72 hours or unavailable required Square data suppresses actionable quantities, retains the SKU, and states the specific blocking reason.
- **Alternatives considered:** Block after 24 hours; use latest snapshot regardless of age; omit the product.
- **Policy risk:** Stale recommendations remain visible and could be misread unless actionability and status are visually explicit.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve with the documented three-state freshness modification.
- **Final status:** `APPROVED WITH MODIFICATION`.

### ORD-DEC-027 — Established product with no recent sales

- **Question:** When may an established product’s recent zero sales produce a zero recommendation?
- **Why it matters:** True decline, stockout, missing data, and unavailability can all look like zero sales.
- **Confirmed V1 behavior:** Full-window averaging produces zero/low demand without a separate zero-sales state.
- **Operational consequence:** A product may silently disappear from ordering attention.
- **Recommended V2 default:** Return zero only when sales/inventory data is fresh and complete and the product had at least 14 eligible in-stock observation days; otherwise show insufficient information.
- **Alternatives:** Always return zero; always use a longer historical baseline; require manual review for every zero-sales product.
- **Recommendation risk:** The 14-day evidence threshold is a proposed operational threshold, not validated production policy.
- **Phase affected / blocks Phase 1:** Phase 1 / Yes.
- **Owner response:** Approve recommended default exactly as documented.
- **Final status:** `APPROVED`.

## Phase 1 non-blocking decisions

### ORD-DEC-006 — Preferred-vendor fallback

- **Question:** Should Phase 1 show another vendor when the preferred vendor is missing or unavailable?
- **Why it matters:** Vendor context helps review but Phase 1 does not create orders.
- **Confirmed V1 behavior:** One active default may exist; fallback is undocumented.
- **Operational consequence:** Silent substitution may imply an unauthorized purchasing choice.
- **Recommended V2 default:** Show preferred vendor and mapped alternatives; never silently substitute.
- **Alternatives:** Hide alternatives; automatically rank by cost; show all equally.
- **Recommendation risk:** More information may require reviewer interpretation.
- **Phase affected / blocks Phase 1:** Phase 1 display / No.
- **Owner response:** Pending.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-007 — MOQ rounding

- **Question:** Should Phase 1 display a quantity raised to the vendor minimum?
- **Why it matters:** It explains practical order size but can imply overbuying.
- **Confirmed V1 behavior:** MOQ is applied before pack rounding.
- **Operational consequence:** Raw need and purchasable quantity differ.
- **Recommended V2 default:** Show raw need and, separately, `max(raw need, MOQ)` with units and explanation.
- **Alternatives:** Raw need only; apply MOQ only in draft phase.
- **Recommendation risk:** Mapping MOQ units may be inaccurate.
- **Phase affected / blocks Phase 1:** Phase 1 optional input/display / No; rounded quantity can be disabled.
- **Owner response:** Pending.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-008 — Case-pack rounding

- **Question:** Should Phase 1 round the MOQ-adjusted quantity to a complete pack?
- **Why it matters:** Purchasable cases differ from individual units.
- **Confirmed V1 behavior:** Quantity rounds upward to mapping pack size.
- **Operational consequence:** Rounded output can exceed raw need.
- **Recommended V2 default:** Show raw, MOQ-adjusted, and pack-rounded quantities separately with unit labels.
- **Alternatives:** Defer rounding to drafts; show cases only.
- **Recommendation risk:** Stale pack mappings can materially overstate quantity.
- **Phase affected / blocks Phase 1:** Phase 1 optional input/display / No; rounded quantity can be disabled.
- **Owner response:** Pending.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-019 — Confidence thresholds

- **Question:** How should Phase 1 summarize data quality without changing calculations?
- **Why it matters:** A deterministic indicator helps triage while preserving human review.
- **Confirmed V1 behavior:** A 50/30/20 score exists but is not validated as an approval threshold.
- **Operational consequence:** Reviewers may over-trust a score.
- **Approved V2 policy:** `HIGH` requires fresh required data, complete sales history, valid inventory, no blocking warnings, and stable inputs. `MEDIUM` covers minor warnings, null-par demand inference, stockout-adjusted velocity, or limited but sufficient history. `LOW` covers new products, sparse history, missing/incomplete supporting data, manual assumptions, or any reliability-reducing condition. LOW takes precedence over MEDIUM; MEDIUM takes precedence over HIGH. Confidence is informational only and never changes calculations or actionability.
- **Alternatives considered:** Retain V1 numeric score; show warning factors without a level.
- **Policy risk:** “Stable” and “sufficient” must be expressed by deterministic evidence checks in the implementation specification and tests.
- **Phase affected / blocks Phase 1:** Phase 1 display / No.
- **Owner response:** Add the deterministic data-quality confidence indicator as a Phase 1 requirement.
- **Final status:** `APPROVED WITH MODIFICATION`.

### ORD-DEC-021 — Maximum inventory

- **Question:** Should Phase 1 cap or warn on unusually large recommendations?
- **Why it matters:** MOQ and pack rules can create excess stock.
- **Confirmed V1 behavior:** No explicit maximum exists.
- **Operational consequence:** A large quantity may be shown without a safety signal.
- **Recommended V2 default:** Never silently cap raw need; show an optional warning only after a maximum-days or maximum-units threshold is approved.
- **Alternatives:** Hard cap; product-level maximum; no maximum warning.
- **Recommendation risk:** Until approved, Phase 1 cannot flag excessive but mathematically valid quantities.
- **Phase affected / blocks Phase 1:** Phase 1 optional input/display / No.
- **Owner response:** Pending.
- **Final status:** `UNRESOLVED`.

## Decisions safely deferred beyond Phase 1

### ORD-DEC-004 — Do-not-reorder duration

- **Question:** Should do-not-reorder be permanent, temporary, or both?
- **Why it matters:** Phase 2 needs explicit merchandising intent.
- **Confirmed V1 behavior:** No explicit model; zero may be used informally.
- **Operational consequence:** Intent can be lost or remain active too long.
- **Recommended V2 default:** Separate permanent status from dated temporary exclusion.
- **Alternatives:** Permanent only; temporary only.
- **Recommendation risk:** More controls require clear ownership.
- **Phase affected / blocks Phase 1:** Phase 2 / No.
- **Owner response:** Deferred to Phase 2 unless answered now.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-005 — Exclusion scope

- **Question:** Does an exclusion apply globally, by store, by vendor, or by combination?
- **Why it matters:** Scope determines who stops ordering what.
- **Confirmed V1 behavior:** No explicit exclusion scope exists.
- **Operational consequence:** A broad workaround can suppress unrelated stores/vendors.
- **Recommended V2 default:** SKU plus optional store/vendor scope and effective dates.
- **Alternatives:** Global SKU only; store/SKU only.
- **Recommendation risk:** Complex scopes can conflict.
- **Phase affected / blocks Phase 1:** Phase 2 / No.
- **Owner response:** Deferred to Phase 2 unless answered now.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-010 — Store transfers

- **Question:** When should an expected transfer change replenishment need?
- **Why it matters:** Transfers need durable states before they can safely offset stock.
- **Confirmed V1 behavior:** Reports observe transfers; ordering does not consistently reserve them.
- **Operational consequence:** Stock may be double-counted.
- **Recommended V2 default:** Do not adjust Phase 1; add only with explicit transfer states later.
- **Alternatives:** Treat report-observed transfers as supply; manual adjustments.
- **Recommendation risk:** Phase 1 may recommend purchasing where a transfer is planned.
- **Phase affected / blocks Phase 1:** Later replenishment / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-017 — Pending transfers

- **Question:** Which pending-transfer state reserves source stock or credits destination stock?
- **Why it matters:** Inbound and outbound timing differ.
- **Confirmed V1 behavior:** Pending transfers are not a consistent recommendation input.
- **Operational consequence:** Both stores may count the same units.
- **Recommended V2 default:** Ignore in Phase 1 and disclose that transfer supply is not modeled.
- **Alternatives:** Reserve at creation; credit at shipment; credit only at receipt.
- **Recommendation risk:** Temporary over-recommendation.
- **Phase affected / blocks Phase 1:** Later replenishment / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-020 — Seasonal adjustment

- **Question:** Who may create seasonal demand adjustments and for what dates?
- **Why it matters:** Hidden multipliers are difficult to audit.
- **Confirmed V1 behavior:** No durable factor or owner exists.
- **Operational consequence:** Seasonal products may use an ordinary trailing window.
- **Recommended V2 default:** No Phase 1 adjustment; later use named, dated, reasoned factors.
- **Alternatives:** Automatic history-based weighting; owner-entered multipliers.
- **Recommendation risk:** Phase 1 may understate known seasonal demand.
- **Phase affected / blocks Phase 1:** Advanced intelligence / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-022 — Manual quantity override

- **Question:** How should a human-selected quantity differ from the calculated recommendation?
- **Why it matters:** Drafts must preserve both facts.
- **Confirmed V1 behavior:** PO quantity is directly editable.
- **Operational consequence:** Original calculation is lost.
- **Recommended V2 default:** Preserve recommendation and chosen quantity separately.
- **Alternatives:** Replace recommendation; create a new recommendation version.
- **Recommendation risk:** More visible values may require UI explanation.
- **Phase affected / blocks Phase 1:** Draft POs / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-023 — Override reason

- **Question:** When must a reviewer explain a quantity override?
- **Why it matters:** Later decisions need an auditable rationale.
- **Confirmed V1 behavior:** Reasons are generally not required.
- **Operational consequence:** Large changes are unexplained.
- **Recommended V2 default:** Require reason outside an approved tolerance or for policy exceptions.
- **Alternatives:** Always require; never require; role-based requirement.
- **Recommendation risk:** Extra workflow friction.
- **Phase affected / blocks Phase 1:** Draft POs / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-024 — Ignored recommendation recurrence

- **Question:** When should an intentionally ignored recommendation return?
- **Why it matters:** Permanent disappearance and repeated noise are both undesirable.
- **Confirmed V1 behavior:** No independent ignored state exists.
- **Operational consequence:** Reviewer intent is not retained.
- **Recommended V2 default:** Return after expiry or material input change.
- **Alternatives:** Return every run; never return; manual reactivation only.
- **Recommendation risk:** “Material change” needs an approved definition.
- **Phase affected / blocks Phase 1:** Phase 2 / No.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-025 — Payments spanning purchase orders

- **Question:** May one vendor payment cover multiple orders and vice versa?
- **Why it matters:** Finance data must match actual remittances.
- **Confirmed V1 behavior:** Unsupported; one mutable amount belongs to one PO.
- **Operational consequence:** Partial and combined payments cannot be reconciled.
- **Recommended V2 default:** Payment events with explicit invoice/PO allocations.
- **Alternatives:** Enforce one payment per PO; invoice-only allocation.
- **Recommendation risk:** Requires accounting workflow and sensitive authorization.
- **Phase affected / blocks Phase 1:** Finance phase / No; later-phase blocker.
- **Owner response:** Deferred.
- **Final status:** `UNRESOLVED`.

### ORD-DEC-026 — COGS recognition

- **Question:** When and at what cost should COGS be recognized?
- **Why it matters:** The answer determines historical accounting reports.
- **Confirmed V1 behavior:** Completed sales are multiplied by current preferred cost.
- **Operational consequence:** Historical COGS changes when mappings/costs change.
- **Recommended V2 default:** Obtain accounting approval for valuation and recognition, then snapshot evidence by period.
- **Alternatives:** Cost at sale, receipt, invoice, weighted average, or another approved method.
- **Recommendation risk:** No technical default is safe without accounting policy.
- **Phase affected / blocks Phase 1:** Finance/reporting phase / No; later-phase blocker.
- **Owner response:** Deferred pending accounting owner.
- **Final status:** `UNRESOLVED`.
