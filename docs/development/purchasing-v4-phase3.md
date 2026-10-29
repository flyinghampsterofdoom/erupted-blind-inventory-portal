# Purchasing V4 Phase 3 owner controls

October 27, 2026. Isolated development only, based on accepted Phase2 commit `68209859783920c4da093142bcd5094f9c3f08ff`. The existing application does not import this management module. Its factory requires an explicitly identified loopback disposable PostgreSQL database; its CLI binds127.0.0.1. No production route, employee entry point, purchasing worksheet, purchase authorization, inventory writer, vendor delivery or cron registration was added.

## Policy ownership and persistence

The independent `purchasing_v4` migration chain advances from `p1_0001` through `p3_0001` and `p3_0002` to `p3_0003`. Public schema head remains `20261027_0053`. Accepted product/variation/supplier identities, historical policies, POs, allocations, receipts, inventory and financial tables remain unchanged.

Typed owner control kinds share a versioned aggregate store, rather than having separate ad hoc save implementations:

| Kind | Canonical scope | Owned data |
|---|---|---|
| GLOBAL | One `global` record | Explicit automatic parameters, global coverage weeks, optional source freshness seconds, review status |
| CATEGORY | Observed category reference | Independent parameter overrides and optional coverage; absent values inherit |
| PRODUCT | Immutable purchasable variation ID, all stores | Global No Reorder and optional product automatic coverage |
| MANUAL | Immutable variation ID + existing store ID | ACTIVE, INACTIVE or explicitly converted AUTOMATIC; integral individual-unit PAR/LEVEL |
| SUPPLIER | Internal authority supplier UUID | Purchasing profile, contacts, terms, descriptive minimum, notes and preferences |
| OFFER | Immutable variation UUID + internal supplier UUID | Owner-approved alternative, supplier pack, expected unit cost and provenance, optional textual Vendor SKU |
| ASSIGNMENT | One immutable variation ID, all stores | Explicit selected default supplier or explicit unresolved state |
| REVIEW | Immutable evidence item identity | Open/acknowledged status and notes; never a historical repair |

`control_head` is the current authority. Every change has an immutable `control_revision` with before/after values, actor, reason, version, command ID and timestamp. `control_command` retains the completed atomic result and request digest. Head writes require a matching audit revision. Revisions, command receipts, supplier identities, history checks and review evidence reject update, delete and truncate. Head removal uses a retained removal revision; manual rules cannot be removed this way.

Every write requires active existing ADMIN owner authorization, checked against the isolated database. The standalone HTTP app reuses existing password hashing and web-session validation, including inactive-account/employee and session expiry/revocation checks. It uses a separate isolated session cookie, CSRF verification, bounded login throttling and no login-triggered Square refresh. Read authentication does not renew sessions. Existing production authentication and role/capability behavior is unchanged. No Manager/employee delegation was introduced.

Commands carry an explicit UUID, audit reason and expected version for every affected record. Up to100 explicit changes form one atomic batch. A short transaction advisory lock serializes management commands whose dependencies can overlap; expected versions still reject stale clients. Related proposed records are validated as a complete batch. A nested transaction rolls back even an application-side failure caught by an enclosing caller. A same-ID/same-content retry returns the stored result; changed content under the same ID conflicts. The browser retains the command identity for a retry of the same pending edit.

Canonical database keys prevent competing per-store supplier assignments. Deferred relationship checks ensure an assignment has its approved offer and active owner profile when selected. An offer cannot be removed while selected unless the same atomic transaction resolves the default. Explicit supplier deactivation retains the selected identity/history and makes its authority unresolved; it does not select another supplier. Foreign keys/reference guards reject unknown product, supplier and store identities. Active manual policies are checked for integral `0<=PAR<LEVEL` in both application validation and PostgreSQL.

## Numerical configuration and inheritance

No automatic numerical profile is installed by migration or initialization. All model values come from explicit owner input. GLOBAL supports draft and owner-approved review status. `approved_configuration` provides an approval gate for future consumers; it does not activate anything. History comparison can use an explicitly entered draft, visibly labeled as a comparison. The approved initial minimum14 days remains configurable; unconfigured history/weights/coverage are not filled from V3.

Owner-facing descriptions cover historical/recent disjoint windows, minimum confirmed available days, recent influence, independent upward/downward sensitivities and limits, evidence depth, Stockout Adjustment meaning/threshold, negative-stock protection, reorder/target weeks and source freshness. Numeric ratios remain exact strings/fractions; binary floats are rejected. Optional source freshness is stored in seconds without a default. A history check must supply its own value when no global age is configured. Zero freshness means evidence at the cutoff, not missing-as-zero.

Category overrides inherit each absent numerical field independently; removing one does not erase others. Coverage is a validated reorder/target pair. Product automatic coverage overrides category/global coverage and carries its owner revision provenance. Manual unit rules remain independent of all automatic parameter/coverage changes. Changing a setting appends a revision; restore appends another revision under current version checks. A restore cannot bypass Use Automatic Engine or erase a manual policy.

