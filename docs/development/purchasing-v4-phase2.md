# Purchasing V4 Phase 2 candidate engine

October 27, 2026. Isolated development only, based on accepted Phase 1 commit `bf80edae0e3eef7b629c8c5663d31b170ba76f91`. This module has no application route, scheduler registration, production configuration, supplier-assignment writer, purchasing commitment writer, inventory writer, vendor delivery or manual-rule conversion. It does not replace the running purchasing engine. No operational numerical profile is installed.

## Architecture and reproducibility

`calculation_source.load_snapshot` reads the accepted source and immutable internal variation references in one database-enforced repeatable-read, read-only, UTC transaction. Fourteen fixed data SELECTs cover catalog, active stores, coverage, source failure status, inventory, gross sales, corrections, movements, incoming allocations, lifecycle, explicit legacy vendor configurations, pending receiving operations, manual rules and mapping exceptions. The count is independent of catalog size; transaction guards add SHOW statements. No per-product SQL is issued. Historical availability, inventory qualification and incoming allocation rules are shared with the Phase 1 single-record adapter, preventing two competing evidence interpretations.

The extracted immutable `StoreInput` includes typed, dated sales and availability, availability reasons/references, current inventory, coherent supply evidence, manual policy and source exceptions. Missing records stay unknown/unavailable. Product identity is the Phase 1 immutable internal variation ID. Store identity remains the existing store ID. Missing internal references are reported and excluded without inventing an identity. Explicit reporting-category attributes, or a sole retained category reference, and explicit primary-vendor pack evidence are read-only source observations; the loader does not establish supplier authority. Ambiguous manual references block possible affected calculations, without binding a rule by SKU. Unresolved manual scope/quantities cannot fall through to automatic policy.

`calculation.recommend_batch` is a pure function with explicit versioned configuration, aware cutoff and evidence digest. It imports no database, HTTP client, ambient settings, wall clock or application service. It returns observed evidence, forecast estimates, unit recommendations and pack recommendations as separate fields. Nothing authorizes an order. `calculation_serialization` represents rationals as numerator/denominator pairs, Decimals as strings and timestamps with offsets. Sorted canonical JSON produces deterministic SHA-256 digests for evidence and results. Category/global/product policy provenance and configuration digest travel with every result.

`calculation_cli` is an explicit offline on-demand entry point. Required input and profile files are never discovered implicitly. It validates the complete input and calculation before atomically publishing an output file. It fsyncs a temporary sibling, replaces the previous complete artifact, then fsyncs the directory. Validation/calculation failure preserves the previous output. Retrying identical evidence produces identical bytes. There is no background generation.

## Complete mathematical specification

All sales, inventory, thresholds, targets, needs and weights use exact Decimal-to-Fraction arithmetic. Integers are individual units unless marked packs. No intermediate unit rounding occurs.

Let `t` be the local America/Los_Angeles date containing the aware generation cutoff. Only completed local dates before `t` enter training. Explicit parameters are lookback `L`, recent length `R`, minimum available days `m` (initial approved value 14), baseline recent influence `a0`, upward/downward sensitivities `s_up/s_down`, upward/downward trend limits `c_up/c_down`, reliability depth multiple `k`, stockout candidate control and negative-inventory candidate treatment. `0<R<L`, `0<m<=L`, `k>0`; all five influence/sensitivity/limit proportions lie in `[0,1]`. No implicit V3 weight, spike clipping, lead time, aging penalty or zero-demand override is used.

The older window is `[t-L,t-R)` and recent window `[t-R,t)`. A known listing boundary trims both without counting pre-listing days as evidence. Unknown listing date is disclosed. Future history is excluded. For each window `W`:

- `D_W` is the number of eligible completed dates in the window after listing trimming.
- `n_W` counts only confirmed available dates with known nonnegative gross completed-sale quantity observed by the cutoff.
- `S_W` sums quantities on those dates. Known sales on other dates are retained separately as observed units, never substituted for qualified exposure.
- Confirmed full-day stockouts contribute neither numerator nor selling-day denominator. Unknown availability, missing days, unavailable sales and future observations never count as available. Positive sales on an alleged full-day stockout make that day contradictory/unknown.
- `V_W=7*S_W/n_W` units/week when `n_W>0`. Without available dates, velocity is unknown, not zero. Qualified zero sales produces estimated zero only after sufficiency checks.
- A window initially qualifies when `n_W>=m`; a selected explicit stockout quality gate can additionally disqualify it.
- `r_W=min(1,n_W/(k*m))*n_W/D_W` for positive `D_W`, otherwise zero. This is an explainable evidence index, not calibrated probability/confidence. Calendar duration appears only in evidence completeness, never as the sales-rate denominator.

