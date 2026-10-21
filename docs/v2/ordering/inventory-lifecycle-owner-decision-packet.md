# Inventory lifecycle owner decision packet

Status date: 2026-07-25. Status: **OWNER APPROVED — FOCUSED PHASE 1/2 IMPLEMENTATION AUTHORIZED**. The owner resolved all 15 decisions and later authorized the focused Lifecycle Foundation and Ordering Integration plan. The authorization does not include workspace redesign, Stagnant Inventory, automation, exposure, deployment, V1 changes, or Square writes.

This packet records the owner’s disposition of the 15 questions from the accepted [inventory lifecycle design](./inventory-lifecycle-and-stagnant-inventory-design.md). Packet IDs `IL-01` through `IL-15` map to approved rules `ORD-DEC-028` through `ORD-DEC-036`; only the separately approved Phase 1/2 implementation plan is in runtime scope.

Phase abbreviations used below:

- **P1** — lifecycle model
- **P2** — bulk status controls
- **P3** — Square workload filtering
- **P4** — Ordering workspace sorting/filtering/pagination
- **P5** — performance remeasurement
- **P6** — Stagnant Inventory

## Disposition and classification summary

All owner-policy blockers are resolved. The classification records when each choice was required; “safely deferred” means the approved boundary omits that functionality from P1 and may still require later implementation approval.

| Owner disposition | Decisions | Count |
|---|---|---:|
| Approved | IL-01–IL-06, IL-08–IL-10, IL-12–IL-14 | 12 |
| Approved with modification | IL-07, IL-11, IL-15 | 3 |
| Unresolved | None | 0 |
| **Total** | **IL-01–IL-15** | **15** |

| Classification | Decisions | Count |
|---|---|---:|
| Immediate blockers for P1 | IL-01, IL-02, IL-03, IL-04 | 4 |
| Required before P3 Square workload filtering | IL-13 | 1 |
| Required before P4 Ordering workspace UX | IL-11, IL-12 | 2 |
| Required only for P6 Stagnant Inventory | IL-07, IL-08, IL-09, IL-10 | 4 |
| Safely deferred | IL-05, IL-06, IL-14, IL-15 | 4 |
| **Total** | **IL-01–IL-15** | **15** |

## IL-01 — Lifecycle identity and scope

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-028` |
| Owner disposition | **APPROVED** |
| Plain-language question | Is lifecycle one company-wide state per Square variation, or may the same variation have different states by store or vendor? |
| Why it matters | It determines the primary key, uniqueness rules, UI meaning, and which products may be removed before Square reads. |
| Recommended default | One global state per Square variation ID. SKU/name are display snapshots; temporary store/vendor exclusions remain separate decisions. |
| Approving recommendation | Produces one unambiguous, reversible product state without changing Square or V1. |
| Main alternative | Store/vendor-scoped lifecycle multiplies rows, introduces precedence conflicts, and makes archive/recovery and reporting materially harder. |
| Blocks | **P1, P2, P3, P4, P5, P6** |
| Safely deferred? | No. |
| Dependencies | None. |
| Exact register wording | **Lifecycle is a single company-wide state keyed by Square variation ID. SKU and product name are display snapshots; store-, vendor-, and date-scoped purchasing exclusions remain separate merchandising decisions.** |

## IL-02 — Restore target and invalid-prior-state fallback

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-030` |
| Owner disposition | **APPROVED** |
| Plain-language question | When an archived product is restored, which state should it return to, including when its prior state is unavailable or invalid? |
| Why it matters | Restoring everything to Active could silently restart purchasing for a product intentionally marked No Future Reorder. The original design did not state the fallback explicitly. |
| Recommended default | Record the prior state; restore Active to Active and No Future Reorder to No Future Reorder. If prior state is unavailable/invalid, restore visibly to No Future Reorder and require a separate explicit owner action to make it Active. |
| Approving recommendation | Recovery is deterministic and cannot silently reactivate purchasing. The fallback is conservative, visible, and audited. |
| Main alternative | Always restore to Active; simpler UI, but it can unintentionally re-enable purchase recommendations. |
| Blocks | **P1, P2** |
| Safely deferred? | No; `pre_archive_status` and fallback behavior belong in the lifecycle contract. |
| Dependencies | IL-01. |
| Exact register wording | **Archive records the prior operating state. Restore returns a prior Active product to Active and a prior No Future Reorder product to No Future Reorder; an unavailable or invalid prior state restores visibly to No Future Reorder and requires a separate explicit owner action before purchasing can resume.** |

