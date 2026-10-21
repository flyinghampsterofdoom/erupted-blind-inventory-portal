# Purchasing V3 — exception resolution and final local integration

Date: 2026-10-18. **OWNER DECISION REQUIRED.** Local implementation and compatibility rehearsal are complete. No production deployment, migration, feature activation, financing change, payment, PO or Square mutation occurred.

Local review: **http://127.0.0.1:8764/management/purchasing/attention**. This runs a separate development database, synthetic financial records and mocked Square, with external network connections blocked. The original 8763 preview/database was preserved. Do not infer production account IDs or financing decisions from Demo account labels.

## A. Needs Attention

Purchasing now has an owner-only indicator and compact V2 table with search, twenty-row pagination, horizontal scrolling, collapsed evidence and individual/bulk confirmation. Select-all applies to filtered financing exceptions across pages (up to 500 decisions per atomic operation). Suggestions use Square category evidence and are never automatically selected or applied.

Initial supported inputs:

| Exception | Owner action / protection |
|---|---|
| Ambiguous current consignment SKU | Confirm an existing financing account; saved received financing cannot be relabeled. |
| Missing/conflicting current unit COGS | Enter a verified current cost; original configured/received evidence is retained, not overwritten. Changed authoritative cost evidence requires renewed review. |
| Missing card role mapping | Select a distinct, existing active card account with a valid payment method. No account is created automatically. |
| Conflicting received financing | Review evidence and confirm a prospective choice if useful. Current mixed/unknown lot ownership remains Incomplete. |
| TERMS-related weekly sales uncertainty | Separate read-only weekly COGS warning when relevant sales exist. A current SKU choice cannot assign historical sales to terms or a card. |

One narrow table, `purchasing_owner_decisions`, persists FINANCING/COST/CARD inputs. It separates explicit owner input from observed Square identity and historical financing. Existing `audit_log` records actor, timestamp, request identity, evidence token, previous value, replacement value and version. No new ledger, payment writer, replenishment engine, approval hierarchy or generalized workflow framework was added.

Saves validate fresh evidence, use optimistic versions and ordered PostgreSQL transaction locks, and audit the whole batch in the same transaction. A committed retry returns the original result; reusing a key for different content conflicts. Stale/concurrent decisions cannot silently overwrite one another. Card-role decisions serialize with Monday payment saves. Missing data remains unknown; unrelated complete valuations continue.

Owner choices survive refreshes and navigation. A changed product name alone does not reopen an exception. New conflicting canonical vendor, cost or received-financing evidence does. A SKU choice never resolves indistinguishable mixed financing lots or changes old POs/payments. Future intended financing follows the saved owner input through the existing purchasing session/group contract; quantity overrides and supplier decisions remain separate.

Fresh read-only verification at **2026-10-18 13:25:44 UTC** reconfirmed **167** dual Square associations, **131** existing uniform Big assignments and **36** unresolved IDs. All affected variations were present. The local preview represents those 36 IDs with synthetic costs/counts and Demo accounts. No owner assignment was applied to production.

Browser verification:

- Initial list: 36 exceptions. Canceling a confirmation saved nothing.
- Individual confirmation: one decision persisted, count became 35.
- Bulk confirmation: two decisions persisted. A simulated lost acknowledgement after commit produced a visible error; retry used the same request key and created no duplicate decisions/audit batches.
- Reload retained three decisions and 33 remaining exceptions. The preview intentionally retains those three **local test decisions**.
- Cross-page select-all selected all 33 remaining rows in one action; its account-named confirmation was canceled without saving.
- Database evidence: **3 decision rows, 2 audit batches (1 + 2 decisions), 0 payment rows, 0 actual activity rows, 0 POs** in this disposable preview.
- At a 390px viewport, the page did not overflow horizontally; the table scrolled inside its container. Temporary viewport override was reset.

Screenshots and `browser-verification.json` are under `output/purchasing-final-integration/`: `needs-attention-36.jpg`, `needs-attention-final.jpg`, `bulk-confirmation.jpg`, `retry-after-commit.jpg`, `persisted-33.jpg`, `attention-mobile.jpg`.

## B. TERMS correction and calculation integrity

Root cause: `consignment_inventory_values` treated saved `INVOICE / TERMS` as unknown and warned both consignment accounts. It now recognizes that verified financing as non-consignment, alongside invoice/card/wire/debit. TERMS/card combinations therefore remain outside Big/Streamline valuation without inventing a card assignment. A combination that actually includes consignment remains Incomplete for the affected account.

