# V1 → V2 feature parity ledger

Status values here describe discovery only: `Inventoried` means the V1 implementation has been located, not that V2 implements it. No capability is marked Retired.

| V1 capability | Module | V1 route(s) | Data source | Permission | Disposition | V2 destination | Migration required | Validation required | Status | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| Login/logout and sliding session | Auth | `/login`, `/logout` | principals, web sessions | Public/authenticated | Preserve | V2 foundation | No initially | Cookie, expiry, inactive user, CSRF | Inventoried | Consider token hashing only as separate security change |
| Root role/capability redirect | Auth | `/` | permission flags | Authenticated | Replace | V2 entry router | No | All roles/overrides | Inventoried | V1 redirect must stay until cutover |
| Autosave/session expiry UX | Shell | shared base | Browser + session | Authenticated | Redesign | V2 shell | No | Dirty form, unload, expiry, logout | Inventoried | Current liveness polling runs only on autosave pages |
| Management dashboard cards | Dashboard | `/management/home` | dashboard config/permissions | management.access | Redesign | Overview | Possibly | Visibility/order/fallback | Inventoried | Preserve configurable category semantics unless approved otherwise |
| Dashboard layout editor | Dashboard admin | `/management/dashboard-settings*` | dashboard tables | management.admin | Preserve | Admin / Navigation | Yes if model changes | CRUD/order/deactivation | Inventoried | Read can lazily seed defaults |
| Role dashboard category visibility | Access/dashboard | access role categories routes | role category table | management.users | Consolidate | Admin / Access | Possibly | Nav hidden vs route allowed | Inventoried | Visibility is not authorization |
| Store workflow home/status | Store ops | `/store/home` | multiple workflow tables | store.access | Redesign | Store Operations | No | Status precedence and links | Inventoried | Aggregated cross-module state |
| Rotating blind count generation | Inventory counts | `/store/daily-count`, `/sessions/generate` | groups/rotation/Square catalog | store.access | Preserve | Inventory / Counts | Investigate | rotation, forced count, recount inclusion | Inventoried | High-risk state machine |
| Count draft autosave | Inventory counts | `/store/sessions/{id}/draft` | entries/snapshot | store.access | Preserve | Inventory / Count Entry | No | ownership, zero/blank, concurrency | Inventoried | Autosave-specific 401 behavior |
| Count submission/variance | Inventory counts | `/store/sessions/{id}/submit` | live on-hand/snapshot/entry | store.access | Preserve | Inventory / Count Entry | No | refresh timestamp, lock, variance | Inventoried | Sends audit-only notification stub |
| Recount queue and 3-match closeout | Inventory counts | submission + management reports | recount state/items | store/admin | Preserve | Inventory / Recounts | Possibly | exhaustive transitions and Square failures | Inventoried | Do not simplify before tests |
| Management session list/detail | Inventory counts | `/management/sessions*` | count history | management.access | Redesign | Inventory / Count History | No | filters/detail/audit | Inventoried | Detail GET logs view |
| Force recount | Inventory counts | session force route | forced counts | management.admin | Preserve | Inventory / Count Actions | No | consumption/rotation | Inventoried | Cyclic FK to session |
| Unlock count | Inventory counts | session unlock | session status | management.access | Investigate | Inventory / Count Actions | No | effective-role parity | Inventoried | Broad permission may be intentional |
| Manual count Square pushes | Inventory integration | push and recount-push routes | snapshots/entries/sync events | literal ADMIN | Preserve | Inventory / Sync Actions | No | idempotency/partial failure/access | Inventoried | Permission mismatch decision required |
| Session variance CSV | Inventory export | session export | count history | management.access | Preserve | Reports / Counts | No | columns/rounding/audit side effect | Inventoried | GET commits audit |
| Full admin store count | Inventory counts | `/management/store-count*` | admin count tables/Square | management.admin | Consolidate | Inventory / Full Count | Possibly | draft/export/submit/Square parity | Inventoried | Separate implementation from rotating counts |
| Count groups/campaign assignments | Inventory admin | `/management/groups*` | campaigns/groups/junction | management.groups | Preserve | Admin / Count Configuration | Possibly | ordering/deactivation/mapping | Inventoried | Campaign identity tied to category text |
| Store count rotation override | Inventory admin | store set-next-group | rotation | management.groups | Preserve | Admin / Store Configuration | No | next selection after consume | Inventoried | Shared with generation |
| Campaign Square sync | Inventory admin | group sync + CLI | Square catalog/campaigns | management.groups/operator | Preserve | Admin / Integrations | No | create/update/deactivate | Inventoried | Manual only |
| Count group coverage audit | Audits | group audit | live Square + groups | management.groups | Preserve | Audits / Configuration | No | captured catalog fixture | Inventoried | Result not stored |
| Non-sellable stock take | Inventory/store ops | store and management stock-take routes | items/takes/lines | store/management | Preserve | Inventory / Non-sellable | No | draft, submit, unlock, item lifecycle | Inventoried | Also written by store-par delivery |
| Change box count | Cash/store ops | store/management change-box routes | count/current inventory | store/management | Preserve | Store Operations / Cash | Possibly | current-state sync and delete | Inventoried | Shared current inventory has several writers |
| Store change form | Cash/store ops | store/management change-form routes | form/lines/current inventory | store/management | Preserve | Customer & Forms / Change | Possibly | denomination movement/current balance | Inventoried | Feeds master-safe report |
| Change box audit | Cash audit | `/management/change-box-audit*` | audit/current inventory | management.admin | Preserve | Audits / Cash | Possibly | target/current replacement | Inventoried | Browser computes totals too |
| Master safe audit/par | Cash audit | `/management/master-safe-audit*` | safe current/par/audit | management.admin | Preserve | Audits / Cash | Possibly | global singleton/denominations | Inventoried | No store FK; intentionally global |
| Master safe change usage report | Reports | report route | change forms | management.admin | Preserve | Reports / Cash | No | section-code calculation | Inventoried | Current HTML only |
| Store par reset/queue/delivery | Cash + non-sellable | `/management/store-par-reset*` | change/non-sellable/par/queue | management.admin | Preserve | Store Operations / Replenishment | Likely | cross-domain transaction/failure | Inventoried | Defer until both domains rebuilt |
| Cash expected calculation | Cash reconciliation | expected fetch | live Square | management.admin | Preserve | Store Operations / Cash Reconciliation | No | timezone/payments/refunds/drawers | Inventoried | Live/non-repeatable |
| Cash actual verification/batches | Cash reconciliation | actual/batches/save/detail | cash tables | management.admin | Preserve | Store Operations / Cash Reconciliation | No | upsert + append history | Inventoried | Expected may be snapshotted per verification |
| Daily chore store sheet | Store ops | store chore routes | chore tables | store.access | Preserve | Store Operations / Chores | No | autosave/restart/delete/submit | Inventoried | Default tasks seeded lazily |
| Daily chore management audit | Audits | chore list/detail/delete | chore tables | management/admin | Preserve | Audits / Chores | No | filters/detail/delete scope | Inventoried | Delete is admin-only |
| Daily chore task editor | Admin | chore task routes | tasks | management.admin | Redesign | Admin / Store Tasks | Possibly | cross-store propagation/order | Inventoried | Per-store rows represent global template concept |
| Opening checklist store form | Store ops | store checklist routes | item/submission/answers | store.access | Preserve | Store Operations / Opening | Possibly | defaults, hierarchy, validation | Inventoried | GET submit compatibility redirect |
| Opening checklist audit | Audits | management checklist routes | checklist history | management.access | Preserve | Audits / Opening | No | filters/details | Inventoried | No item editor in V1 |
| Customer request submission | Forms | store request routes | request item/submission/line | store.access | Preserve | Customer & Forms / Requests | Possibly | normalization/quantities/duplicates | Inventoried | Submission can create catalog items |
| Customer request administration | Forms/admin | management request routes | item/history | management.access | Investigate | Customer & Forms / Requests | Possibly | meaning of aggregate count | Inventoried | Write permission broad and count semantics ambiguous |
| Exchange/return form | Forms | store/management exchange routes | exchange forms | store/management | Preserve | Customer & Forms / Exchanges | No | field validation/date/filter/detail | Implemented locally in V2 | Feature-gated local slice; V1 remains active, no redirect/cutover/retirement |
| Employee log entry/history | People/audits | employee-log page/entry | employee/category/entry | management access wrapper | Preserve | Audits / Employee Logs | No | lead visibility/category snapshot | Inventoried | Category label denormalized into entry |
| Employee/category administration | Admin | employee-log admin routes | employees/categories | admin-role wrapper | Preserve | Admin / Employee Logs | No | role+capability matrix | Inventoried | LEAD override cannot elevate |
| Vendor sync | Ordering admin | vendor sync | Square/vendors | management.admin | Preserve | Admin / Vendors | No | deactivate/rename/last-sync | Inventoried | Manual only |
| Vendor SKU mapping edit/import/autofill | Ordering admin | mapping routes | configs/Square/CSV | management.admin | Preserve | Admin / Product Mappings | Likely | current distinct values/default vendor/GTIN | Inventoried | Critical shared reference data |
| Vendor contacts/email state | Ordering | no active route | vendor contacts/PO email columns | none found | Investigate | Admin / Vendors | Unknown | production row/usage audit | Inventoried | Do not retire based on code alone |
| Ordering math/vendor settings | Ordering | implicit in generation | singleton/vendor settings | management.admin | Consolidate | Admin / Ordering Settings | Possibly | defaults and override precedence | Inventoried | Singleton lazily created |
| Manual/dynamic par levels | Ordering | par routes | pars/Square history | management.admin | Preserve | Ordering / Par Levels | Likely | global/store uniqueness, confidence | Inventoried | Null/zero/manual-lock semantics |
| PO generation | Ordering | generate/full-stock | live Square + local refs | management.admin | Preserve | Ordering / Generate | No initially | recommendation snapshot parity | Inventoried | Math covered by tests |
| Stock coverage→PO | Reports/ordering | create-order | transient report→PO tables | literal ADMIN | Preserve | Ordering / Generate | No initially | selected rows/store splits | Inventoried | Crosses read/write boundary |
| PO draft edit/line refresh/add/delete | Ordering | order detail mutations | PO/lines/allocations/Square | management.admin | Preserve | Ordering / Order Detail | No | status/removed/zero behavior | Inventoried | Partial unit tests exist |
| PO invoice tracking | Ordering | invoice route | PO text/date/amount fields | management.admin | Preserve | Ordering / Invoice | Investigate | status text and amount difference rules | Inventoried | `invoice_payment_status` is text+check constraint |
| PO PDF templates/download | Ordering | PDF template and download routes | templates/PO/filesystem | management.admin | Replace | Ordering / Documents | Yes if storage changes | byte/layout/generation/staleness | Inventoried | Local filesystem risk |
| Barcode receiving quantities | Receiving | received/scan/cancel routes | PO lines/allocations/mappings | management.admin | Preserve | Ordering / Receiving | No initially | GTIN/pack/overage/zero | Inventoried | JSON-like UI endpoints |
| Receive/send quantities to stores | Receiving | receive/retry routes | allocations/sync events/Square | management.admin | Preserve | Ordering / Receiving | No initially | deterministic idempotency/partial failure | Inventoried | High-risk Square write |
| Receipt tables and unused statuses | Receiving | no clear route use | receipt tables/status enum | none found | Investigate | Ordering / Receiving | Unknown | production references and intent | Inventoried | Candidate for Retirement only after evidence |
| Emergency on-hand editor | Ordering integration | emergency routes | draft JSON/mapping/Square/sync | management.admin | Preserve | Ordering / Emergency Inventory | No initially | partial failure/retry/idempotency | Inventoried | Defer late |
| Reports hub | Reports | `/management/reports` | static/role | management.access | Redesign | Reports | No | visibility/backend parity | Inventoried | Current link permission inconsistencies |
| Sales/COGS reports and CSVs | Reports | report/export routes | live Square/local mappings | mixed | Preserve | Reports / Sales & Finance | No initially | dates/timezones/columns/calculations | Inventoried | Capture Square fixtures |
| Inventory value/velocity/demand/coverage | Reports | report/export routes | live Square/local refs | mixed | Consolidate | Reports / Inventory | No initially | algorithms, filters, exports | Inventoried | Shared computation should be unified only after parity |
| Count/sync reports | Reports/audits | count-sync/recount/session export | count/sync history | mixed | Consolidate | Audits / Inventory Sync | No | event types/status/detail | Inventoried | Duplicate hub entry points |
| User administration | Admin | `/management/users*` | principals | management.users | Preserve | Admin / Users | No | create/role/status/password | Inventoried | No hard delete |
| Store credentials/self password | Admin | group store/password routes | principals/stores | management.groups | Redesign | Admin / Users & Stores | No | access separation/password behavior | Inventoried | Broadly coupled to group screen |
| Role/principal permission overrides | Admin | access control routes | permission tables | management.users | Preserve | Admin / Access | No | precedence and all role matrices | Inventoried | Do not redesign within module migration |
| Audit/auth logs | System | implicit | audit/auth tables | caller | Preserve | Admin / Audit Trail | Possibly | action coverage/metadata/retention | Inventoried | No generic viewer/retention job |
| Audit Queue placeholder | Audits | `/management/audit-queue` | none | management.access | Candidate for Retirement | Audits | No | usage telemetry/bookmarks | Inventoried | Active route but no function |
| Store sync CLI | System admin | CLI only | Square→stores | operator | Preserve | Admin / Integrations | No | create/update/deactivate | Inventoried | No schedule/UI |
| Bootstrap/schema/seed | System admin | shell/startup | SQL/models/example data | operator | Replace | Deployment tooling | Yes, later | fresh DB and production baseline | Inventoried | No migration history; demo seed risk |