## IL-03 — No Future Reorder output

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-029` |
| Owner disposition | **APPROVED AS RECOMMENDED** |
| Plain-language question | Should No Future Reorder retain a hidden calculated purchase quantity, or produce no purchase quantity at all? |
| Why it matters | A hidden quantity can leak into later drafts, exports, or APIs and contradict the “never purchase again” intent. |
| Approved rule | Preserve inventory, sales, velocity, last-sale evidence, freshness, confidence, warnings, and relevant descriptive evidence, but do not calculate, retain, hide, or display a hypothetical purchase quantity. Show `NO FUTURE REORDER` and a named exclusion reason. |
| Approving recommendation | The product remains operationally visible while every purchasing surface receives an unambiguous non-purchasable result. |
| Main alternative | Calculate but hide the quantity; aids hypothetical analysis but creates accidental-order and policy-consistency risk. |
| Blocks | **P1, P4, P6** |
| Safely deferred? | No. |
| Dependencies | IL-01. |
| Exact register wording | **No Future Reorder preserves inventory, sales, velocity, last-sale evidence, freshness, confidence, warnings, and relevant descriptive evidence but does not calculate, retain, hide, or display a hypothetical purchase quantity. It displays NO FUTURE REORDER and a named purchasing exclusion reason; par zero and template hiding are prohibited implementations.** |

## IL-04 — Archive eligibility, control, and remote-work effect

| Field | Owner choice |
|---|---|
| Register IDs | `ORD-DEC-030`, `ORD-DEC-031` |
| Owner disposition | **APPROVED** |
| Plain-language question | Which products may be archived, who may change lifecycle, and what routine work may an archived product skip? |
| Why it matters | The answer controls recoverability, authorization, and whether archive can safely reduce the dominant Square inventory-change workload. |
| Recommended default | Active and No Future Reorder may both be archived. Only principals with a new `ordering.lifecycle.manage` capability may change status; initial exposure is owner-only. Archived variations are excluded from recommendation calculation, inventory counts, and inventory-change retrieval, but catalog integrity/recovery metadata remains available. Do not claim that current order searches can skip their lines. |
| Approving recommendation | Provides controlled reversible archive and permits early filtering of variation-scoped Square counts/history without overstating savings. |
| Main alternative | Archive only Active products or hide archived rows only in the UI; simpler policy, but it either prevents valid cleanup or leaves expensive remote/calculation work unchanged. |
| Blocks | **P1, P2, P3, P5** |
| Safely deferred? | No. |
| Dependencies | IL-01, IL-02. |
| Exact register wording | **Active and No Future Reorder products may be archived only by a principal with ordering.lifecycle.manage, initially owner-only. Archived variations are excluded before recommendation calculation and eligible variation-scoped Square inventory-count and inventory-change reads; catalog integrity and recovery metadata remain available, and the current location/date order search is not represented as variation-filtered.** |

## IL-05 — Lifecycle notes

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-030` |
| Owner disposition | **APPROVED** |
| Plain-language question | Is a note required when setting No Future Reorder, archiving, or restoring? |
| Why it matters | Required notes improve context but add friction to high-volume cleanup and can produce low-quality filler text. |
| Recommended default | Notes are optional for ordinary owner-controlled transitions. Actor, timestamp, from/to state, and batch correlation remain mandatory. |
| Approving recommendation | Bulk cleanup stays lightweight while every change retains structured audit evidence. |
| Main alternative | Require a note for every transition; provides more narrative context but slows bulk work and encourages meaningless text. |
| Blocks | **P2** |
| Safely deferred? | Yes, until P2 validation/UI design; schema can keep the field nullable. |
| Dependencies | IL-04. |
| Exact register wording | **Lifecycle notes are optional; actor, UTC timestamp, prior state, resulting state, and batch correlation are mandatory audit evidence for every transition.** |