When older and recent windows both qualify, define `H=V_H`, `J=V_J`, direction by comparison, and `z=abs(J-H)/(J+H)`, or zero when both rates are zero. Set `a*=min(1,a0+s_direction*z)`; stable uses `a0`. Reliability-adjusted recent influence is `a=(a* * r_J)/(a* * r_J+(1-a*)*r_H)`. A zero denominator invokes the qualified pooled branch. Final weight `w=min(a,c_direction)` for a trend; stable uses `a`. Forecast `F=(1-w)*H+w*J`. It is finite, nonnegative and lies between the supported rates. Increasing and decreasing controls are independent. No extrapolated trend or confidence interval is invented.

If either comparison window is unqualified but pooled `[t-L,t)` qualifies under the same evidence/control rules, use `F=V_pooled` with `LIMITED_TREND_HISTORY`. Otherwise abstain. Store history and qualification are independent; no demand pooling across stores or suppliers occurs. Source freshness and availability qualification remain separate from minimum demand history.

Three Stockout Adjustment interpretations are implemented as explicit evaluation inputs:

1. `confirmed_only`: the baseline confirmed-exposure model, without an extra numerical gate.
2. `minimum_available_share(q)`: require `n_W/D_W>=q`, in addition to `m`. This penalizes both confirmed stockouts and unknown evidence, so it can reject otherwise supported conditional demand.
3. `maximum_unknown_share(q)`: require `unknown_W/D_W<=q`, in addition to `m`. Unknown includes unavailable/missing sales on allegedly available dates. Confirmed stockouts remain distinct.

Neither gate creates lost sales, changes the demand-rate denominator or reclassifies unknown exposure. Thresholds are explicit trial values; no operational meaning is installed. Duration-zero windows cannot qualify under positive `m`.

## Inventory, policy and final packs

Inventory position `IP=OH-R_out-Hold+U+C+T`. `OH` is known observed current stock, `R_out` outward reservations and `Hold` unavailable stock already represented in `OH`. `U/C/T` are intended-store vendor-open, central or in-transit entitlements not yet represented in destination stock. Quantities require distinct nonoverlapping identities, nonnegative integral supply amounts, appropriate stock representation and a coherent stock/supply watermark. Deductions cannot exceed represented positive stock. Unknown/unavailable/future observations, overlap, already represented incoming and pending arrival/posting make position unknown. No unknown bucket becomes zero. Negative observed stock is preserved with a visible anomaly under one candidate, or causes abstention under the explicitly selected alternative. Neither interpretation repairs inventory. Fractional observed stock is preserved exactly and flagged.

The retained legacy adapter projects only its verified open-PO allocation evidence into `U`; it does not invent central/transit stage facts absent from the source. Modern receiving postings make coherence unknown until success is observed and a qualified count watermark proves the posting is represented. Success alone cannot make a stale pre-posting count coherent. Exact receipt-allocation references explain the block. U→C→T→arrival-pending→OH conservation is verified with explicit pure fixtures; a future receiving adapter must supply those verified stages. The two PO 129 allocation conflicts remain unknown for every possible destination. PO 77's invalid old identity has proved no remaining obligation and is not spread to unrelated products. No PO, receipt or financial row is rewritten.

Policy precedence:

1. Global No Reorder: skip forecasting and return zero suggested packs, while manual addition remains permitted.
2. Unresolved legacy manual policy: visibly abstain; do not replace it with forecasting.
3. Explicit store manual rule: `P=PAR_units`, `T=LEVEL_units`, independently per store. History is unnecessary. Candidate valid boundary is `0<=PAR<LEVEL`; equal/reversed historical values remain unresolved pending policy acceptance.
4. Automatic: choose weeks from explicit authorized product override, else category coverage override, else global coverage. `P=F*reorder_weeks`, `T=F*target_weeks`, with `0<reorder_weeks<target_weeks`. Global numerical parameters can have independent category overrides; removing one override restores inheritance without erasing others. No vendor-level numerical defaults are inherited.

If position/target are known, `need=max(T-IP,0)` when `IP<=P`, else zero. The trigger is inclusive. Unknown demand/position yields unknown need, zero suggested packs and no claimed actual-unit result. At observed zero stock with unknown automatic demand, the informational prompt is `Please set a PAR and LEVEL`. A missing/invalid pack is unresolved separately. Mapping/cost issues remain visible in result reasons even when they do not prevent unit arithmetic.