Regression fixtures cover the representative **108 TERMS-only + 12 TERMS/card** cohort. Big/Streamline values remain independently verified at **$1,600 / $370** in the four-store fixture. Owner-confirmed current assignment and missing-cost resolution trigger fresh valuations, while payments, credits, unpaid reports, weekly recommendation tokens and original financial records remain independent. Tests verify preservation of **17 financial/PO/receiving/sales tables** when saving decisions.

The formula remains **Σ(current Square IN_STOCK on-hand × authoritative SKU unit COGS)**. Saved valid received financing remains primary for current stock. A current owner fallback applies only where it cannot rewrite separately identified received financing. Historical PO costs remain unchanged; a verified current-cost decision has explicit scope/provenance and is invalidated by changed authoritative evidence. Existing explicit cash exceptions remain protected. Purchasing recommendations still use the existing engine; resolving supplier identity changes verified inputs rather than introducing another formula.

Sources: `app/services/purchasing_attention_service.py`, `purchasing_payments_service.py`, `purchasing_session_service.py`; owner-only routes in `app/routers/purchasing.py`; template/static assets under `app/templates/purchasing/attention.html` and `app/static/v2/purchasing-attention*.js`.

## C. Production compatibility

Live web and cron were rechecked read-only at **13:25:37 UTC**: both remain on **529df6f566b780e2e8ab5d470c160fb481d06715**, schema **20261016_0037**. The local integration preserved the 145 paths changed after Purchasing diverged, including later scheduling, dated allocations, historical scheduling, eligibility, employee/store access and inventory behavior. Critical scheduling/security/access/inventory paths match the production source byte-for-byte. Every production model remains present; the only existing model with a Purchasing extension is `PurchaseOrderReceipt` (additive event fields). **All 44 already-applied production migration files remain byte-identical.**

The single forward chain is:

```
20261016_0037  (existing production scheduling head)
  → 20261017_0032  Purchasing evidence / actual-card authority
  → 20261017_0033  Receiving event evidence
  → 20261017_0034  Purchasing sessions
  → 20261017_0035  Monday payment batches
  → 20261018_0038  Owner decisions (new integrated head)
```

Only the **unapplied Purchasing 0032** parent was changed from the obsolete fork to production 0037. Production migrations were not edited or stamped. Web and cron now share the strict `20261018_0038` schema contract; schema validation remains enabled.

An isolated copy of the production migration schema was seeded with synthetic PO, receiving and scheduling/approval records, upgraded forward and discarded. **All 178 pre-existing table fingerprints matched** afterward (new nullable receipt fields excluded from old-column comparisons). No automatic financial backfill/posting occurred. Actual ASGI startup/login succeeded. The enabled cron entrypoint passed its schema gate with a **local no-op scheduler**; no live automation ran.

Read-only live schema metadata matched the isolated production reference semantically after accounting for the four previously documented absent V1 checks, extra named NOT NULL representations verified against identical column nullability, and extension-version differences. Physical column/enum order was not compared in this targeted check. Production is PostgreSQL 18; this rehearsal used local PostgreSQL 17. An exact PostgreSQL 18 backup/restore rehearsal remains a deployment prerequisite, not a result claimed here.

Unreleased old branch-only local databases must not be assumed to contain the newly integrated production ancestry merely because their revision says 0035. Use fresh isolated fixtures or an explicitly reviewed local recovery path; do not stamp over missing migrations. The new preview uses the integrated schema.

Evidence: `integration.json`, `production-source-preservation.json`, `production-schema-comparison.json`, `forward-rehearsal.json`, `rehearsal.log`, `live-release-check.json`, `fresh-initial-exceptions-verification.json`.

## D. Verification results