Persistent configurations enforce the Phase3 negative-inventory direction: `abstain_on_negative`, including category overrides. The Phase2 research candidates remain reproducible for evaluation, while the owner control projection cannot authorize preserving an unreliable negative position. Actual negative counts remain visible; numeric purchasing need is withheld, with no inventory correction.

## Supplier authority and onboarding

`authority_supplier` reuses each accepted imported supplier UUID and provenance. A locally created supplier receives its own UUID with LOCAL origin and no fabricated Square or legacy vendor reference. Identity intake can register newly observed foundation suppliers without creating a profile, offer or default. Identity collisions fail rather than merge by name.

An owner-selected default is one global variation assignment. Square associations, legacy default flags, financing and historical POs are never fallback authority. Missing assignment returns UNRESOLVED_DEFAULT even with one catalog supplier. Approved alternatives coexist as distinct offers. A default requires an explicit owner-approved offer and an active owner supplier profile. Unknown cost and unknown pack remain null; they do not prevent choosing a supplier, but missing pack prevents a qualified pack recommendation. Known expected unit costs require exact decimal text and provenance. Vendor SKU remains optional text and retains leading zeros.

Profiles support name, primary/secondary contact, email, phone, address, Erupted account number, payment terms, optional descriptive order minimum, notes, active state, confirmation preference, spreadsheet/official-PO email preferences, preferred format and Vendor SKU usage. Confirmation/email settings start off for newly reviewed profiles and have no delivery worker. Order minimum is descriptive and never enters the forecasting formula. No profile/offer operation updates historical PO costs or accounting relationships.

The onboarding screen pages immutable Phase1 supplier-candidate evidence and its source observation: product identity, observed supplier, legacy default flag, locally maintained expected cost/pack and disposition. An owner must review and save an offer; approval is not preselected for a new relationship. Existing owner offer values take precedence over a new suggestion. Missing mappings remain unresolved. Opening a suggestion never selects a default. Legacy Our SKU is not automatically copied into Vendor SKU. Supplier defaults still require their separate explicit owner action.

## Manual Par/Levels and Check History

The dedicated management area has name/SKU search, store/state filters, bounded pages, visible selection, individual editing and atomic bulk editing/inactivation. Rules are absolute individual units per store. ACTIVE requires `0<=PAR<LEVEL`; INACTIVE is explicit and does not fall through to forecasting. Neither state expires. AUTOMATIC is reachable only through Use Automatic Engine after an owner history check. Original quantities remain in the current converted record as historical values and in all revisions.

Explicit catalog intake freezes original legacy MANUAL observations in immutable `legacy_manual_evidence`, including complete original records, source digests, exact mapped identities and separately labeled uncertainty scopes. Repeated intake never overwrites this baseline. Exact valid rules remain manual after a legacy source mode/value change; ambiguous or invalid frozen rules remain unresolved. Only explicit owner revisions supersede original intent. No owner head or numerical default is created by capturing evidence.

Explicit catalog intake freezes original legacy MANUAL observations in immutable `legacy_manual_evidence`, including complete original records, source digests, exact mapped identities and separately labeled uncertainty scopes. Repeated intake never overwrites this baseline. Exact valid rules remain manual after a legacy source mode/value change; ambiguous or invalid frozen rules remain unresolved. Only explicit owner revisions supersede original intent. No owner head or numerical default is created by capturing evidence.

Legacy rules appear in a separate review view with original IDs, values, stores and verified identity candidates. Original records are never rewritten. Individual review can create an explicit V4 rule. Bulk adoption requires an explicit selection of verified active-store references and valid unit boundaries, pinned source digests and expected owner versions. Ambiguous references and conflicting duplicate sources are rejected. The immutable command retains original source records/provenance. Adoption copies the same manual quantities; it is not a conversion. Existing owner rules cannot be overwritten by an old unreviewed source version.

Check History is owner-triggered for explicitly selected ACTIVE or INACTIVE manual rules. It loads only the selected variations' sales/movement histories through the same read-only evidence rules, preserving unknown/zero/unavailable distinctions and all exceptions. The Phase2 loader retains its original full-snapshot API and adds a bounded selected-variation path; it still uses14 data queries, without per-product history SQL. No history read occurs during routine settings edits.

Both sides use the single Phase2 calculation pipeline. The manual side retains active units or explicit inactivity. The automatic side is a labeled counterfactual with manual policy removed, qualified evidence/windows, current owner supplier/pack context and approved final per-store pack rounding. An automatic category ambiguity remains a block for the counterfactual while it cannot block an otherwise valid active manual unit rule. All conversion checkboxes start false.