For positive pack size `p`, independently per store: `packs=0` if `need=0`; otherwise `max(1,floor(need/p+1/2))`. `actual_units=packs*p`. This is approved nearest-pack, half upward, with at least one pack for any positive need. Never ceiling all needs or pool stores before rounding. An unresolved row's displayed zero suggestion is not a measured zero need.

`compare_manual_history` requires explicit owner-triggered invocation. It returns manual calculations and automatic counterfactuals, every conversion choice false and conversion performed false. It has no policy writer. Persistent manual rules never expire or convert automatically. The boolean calculation guard is not an authentication endpoint; any future UI must retain actual owner authorization.

## Independently checked examples

The mathematical test suite includes a separate 80-digit Decimal reference calculation over 100 seeded inputs; the reference computes direction, reliability, adaptive weighting and HALF_UP pack conversion independently of engine helpers.

| Input | Result |
|---|---|
| Older20 units/14 available dates; recent40/14; each spans28; `a0=2/5,s_up=3/5,k=2`, caps1 | H10, J20, both reliability1/4, final weight3/5, F16/week |
| Same rates; older reliability1/recent1/2 | Weight3/7, F100/7/week |
| H20,J10, `a0=2/5,s_down=3/10`, equal reliability | F15/week |
| Manual PAR5/LEVEL20, OH3+incoming2, no sales | IP5, need15 units, pack5→3 packs |
| Needs1,6,8,3 across four stores; pack5 | 1,1,2,1 packs; vendor sum5 |
| Same evidence shape with store sales multipliers1,2,3,4 | F16,32,48,64/week |
| CentralOH100, outward70, hold5 | IP25; destination's70 remains incoming |
| Pending arrival or unresolved PO129 allocation | IP/need unknown; suggested0 with explicit reason |

## Evaluation, performance and operational limits

Run the reproducible tool with the checked-in explicit candidate file:

```sh
PYTHONPATH=. python scripts/evaluate_purchasing_v4_phase2.py \
  --profiles tests/fixtures/purchasing_v4_phase2_profiles.json \
  --output /absolute/isolated/evaluation \
  --isolated-url postgresql+psycopg://postgres@127.0.0.1:PORT/erupted_v4_phase1_NAME \
  --retained-as-of 2026-10-27T20:42:07.698278+00:00 \
  --benchmark-products 1322 2500
```

The source URL must pass the existing loopback/disposable-name guard. Profiles are reproducible trials, not installed defaults. Synthetic holdouts use seven generation origins, 17 cohorts, four stores and 28 future dates, scoring only at least14 confirmed available outcome dates. Report bias, MAE, WAPE (undefined for all-zero outcomes), abstention, directional gap response and pack changes between origins. Do not treat abstaining more as proving better quality. The retained snapshot's historical availability limitations prevent defensible real-world quality selection; error and bias remain null where outcomes are unqualified. Synthetic examples establish behavior, not commercial usefulness or counterfactual lost sales.

The benchmark builds full182-date histories at realistic and larger catalog sizes, measures three generation runs and records process memory high-water mark, hardware, input digest and deterministic result digest. Evidence loading/digesting and artifact serialization are separately identified from pure generation. There is no UI/keystroke or production latency claim. Whole snapshot materialization can consume significant memory; bounded snapshot caching/chunking must be evaluated before runtime integration.

## Recovery and compatibility

There are no Phase 2 database migrations or schema changes. Public head remains `20261027_0053`, separate foundation head `p1_0001`. Accepted Phase 1 identities, all legacy financial/purchasing rows, source caches, sequences and all foundation records are reconciled before/after isolated evaluation. Tests create and destroy only prefixed disposable databases. The retained accepted database is read-only.

Rollback is removal of the unintegrated candidate source/artifacts or use of accepted Phase 1 commit. No database down-migration, order cancellation, replay of commitments or compensating financial write is necessary. Failed offline artifact publication retains the last complete artifact; same evidence/config replay is forward recovery. Source failures remain unresolved until independently verified evidence changes. Deployment migration/rollback proposals remain unauthorized.

The original582 runtime files are checked against their accepted release manifest; original web schema startup is exercised with database-enforced read-only settings and cron is imported without executing a job. This is source/runtime compatibility qualification on macOS and PostgreSQL18; it is not a native Linux deployment build or production rollout test.
