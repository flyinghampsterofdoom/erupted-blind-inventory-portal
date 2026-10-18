# Big Wholesale / Streamline SKU attribution investigation

Date: 2026-10-18. Scope: read-only production/Square evidence; no implementation changes.

**The 167 current Square overlaps are real. Of those, 131 have an existing authoritative Big Wholesale financing override; 36 remain unresolved. The separate 120 exceptions are non-consignment invoice financing, not 120 Big/Streamline ownership conflicts.** The previous readiness wording overstated the latter finding: it described a valuation classifier's unknown results as unresolved consignment attribution.

## Exact reconciliation

The two original cohorts are disjoint: 167 + 120 = **287 unique canonical Square variation IDs**. Every variation is represented once in `output/purchasing-attribution-investigation/all-287-reconciliation.csv`.

| Requested measure — original overlap cohort | Count |
|---|---:|
| Originally reported overlaps | 167 |
| Genuine current Square overlaps | 167 |
| Historical-only overlaps, absent from current Square | 0 |
| Conflicting saved consignment financing assignments | 0 |
| Resolved through existing authoritative financing evidence | 131 |
| Remaining unresolved | 36 |

The first two rows describe the **same** 167, not independent populations. Their mutually exclusive disposition is **131 resolved + 36 unresolved = 167**. A dual Square association is a purchasing/identity ambiguity, not proof of two owners or a split financing lot. “Saved financing conflict” here means competing consignment assignments, or consignment versus other financing for the same qualifying received variation; a historical purchasing supplier differing from its saved financial vendor is not such a conflict.

| Primary classification — original 120 financing exceptions | Count |
|---|---:|
| Resolvable from current Square associations alone | 0 |
| Resolvable from existing authoritative financing assignment | 120 |
| Historical-only conflict | 0 |
| Genuine current Big/Streamline financing ambiguity | 0 |
| Missing/insufficient evidence for Big/Streamline membership | 0 |
| **Total** | **120** |

Current Square independently corroborates exclusion for **all 120**: each has one canonical vendor, outside Big/Streamline. That is supporting evidence for the same 120, not an additional resolved population. Saved invoice treatment takes primary classification precedence. The 120 comprise **108 TERMS-only** variations and **12 with both TERMS and CREDIT_CARD invoice lots**. Both financing forms are outside Big/Streamline consignment. This does **not** resolve which current units belong to a card versus terms, identify a particular card, allocate weekly sales COGS or settle an invoice.

Combined disposition for this investigation: **251 resolved for consignment membership/exclusion; 36 unresolved**. No automatic record assignment or repair was performed.

## Fresh Square evidence and historical comparison

Fresh catalog retrieval completed at **2026-10-18 12:42:36 UTC** through the existing `SquareOrderingReadGateway.fetch_active_catalog`, using policy-controlled `/v2/catalog/search-catalog-items` reads. The complete active catalog contained 1,322 variations. All 287 affected IDs were present; no affected SKU was missing from the fresh response.

For every one of the original 167 overlaps, the current variation's `item_variation_data.item_variation_vendor_infos` contains both:

- Big Wholesale: **42STABRGTKEP4PSC**, local vendor **40**, consignment account **1**.
- Streamline: **JYKK2W5FJUPOMDPQ**, local vendor **32**, consignment account **3**.

Both vendor-info records are explicitly **`is_deleted=false`** in all 167 cases. The raw record IDs, versions, update times, SKU, product/variation name, Square category IDs/names and product type are retained in `fresh-evidence.json`. The existing `square_vendor_ids` extractor returns these two IDs directly; neither historical POs nor local supplier tables enter that extraction. **The earlier 167 count was not produced by a historical PO join, a local cache or name/brand inference.**

Historical Square reads were also available. Using the existing policy-controlled Square GET client, 67 parent items were retrieved at two historical catalog-version cutoffs (134 requests), covering all 167 affected variations:

| Historical/current Square observation | Streamline only | Big + Streamline |
|---|---:|---:|
| Catalog version 1785610569172 — 2026-08-01 18:56:09.172 UTC | 167 | 0 |
| Catalog version 1789162890562 — 2026-09-11 21:41:30.562 UTC | 0 | 167 |
| Fresh current catalog — 2026-10-18 | 0 | 167 |

Returned item and nested vendor-info versions were checked not to exceed the requested historical cutoffs. These snapshots establish a transition from Streamline-only to both associations between the observations, with both persisting now. They do not establish the actor, business purpose, exact edit time or financing ownership. Current vendor-info `updated_at` timestamps are modifications, not proof of initial creation.