## Proposed V2 domain, destination, and sequencing overlay

This overlay is planning-only and derives from [`v2-product-architecture-and-ux-blueprint.md` §3–§17](./v2-product-architecture-and-ux-blueprint.md). It does not change any V1 fact, disposition, validation requirement, or discovery status above, and it does not mean a capability is implemented. A capability’s exact canonical destination and shortcut/redirect policy remain in blueprint §4.

| V1 capability group | Proposed V2 domain | Proposed canonical destination | Proposed sequence |
|---|---|---|---|
| Authentication, session, root landing | Shared foundation / Overview | Existing auth; `/v2/overview` or My Store Today | M3 foundation; retain compatibility through cutover |
| Individual employee authentication and actor attribution | Shared foundation / Administration | One account per employee; store assignment is user data; authenticated principal on every V2 operational event | M3 contract; non-destructive V1 shared-principal transition in later approved rollout |
| Autosave/session UX | Shared foundation | Shared forms/session behavior | M3 contract, exercised M4–M6 |
| Management dashboard cards | Overview | `/v2/overview` attention queues | Incrementally after each source module |
| Dashboard layout and category visibility | Administration | Experience; People & Access | M10 after access decision |
| Store workflow home/status | Store Operations | My Store → Today | M5–M7 incrementally |
| Rotating count generation, drafts, submit/variance, recount closeout | Inventory | Counts; Recounts & Discrepancies | M15 |
| Management count list/detail, force/unlock, manual pushes, full count | Inventory | Count History/Detail/Actions; Full Store Count | M15 |
| Session variance export | Reports & Analytics | Operational Reports → Count Variance, with Count Detail shortcut | M15 with export parity |
| Count groups, rotation, campaign sync | Administration / Integrations & System Health | Inventory Configuration; Square Catalog Sync | M10 configuration, M15 external action |
| Count group coverage audit | Audits | Inventory Audits → Count Group Coverage | M8 read-only, refreshed with M15 |
| Non-sellable stock take | Inventory | Non-sellable Stock | M12 |
| Change-box count/forms, master-safe audit/par | Cash & Store Funds | Change Box; Master Safe | M11 |
| Change-box and funds audits | Audits | Funds Audits | M8 adapter or M11 canonical data view |
| Master-safe change usage report | Reports & Analytics | Operational Reports → Master Safe Change Usage | M9/M11 after semantics parity |
| Store-par reset/queue/delivery | Cash & Store Funds | Store Replenishment | M12 after cash/non-sellable ownership contract |
| Cash expected, actual verification, batches | Cash & Store Funds | Cash Reconciliation | M11 |
| Daily chore sheet, management audit, task editor | Store Operations / Audits / Administration | Procedures → Daily Chores; Procedure Audits; Chore Templates | M5; configuration follow-up M10 if needed |
| Opening checklist form and audit | Store Operations / Audits | Procedures → Opening Checklist; Procedure Audits | M6 |
| Customer request submission/administration | Customer & Forms | Customer Requests → Submit/Catalog/History | M7 after count/resolution decision |
| Exchange/return form | Customer & Forms | Exchanges & Returns | **M4 implemented locally; feature-gated, not cut over** |
| Employee log entry/history | Employees | Employee Logs | M7 |
| Employee/category administration | Administration | People & Access → Employee Directory & Categories | M10 |
| Vendor sync | Integrations & System Health | Square → Vendor Sync | M10 read/config; safe action after integration gate |
| Vendor mapping/import/autofill, contacts, ordering settings, pars | Administration | Purchasing Configuration | M10–M13; contacts remain Investigate |
| PO generation and coverage handoff | Purchasing & Ordering | Planning → Generate Orders | M13 |
| PO detail/invoice/PDF | Purchasing & Ordering | Purchase Orders → Detail/Invoice/Documents | M13 |
| Barcode receiving and receive/send to stores | Purchasing & Ordering | Purchase Orders → Receiving | M14 |
| Receipt tables/unused statuses | Purchasing & Ordering | Receiving, hidden pending investigation | Decision before M14; no retirement implied |
| Emergency on-hand | Purchasing & Ordering | Inventory Adjustments → Emergency On-hand | M15 |
| Reports hub | Reports & Analytics | Reports catalog | M9 |
| Sales/COGS reports and exports | Reports & Analytics | Sales & Finance | M9 individually |
| Inventory value/velocity/demand/coverage | Reports & Analytics | Inventory Analytics, with Purchasing handoff | M9 individually; M13 actionable handoff |
| Count/sync reports | Audits / Integrations & System Health | Inventory/Integration Audits and System Health | M8 read-only; M15 final parity |
| Users, credentials/password, permission overrides | Administration | People & Access; Stores & Procedures; user menu | M10 |
| Audit/auth logs | Integrations & System Health | System → Audit Trail | M8/M10 after retention/access decision |
| Audit Queue placeholder | Audits | Audit Queues overview, compatibility only | Decision in M8; candidate status retained |
| Store sync CLI | Integrations & System Health | Square → Store Sync | Keep CLI; UI/automation only after explicit decision |
| Bootstrap/schema/seed | Integrations & System Health / deployment tooling | System → Deployment & Schema Health | M3 stabilization |