## IL-06 — Bulk conflict behavior

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-030` |
| Owner disposition | **APPROVED** |
| Plain-language question | If one selected product changed after the page loaded, should the entire bulk action fail or should unaffected products still change? |
| Why it matters | Partial success can leave the owner unsure which products were archived or restored. |
| Recommended default | One atomic batch: any missing, unauthorized, invalid, or stale row rejects the complete selected set with one clear conflict result. |
| Approving recommendation | The final state always matches the single confirmation the owner approved. |
| Main alternative | Per-row partial success reduces retries but requires a detailed outcome/reconciliation workflow. |
| Blocks | **P2** |
| Safely deferred? | Yes, until P2. |
| Dependencies | IL-01, IL-04. |
| Exact register wording | **A bulk lifecycle command is atomic: any missing, unauthorized, invalid, or version-conflicted selection rejects the complete selected set and changes no lifecycle row.** |

## IL-07 — Inventory valuation basis

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-033` |
| Owner disposition | **APPROVED WITH MODIFICATION** |
| Plain-language question | Which unit cost should value stagnant inventory when several or no vendor mappings exist? |
| Why it matters | Treating missing cost as zero understates exposure; silently choosing another vendor changes the financial meaning. |
| Approved rule | Use the current preferred vendor’s valid configured unit cost; otherwise use the most recent valid purchase cost from a trustworthy source; otherwise show `Unknown`. Label the basis and report known value separately from unknown-cost rows. Never substitute another vendor’s configured cost silently. |
| Consequence | Produces a transparent operational estimate without treating missing cost as zero. Until trustworthy recent purchase cost exists, the fallback remains unavailable and the value is `Unknown`. |
| Main alternative | Use another vendor’s configured or current Square cost; raises coverage but creates unapproved and potentially incorrect valuation. |
| Blocks | **P6** |
| Safely deferred? | Yes, until P6; do not display value metrics earlier. |
| Dependencies | IL-01, preferred-vendor data integrity, and TD-028 for a trustworthy recent-purchase-cost fallback. |
| Exact register wording | **Stagnant Inventory valuation uses, in order, the current preferred vendor’s valid configured unit cost, then the most recent valid purchase cost from a trustworthy source, then Unknown. Missing cost is never coerced to zero; the displayed basis and as-of time are required; known value is reported separately from unknown-cost rows; another vendor’s configured cost is not substituted without separate approval.** |

## IL-08 — Last-sale evidence and horizon

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-034` |
| Owner disposition | **APPROVED** |
| Plain-language question | What evidence establishes last sale, especially beyond the current 56-day request window? |
| Why it matters | A 90/180/365-day report cannot distinguish “no sale” from “not searched” using current request-time data. |
| Recommended default | Use the latest observed positive completed sale per store/variation from a durable Square read model with explicit coverage start and refresh time. Before that model exists, long-horizon last-sale metrics remain unavailable. |
| Approving recommendation | Prevents false age claims and avoids unbounded Square order scans in report requests. |
| Main alternative | Search deeper Square history on every request; avoids persistence but creates severe latency/rate-limit risk and uncertain coverage. |
| Blocks | **P6** and depends on the future read-model milestone. |
| Safely deferred? | Yes, until P6; long-horizon metrics must remain absent. |
| Dependencies | TD-026 read-model design, IL-01. |
| Exact register wording | **Last sale is the latest observed positive completed sale per store and variation from a durable Square read model with explicit coverage start and refresh time. Long-horizon age is unavailable until that evidence exists; request-time unbounded history scans are prohibited.** |

## IL-09 — Stagnant sell-through velocity

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-032` |
| Owner disposition | **APPROVED** |
| Plain-language question | Which velocity drives estimated stagnant sell-through? |
| Why it matters | A different hidden reporting window would make Ordering and Stagnant Inventory disagree. |
| Recommended default | Reuse the approved 28-day adjusted Ordering velocity, with 7/56-day comparisons available as context. Positive velocity yields inventory/velocity days; zero velocity displays `No projected sell-through`. |
| Approving recommendation | Keeps calculations deterministic, explainable, and consistent with Ordering Phase 1. |
| Main alternative | Define a separate stagnant-report window; may better fit merchandising but requires a new policy, tests, and explanation contract. |
| Blocks | **P6** |
| Safely deferred? | Yes, until P6. |
| Dependencies | Approved `ORD-DEC-011` and `ORD-DEC-012`; IL-08 for last-sale context. |
| Exact register wording | **Estimated stagnant sell-through uses the approved 28-day stockout-adjusted Ordering velocity, with 7- and 56-day comparisons as context. Positive velocity yields inventory divided by velocity; zero velocity displays No projected sell-through.** |