The 167 products have current Square categories describing **99 Freebase and 68 Salt** variations; none of this cohort has a Disposable category. Square product type is `REGULAR`. This is consistent with the owner's business context but is **not** used to assign ownership. The 120 other variations include e-liquids and 12 Disposable variations, likewise without using category as financing authority.

Historical Square snapshots were queried for the 167 overlap cohort. For the 120 exceptions, fresh unique vendor evidence and saved invoice records are sufficient for the scoped exclusion; this report does not claim a complete historical Square change log for that cohort.

## PO, financing, supplier and cache comparison

Production SQL ran in an explicitly verified **READ ONLY** transaction, with 8-second statement and 1-second lock timeouts. Bounded queries returned 1,259 historical PO-line rows, 454 local supplier rows and 287 cached catalog identity rows for the affected IDs. None reached its query cap.

All 167 overlap variations have local supplier mappings for **both** vendors. Every one has **Big Wholesale as its sole active default purchasing supplier**. Both suppliers' configured unit costs are equal for each of these 167 variations. Those defaults/costs are existing local data; they do not override saved financing or resolve a dual Square financing identity.

Historical purchasing records reference Streamline POs **98 and 107**, and Big Wholesale PO **129**. The recorded purchasing supplier is distinct from financing:

- **131 variations** have qualifying, unremoved, received lines on PO **107**, whose purchasing vendor is Streamline **32**, with saved OrderPayment **50** assigning financial vendor **40**, `REPLENISHMENT / CONSIGNMENT`. Under the approved existing valuation rule, these are a uniform **Big Wholesale** financing cohort despite the historical purchasing supplier and dual current Square associations. No competing qualifying received financing assignment was found for these 131. Preserve this override; do not reassign them to Streamline.
- The remaining **36** have no qualifying saved received-financing override. Historical PO supplier names, unassigned orders, removed lines, receipt totals, default supplier 40 and e-liquid categories do not establish current financing ownership. Their two current Square IDs remain genuinely ambiguous under the existing rules.

The effective-dated `vendor_variation_assignments` and `vendor_variation_costs` queries returned zero rows for the affected IDs. `vendor_assignment_changes` and matching receipt-line history also returned zero. Absence is not permission to reconstruct inventory ownership from historical order quantities. Cached `ordering_catalog_identity` rows contain product identity/timestamps, **not vendor ID associations**, so they cannot be the source of the two-vendor current result.

### Why the 120 were flagged

All 120 have saved qualifying `INVOICE` assignments to other financial vendors:

| Saved financial vendor | Unique variations | Financing evidence |
|---|---:|---|
| 2 — 7 Daze Mfg | 92 | 80 TERMS-only; 12 TERMS + CREDIT_CARD |
| 14 — Ejuice Enterprise | 23 | TERMS-only |
| 33 — USA Vape Lab | 5 | TERMS-only |
| **Total** | **120** | No Big/Streamline consignment assignment |

Current canonical Square IDs independently identify those other vendors. The valuation's existing classification at `purchasing_payments_service.py:318–324` recognizes `INVOICE + CREDIT_CARD/WIRE/DEBIT_CARD` as `other`, but routes **`INVOICE + TERMS` to `unknown`**. At lines 357–363, any `unknown` received category warns both consignment accounts and skips that SKU. Thus 108 TERMS-only SKUs become unknown and 12 terms/card SKUs become other-plus-unknown. This code path explains the exact 120; it is not evidence that Big and Streamline own that inventory.

For the 12 mixed terms/card SKUs, current unit ownership across those two non-consignment financing forms remains undetermined. Both forms nevertheless exclude Big/Streamline under their explicitly saved invoice treatment. Do not convert TERMS to a card, apply FIFO or change weekly payment attribution to suppress that distinction.

## Genuinely unresolved SKU report

**Only 36 cases remain unresolved for the requested consignment question.** See `output/purchasing-attribution-investigation/unresolved-36-skus.csv`. It contains canonical variation ID, SKU, product/variation, current vendor-info IDs, categories, historical Square observations, historical PO references, saved qualifying financing evidence, supplier mappings/defaults/costs and the specific unresolved reason.

Examples:

| SKU | Product / variation | Unresolved reason |
|---|---|---|
| 4066178 | Juice Head Salt Watermelon Strawberry / 50mg | Both current canonical vendors; no qualifying saved financing override |
| 6794711 | Juice Head Salt Strawberry Kiwi Freeze (Kiwi Berry) / 50mg | Same |
| 762876650160 | Juice Head Salt Guava Peach / 25mg | Same |
| J697712 | Juice Head Tropical POG Freeze / 6mg | Same |
| 756029734425 | Juice Head Mango Strawberry / 0mg | Same |

Names/categories are identifiers and context only. No account assignment was inferred from them. The other 251 investigated cases are recorded in the full reconciliation, not included as unresolved SKU exceptions.

## Purchasing and valuation impact

| Area | Finding / necessary response |
|---|---|
| Current supplier selection | All 167 already prefer Big locally, but `purchasing_session_service.py:159–160` only accepts a single canonical Square vendor for automatic selection. Dual associations therefore still enter vendor review. Existing explicit supplier selection can choose Big without redefining financing. No automatic reassignment was performed or recommended from product type. |
| Purchasing recommendations | The need engine itself has not been shown to be wrong. Dual vendor identity affects default grouping, purchase-cost selection, supplier lead-time input and purchase-multiple handling, so resolving supplier identity may change those inputs; identical raw quantities cannot be promised. Preserve the engine and owner decisions. |
| Big consignment inventory value | Existing saved-financing logic already assigns the 131 uniform qualifying variations to Big. The 36 ambiguous variations must keep the total incomplete until reliable ownership evidence is confirmed. The unrelated 120 warnings are overbroad and need a narrow classifier correction if separately authorized. |
| Streamline consignment inventory value | The 131 saved Big assignments must not also count for Streamline. The same 36 ambiguous cases prevent complete attribution. Removing false TERMS warnings does not resolve these real ambiguities. |
| SKU unit COGS | No cost formula change is required. All 167 have equal per-SKU configured Big/Streamline costs. Assigned 131 use the existing authoritative saved received-line costs. The 36 must not receive a financing cost until attribution is reliable. No Square cost was imported. |
| Weekly COGS recommendations | Separate sales-period calculation. This investigation does not establish dated sale-to-financing allocation, a card/terms unit split or complete legacy receipt coverage. Do not modify weekly recommendations or reuse current inventory valuation as sales COGS. |

The approved valuation stays **Σ(current Square on-hand × authoritative SKU unit COGS)**, once per reliably attributed variation. Vendor credits, payments and unpaid reports are irrelevant. No new numerical production valuation is asserted: ambiguity still prevents a trustworthy complete total.

## Minimal recommended resolution — not implemented

1. **Preserve existing financing.** Retain PO 107 / OrderPayment 50's Big assignment for its 131 variations. Do not change historical POs, finance or receiving records simply because the purchasing vendor differs.
2. **Confirm the 36 exact unresolved IDs.** The owner should review the attached SKU list and confirm authoritative financing evidence through an existing approved workflow. The business statement about e-liquid/disposable suppliers is context, not sufficient evidence of every current unit's financing. Keep Incomplete where a real mixed/unknown current split remains.
3. **Authorize only a targeted valuation classifier correction:** explicitly saved non-consignment `INVOICE / TERMS` evidence should not make both consignment accounts incomplete. Keep malformed/unknown treatments and genuinely mixed consignment financing incomplete. Do not alter weekly card/terms COGS allocation, payment recording or inventory quantities. Regress TERMS-only, card+TERMS, consignment+TERMS and missing-treatment cases before adopting a correction.
4. **Keep purchasing supplier decisions separate.** Use the existing explicit supplier-selection workflow while the dual current Square associations are reviewed. Any future automatic supplier resolution needs a separately reviewed rule; do not guess from vendor-list order, brand ownership, category or latest historical PO. No new accounting structure or Square writer is needed.

Verification: the offline reconciliation asserts all 287 IDs are unique across the two original cohorts; all 167 current overlaps contain both non-deleted vendor records; all 131 financing overrides are uniform Big consignment; all 120 scoped exclusions have explicit saved non-consignment invoice evidence and unique other-vendor current IDs; both historical snapshots cover all 167 with valid versions; primary disposition totals reconcile. Inspection/reconciliation scripts and a SHA-256 evidence manifest are included in the output directory. No application regression tests were rerun because no application implementation changed.

This investigation does not clear the separate production migration ancestry, card-account mapping or integration-test deployment gates. No deployment, migration, production record/configuration change, catalog mutation, inventory adjustment or implementation correction was performed. Investigation complete; await owner direction.