| Run | Result | Interpretation |
|---|---|---|
| Integrated full application suite | **1,799 passed; 4 failed; 1 skipped; 7 subtests passed** | `full-regression.log/xml`, 1,110.29 seconds. |
| Untouched live-production source baseline | **Same 4 failures** | `production-baseline-employee-access.log`; independent immutable source export. |
| Final affected operational/integration suite | **615 passed, zero skips** | `final-affected.log/xml`: Purchasing, 500-SKU interface, PO/PDF, receiving/partial shipments, retry/concurrency protections, finance, Square gateway, V2 shell, schema migrations and release entrypoints. |
| Final owner/valuation/Monday checks | **58 passed, zero skips** | `final-owner-valuation.log`: final decision/cost/card guards, generation/refresh, historical preservation and weekly payment concurrency/idempotency. |
| Final exception detail check | **15 passed, zero skips** | `final-attention-details.log`; includes the final vendor-specific cost explanation. |
| Isolated forward migration/startup rehearsal | **PASS** | 178 historical table fingerprints preserved; web and no-op cron accept new head. |
| Browser verification | **PASS** | Individual/bulk persistence, cancellation, after-commit retry, reload, cross-page selection and narrow viewport. |
| Static checks | **PASS** | Python compilation, JavaScript syntax and diff whitespace checks. |

Runs overlap and must not be added as independent test counts. The full run preceded the final localized guards/UI polish; the final affected and owner/valuation runs cover the changed paths.

The four failures are `tests/test_employee_access.py::test_own_schedule_scope_roles_and_denies` for STORE, LEAD, MANAGER and ADMIN: the test expects three occurrences of `9:00 AM`, while the current production-aligned page renders two. All four reproduce against the **untouched deployed commit**, with its original schema and tests. No scheduling implementation or fixture was changed to suppress them. They remain failed tests, not passes, and require baseline-test disposition before declaring an entirely green release suite.

The sole skip is `test_real_r2_private_delivery_and_authentication_separation`, opt-in via `RUN_REAL_R2_TESTS=1`. Only production-bucket credentials are available. The unchanged test uploads into the real media namespace, so it was not run against production storage. A dedicated isolated private bucket/credentials or an explicit scoped acceptance exception is still needed; this skip is not a pass.

## E. Remaining owner decisions and deployment gates

| Item | Required decision / action |
|---|---|
| Big / Streamline canonical configuration | Verified identities: Big account 1/vendor 40/42STABRGTKEP4PSC; Streamline account 3/vendor 32/JYKK2W5FJUPOMDPQ. Configure reviewed values only during authorized activation. |
| Credit Card 1 | Confirm whether existing account **2**, Chase Visa Inventory / payment method **1**, is Card 1 or Card 2. No role was guessed. |
| Other credit card | A second active card funding account/payment method is missing in inspected production data. Identify/configure it through the existing authorized workflow; the UI will not create one or reuse account 2 for both roles. |
| Owner principal | Principal 6 is active ADMIN, but owner identity/configuration still requires confirmation. |
| 36 ambiguous SKUs | Owner must confirm actual production decisions through the new interface after an authorized rollout/configuration. Local demo choices are not production decisions. Real mixed financing stays Incomplete. |
| Missing current configured costs | Verify **S491722** (IMRBatteries) and **H504649** (VaporBeast). No cost was invented. Historical PO 77/line 7083/SKU 7 remains preserved and needs identity/cost provenance review if required; it is not reconstructed as current stock. |
| Legacy weekly attribution | Missing dated receipt/financing evidence can still prevent complete sales-period attribution. Current SKU decisions must not retroactively supply it. |
| Baseline failures / R2 | Decide disposition of the four reproduced production baseline assertions and provide isolated R2 verification or accept a documented scoped exception. |

Before any deployment: freeze/review an immutable release commit containing all new files; perform the PostgreSQL 18 restore rehearsal, verified database and document/PDF backups, feature/configuration review, and a coordinated web/cron migration window. Actual card checkpoints require owner-verified issuer balances; never initialize them from PO estimates. Review legacy financial writers during cutover. No V3 production flags were activated.

Use the prior deployment plan's non-mutating smoke checks only: existing pages/documents/history, read-only Square counts and permissions. Do not generate real POs, submit receiving, save payments or run stock adjustments as smoke tests without separate authorization.

Rollback must retain the additive schema and decision/financial/receiving history. Prepare a schema-compatible fallback from this integrated code; old production or old Purchasing binaries enforce incompatible heads. After actual-card authority is activated, do **not** turn off Phase 1 to fall back to legacy balance semantics. Hide/disable Phase 2 and Monday entry points as authorized while retaining the sole balance authority. Do not downgrade populated evidence or automatically restore a backup over legitimate later transactions. Coordinate cron suspension/resumption with the schema-compatible binaries.

**Recommended next action:** owner review of the local interface and remaining mappings/data decisions, then a separately authorized controlled deployment window once the stated verification and recovery gates close. No additional feature work or deployment is authorized by this completion report.