History-check evidence is retained separately from policy. The source digest, as-of cutoff, freshness, configuration digest and versions of all relevant manual/product/assignment/offer/profile dependencies are pinned. Before saving a check or applying a selected conversion, changed dependencies cause a conflict. Use Automatic Engine requires a nonempty explicit subset of qualified rows from that owner's check. It appends an auditable manual-state revision. Generic edit/restore flags cannot convert a manual rule. A retry of the same completed conversion returns its immutable receipt.

There is no automatic history scan, policy expiry, default conversion selection or source repair. Source evidence becoming insufficient does not delete an owner rule. No Reorder remains global across stores and permits manual addition under the accepted calculation contract.

## Activity and dormant-catalog investigation

`activity.investigate` is a read-only analysis with an explicit aware cutoff and source age. It reports30/60/90/180 completed LA-date windows and older observed activity. Sales use observed completed gross quantities with the correction overlay and source cutoff; receipts require dated submitted receipt evidence; inventory observation age/value qualification is separate; purchasing uses positive submitted, nonremoved order records observed by the cutoff. These streams are never treated as interchangeable demand.

The categories are descriptive evidence cohorts:

- VERIFIED_RECENT_ACTIVITY: at least one verified positive recorded-unit event in30 days.
- INTERMITTENT_OBSERVED_ACTIVITY: a positive observed event31–180 days ago, without asserting a seasonal pattern or inventing a recent zero.
- NO_MEANINGFUL_RECENT_RECORDED_ACTIVITY: no positive event and complete negative coverage for every stream; this describes recorded evidence only.
- INSUFFICIENT_EVIDENCE: absence of positive observations without the coverage needed to establish inactivity.

No additional volume cutoff or replenishment suppression is invented. No active catalog flag is used as demand evidence. Missing timestamps, future revisions, unqualified counts and undated legacy aggregate receipts remain visible uncertainty. Longer-term “observed only before180 days” is not a claim of verified physical inactivity. The report gives per-source/window coverage and availability separately for each cohort. This investigation makes zero product-status or policy changes.

## Management UI and performance

The standalone app has separate Engine settings, Product policies, Manual Par/Levels, Suppliers, Evidence review and Change history areas. It is distinct from a purchasing worksheet. Reads use bounded keyset pages, stable internal IDs and indexed text search. Manual-store and offer-product lookups have partial/expression indexes; catalog search uses GIN and UUID keyset predicates. Dropdown searches and row names retain stable identities. Reads do not create defaults or refresh Square.

Routine saves use fetch and update the current section, with saved/unsaved/conflict status. Unconfirmed or stale saves retain the entered values; a retry uses the same command identity. Search responses carry a client generation guard, and search focus/caret is retained across results. Narrow layouts present labeled record cards rather than requiring a wide worksheet. Audit lists remain concise; before/after revisions load on demand. Heavy history computation runs on an awaited request thread, outside management write locks, without a background scheduler.

`?qualification=1` enables local console timing of HTTP requests, rendered views and saves, including a document ID and browser user agent. It logs no command bodies, passwords, tokens, costs or external telemetry. Default UI has no timing output. Report timings on declared fixtures/hardware and do not substitute API timings for browser behavior or claim Phase4 interaction targets from these measurements.

## Qualification and recovery

Run `scripts/qualify_purchasing_v4_phase3.py` with explicit source/target loopback URLs, namespace, cutoff, study-profile file/ID, accepted Phase2 evidence directory and output directory. It verifies the accepted bundle manifest, migrates a separate clone, projects catalog observations, imports102 immutable exception-review items and reconciles all199 public tables/772,917 rows plus original foundation facts. It installs no owner controls or numerical defaults. Study profiles are not persisted.

The independent review evidence preserves8 mapping exceptions,2 PO129 allocation conflicts,52 unresolved legacy manual references and40 pack-configuration exceptions. Acknowledging evidence appends notes only; no interface repairs old costs, POs, allocation balances, supplier assignments or inventory. Corrections to future owner controls and repairs to historical evidence remain different authorized actions.

Automated tests exercise real PostgreSQL migration rollback/forward retry, native constraints, owner/CSRF/session checks, independent inheritance removal, manual persistence/inactivity, bulk adoption and interruption, concurrent stale edits, command replay, supplier independence from Square, unknown cost, negative-position abstention, history-check immutability and explicit conversion. Legacy purchasing/accounting/schema/Square regressions run in separate disposable databases. Cold dump/restore and original web startup/cron-import compatibility are qualified separately; cron is never executed.

Rollback means using the accepted Phase2 application source while retaining new V4 schema, identities and audit facts. No database downgrade, financial rewrite or order replay is needed. Forward recovery reruns a failed transactional migration or the same pinned command. Restore settings through new versioned owner commands, preserving all prior revisions. The development app and task-owned cluster are stopped after qualification, with database data and local recovery instructions retained. Production migration, merge, deployment, activation and Phase4 require separate authorization.