## IL-10 — Report inclusion, store aggregation, and unknown/zero inventory

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-032` |
| Owner disposition | **APPROVED** |
| Plain-language question | How should Stagnant Inventory handle stores, zero inventory, and unavailable or critically stale counts? |
| Why it matters | Pooling stores or treating unavailable data as zero can hide remaining physical inventory. |
| Recommended default | Qualify per store/variation only when status is No Future Reorder and fresh current inventory is greater than zero. Zero inventory does not qualify but leaves lifecycle unchanged. Unknown/critical inventory appears separately as `Unable to determine` and is excluded from totals. Summary units/value sum store rows; product count is labeled as store-product count. Archived status alone never qualifies. |
| Approving recommendation | Preserves store isolation and the exact intended report definition without inventing zeroes. |
| Main alternative | Pool inventory company-wide or omit unknown rows; simpler totals but obscures where stock exists and can falsely clear products. |
| Blocks | **P6** |
| Safely deferred? | Yes, until P6. |
| Dependencies | IL-01, IL-03, approved freshness policy `ORD-DEC-018`. |
| Exact register wording | **Stagnant Inventory qualifies a store-variation only when lifecycle is No Future Reorder and fresh current store inventory is greater than zero. Zero inventory does not qualify or change lifecycle; unavailable or critical inventory appears as Unable to determine outside totals; stores are never silently pooled; Archived alone never qualifies.** |

## IL-11 — Default workspace queues, sort, and archived visibility

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-035` |
| Owner disposition | **APPROVED WITH MODIFICATION** |
| Plain-language question | Which queues and sorts should owners see first, and where do archived products appear? |
| Why it matters | The current all-products table treats urgent and historical rows equally and renders excessive output. |
| Approved rule | Actionable contains active products with actionable need above zero and sorts by need descending, then stable product/variation. Review Required orders critical/blocked, stale/informational, low confidence, then remaining named reasons with stable ties. All Active includes all non-archived products, including No Future Reorder, and sorts by product name then variation. Archived products remain separate. |
| Approving recommendation | Creates an operational work queue without deleting access to complete active evidence. |
| Main alternative | Keep one all-products table; simplest behavior, but prioritization and archived separation remain inefficient. |
| Blocks | **P4** |
| Safely deferred? | Yes, until P4. |
| Dependencies | IL-01, IL-03, IL-04. |
| Exact register wording | **Ordering Intelligence provides Actionable, Review Required, and All Active queues. Actionable contains active products with actionable calculated need greater than zero and sorts by need descending then stable product/variation. Review Required orders critical/blocked, stale/informational, low confidence, then remaining named review reasons with stable ties. All Active contains every non-archived product including No Future Reorder and sorts by product name then stable variation. Archived products appear only in the recoverable Archived Products view.** |

## IL-12 — Select-all scope

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-035` |
| Owner disposition | **APPROVED** |
| Plain-language question | Does `Select all visible` mean the current rendered page or every record matching the filters? |
| Why it matters | Acting on unseen pages makes one confirmation much riskier and complicates concurrency guarantees. |
| Recommended default | Current rendered page only. A future all-filtered-matches action would require a separate explicit confirmation showing the resolved count. |
| Approving recommendation | The owner confirms exactly the rows currently visible and avoids accidental mass archive. |
| Main alternative | Select every filtered match; faster for very large cleanup but can affect unseen products and needs snapshot/conflict semantics. |
| Blocks | **P2, P4** |
| Safely deferred? | Yes, until P2/P4. |
| Dependencies | IL-06, IL-11. |
| Exact register wording | **Select all visible selects only the current rendered page. Any future all-filtered-matches action requires a separate explicit confirmation that displays the server-resolved count.** |

## IL-13 — Page/batch limits and precise workload filtering

| Field | Owner choice |
|---|---|
| Register IDs | `ORD-DEC-031`, `ORD-DEC-035` |
| Owner disposition | **APPROVED** |
| Plain-language question | What limits should constrain pages and bulk actions, and which work may lifecycle filtering actually skip? |
| Why it matters | Limits protect usability and transaction size; endpoint-specific rules prevent archive from being overstated as a universal Square optimization. |
| Approved rule | Page choices are 25, 50, 100, and 250; default 50. Atomic lifecycle batches allow at most 250 variations and oversized requests are rejected server-side. Archived products are excluded from calculations and eligible variation-scoped inventory reads. No Future Reorder retains inventory and sales retrieval. Current catalog and location/date order-search behavior remains until separately approved. |
| Approving recommendation | Establishes bounded operations and permits remeasurement of the exact variation-scoped savings. |
| Main alternative | Larger/unbounded pages or treating archive as UI-only; increases response/transaction risk or produces no meaningful Square-history reduction. |
| Blocks | **P2, P3, P4, P5** |
| Safely deferred? | No later than P3; P1 data modeling can proceed without it. |
| Dependencies | IL-01, IL-03, IL-04, IL-06. |
| Exact register wording | **Ordering workspace page-size choices are 25, 50, 100, and 250 with a default of 50; unlimited pages are prohibited. An atomic lifecycle batch allows at most 250 variations and oversized requests are rejected server-side. Archived variations are excluded before recommendation calculation and eligible variation-scoped Square inventory-count and inventory-change reads; catalog integrity and recovery metadata remain available, and the current location/date order search is not represented as variation-filtered. No Future Reorder retains inventory and sales reads.** |

## IL-14 — Automatic zero-inventory archive

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-036` |
| Owner disposition | **APPROVED** |
| Plain-language question | Should a No Future Reorder product archive automatically when inventory reaches zero? |
| Why it matters | Stale or incomplete inventory could otherwise hide a product incorrectly without owner action. |
| Recommended default | Disabled. Consider only in a later milestone with fresh zero inventory at every active store, dry-run preview, configuration, audit, and explicit approval. |
| Approving recommendation | Initial lifecycle remains entirely owner-controlled; no automatic transition is introduced. |
| Main alternative | Enable automatic archive now; reduces cleanup but creates high stale-data and recovery risk. |
| Blocks | None of P1–P6; P6 excludes zero inventory without changing lifecycle. |
| Safely deferred? | Yes. |
| Dependencies | Future durable read model, monitoring, approved automation runbook. |
| Exact register wording | **Automatic zero-inventory archive is disabled. Any future enablement requires separate approval, fresh confirmed zero inventory at every active store, dry-run preview, configuration, audit, monitoring, and a recovery runbook.** |

## IL-15 — Shelf-location ownership

| Field | Owner choice |
|---|---|
| Register ID | `ORD-DEC-032` future-field boundary |
| Owner disposition | **APPROVED WITH MODIFICATION** |
| Plain-language question | When shelf location is later introduced, is it global, store-specific, or owned outside Ordering? |
| Why it matters | Prematurely choosing a column shape could place a physical store fact at the wrong scope. |
| Approved rule | Shelf location is outside Ordering ownership and belongs to a future Inventory Management data model. Ordering and Stagnant Inventory may consume it later; no lifecycle field is added. |
| Approving recommendation | Avoids speculative schema and keeps the first report read-only. |
| Main alternative | Add a global shelf-location field now; simpler schema but incorrect for products placed differently by store. |
| Blocks | None of P1–P6; the first P6 report omits the future field. |
| Safely deferred? | Yes. |
| Dependencies | Future store merchandising/location ownership design. |
| Exact register wording | **Shelf location is outside Ordering ownership and belongs to a future Inventory Management data model. Ordering Intelligence and Stagnant Inventory may consume it later, but the lifecycle table does not own or store shelf location.** |

## Approval record

Owner review completed on 2026-07-25. IL-01–IL-06, IL-08–IL-10, and IL-12–IL-14 were approved; IL-07, IL-11, and IL-15 were approved with the modifications recorded above. This approval resolves policy for implementation planning only and does not authorize implementation.
