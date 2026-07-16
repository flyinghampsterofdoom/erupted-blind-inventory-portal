# Erupted Operations V2 product architecture and UX blueprint

## Document status and evidence rules

This is the proposed authoritative product and UX architecture for V2. It does not change V1 behavior or authorize implementation. Statements labeled **Confirmed V1** come from repository discovery. Statements labeled **Proposed V2 policy** are recommendations requiring acceptance through this blueprint or a listed decision gate. Inferences are called out explicitly.

Primary evidence:

- [V1 application map — Functional areas](./v1-application-map.md#functional-areas)
- [V1 route inventory — Legend and method](./v1-route-inventory.md#legend-and-method)
- [V1 data map — Database baseline](./v1-data-map.md#database-baseline)
- [V1 permission map — Capability resolution](./v1-permission-map.md#capability-resolution)
- [V1 integration map — Integration points](./v1-integrations.md#integration-points)
- [V1 reports inventory — Report entry points](./v1-reports-inventory.md#report-entry-points)
- [V1 dependency map — Dependency matrix](./v1-module-dependencies.md#dependency-matrix)
- [V1 discovery risk register](./v1-discovery-risk-register.md)
- [V1/V2 feature parity ledger](./v1-v2-feature-parity-ledger.md)

`v1-system-inventory.json` was not present under `docs/v2/` during this milestone; the listed Markdown discovery artifacts are the evidence set.

## 1. Product definition

**Proposed V2 policy:** Erupted Operations V2 is the internal, multi-store operating system for completing store procedures, controlling inventory and purchasing, reconciling store funds, reviewing exceptions, retaining operational history, and administering shared configuration and integrations.

It is not a generic BI dashboard, POS replacement, employee HR system, or public customer portal. Square remains an external commerce/inventory system. V2 coordinates Erupted-specific work around Square and preserves the local facts required to explain what happened.

### Primary user groups

| User group | Current V1 evidence | V2 product need |
|---|---|---|
| Store employees | `STORE` principals complete counts, chores, opening checklist, change/non-sellable counts, requests, and exchange forms; V1 may use shared store principals | Every employee has an individual authenticated account, with a mobile-first assigned-store workspace and attributable tasks, drafts, and submissions |
| Store leads | `LEAD` has management access and limited employee visibility; no assigned-store model | Cross-store or supervisory review according to explicit scope; ability to enter employee logs without gaining configuration rights |
| Management | Legacy `MANAGER` is treated as admin in some places and management in others | Operational review and approved corrective actions across selected stores; configuration separated from daily work |
| Administrators | `ADMIN` and `management.users` administer users/access; `management.admin` covers many configuration and write actions | System configuration, permissions, catalogs, integration health, and high-risk operational controls |
| Owners | No V1 OWNER role exists (**confirmed**); owner intent is inferred from literal ADMIN-only reporting/actions | A product persona, not a new role in this milestone. Until decided, owners use existing ADMIN behavior and no permission is invented |

See [V1 permission map — Roles and defaults](./v1-permission-map.md#roles-and-defaults).

### Primary business outcomes

1. Every store knows what must be completed now and can complete it reliably on a phone.
2. Management can see exceptions across stores without opening every workflow.
3. Inventory discrepancies, purchasing needs, receipts, and Square writes are explainable and recoverable.
4. Cash/change/safe movements reconcile current balances to historical evidence.
5. Customer, employee, and procedural submissions remain searchable, scoped, and auditable.
6. Reports clearly state whether they use stored facts, snapshots, or live/recalculated Square data.
7. Administrators can configure users, access, stores, tasks, products, vendors, and count programs without mixing those actions into daily operations.

## 2. Product principles

These principles constrain future design and implementation.

1. **Overview surfaces attention, not links.** Navigation owns discovery; Overview ranks overdue, failed, incomplete, stale, or otherwise actionable work.
2. **Every capability has one canonical home.** Secondary shortcuts may deep-link to that home but must not create another owner or divergent workflow.
3. **Multi-store behavior is designed at the query and command boundary.** “All Stores” is not added after a single-store screen is built.
4. **Store and company views use the same scope control and vocabulary.** A report, queue, and export must interpret scope the same way unless the workflow explicitly prohibits cross-store writes.
5. **Configuration is separated from operations.** Task templates, count groups, pars, users, vendor mappings, and PDF templates live in Administration even when their effects appear in an operational domain.
6. **Human search terms are first-class.** Product, variation, store, vendor, employee, ticket, SKU, GTIN, and Square IDs are searchable where the underlying data exists.
7. **Statuses are business contracts.** Stored values, display labels, entry criteria, exit criteria, and available actions must be documented; color alone never carries meaning.
8. **Historical facts are not silently recomputed or overwritten.** Live calculations are labeled as live. Corrections append or explicitly supersede; they do not rewrite unexplained history.
9. **Important mutations are auditable.** Actor, time, scope, before/after or command payload, outcome, and external correlation are available for high-impact actions.
10. **External writes are explicit, idempotent, observable, and safely retryable.** Partial success is a supported state, not an exception hidden by a generic error.
11. **Mobile is a complete operational surface.** Store workflows remain readable, tappable, resumable, and safe under intermittent connectivity and session expiry.
12. **V1 capability retirement requires an explicit product decision and evidence.** Absence of a current code reference or navigation link is insufficient.
13. **Current-state ownership is singular.** A balance or queue may be read by many domains, but one domain defines its valid mutations.
14. **Read-only screens do not create business data implicitly.** V1 lazy default creation is characterized first, then moved to explicit initialization/configuration where feasible.
15. **Risk determines sequence.** Append-only local workflows precede shared balances, complex state machines, and Square inventory writes.
16. **Exports are product behavior.** Filters, scope, columns, labels, rounding, filenames, and audit behavior are parity requirements.
17. **Authentication identifies a person.** Every employee uses an individual account. Store assignment is an attribute of that authenticated employee, and every operational event records the acting principal; shared store principals remain a V1 compatibility concern only.

Principles 8, 10, 13, and 14 directly address the confirmed risks in [V1 data map — Orphan, duplication, and migration hazards](./v1-data-map.md#orphan-duplication-and-migration-hazards) and [V1 integration map — Square write safety](./v1-integrations.md#square-write-safety).

## 3. V2 domain model

### Domain ownership table

| Domain | Purpose and primary users | Owns | Reads but does not own | Must not own | Major dependencies, risk, likely order |
|---|---|---|---|---|---|
| Overview | Cross-domain exception/action queue for all roles | Widget definitions, saved view preferences only if approved | Attention projections from every authorized domain | Business records, configuration editors, report computation | Depends on stable domain status contracts. Medium risk if built as projections; foundation then incremental widgets |
| Store Operations | Assigned-store recurring procedures; employees/leads/management | Chore sheets and completion commands; opening checklist submissions; store task presentation | Store identity, configured task/checklist templates, selected exceptions | Template administration, inventory count state, cash balances | Local/append-oriented. Low–medium risk. Early after foundation; chores after append-only forms |
| Inventory | Count programs in execution, discrepancies, recounts, non-sellable stock, full counts; store teams and management | Count sessions/snapshots/entries/recount state; non-sellable stock-take facts | Store identity, configured count groups/campaigns, Square catalog/on-hand, sync outcomes | Vendor/order configuration; cash/change balances | Tightly coupled state machines and Square writes. High risk. Read history early; writes late |
| Purchasing & Ordering | Demand planning, vendor orders, editing, documents, receiving, emergency inventory; management/admin/owners | PO aggregates/lines/allocations, ordering workflow statuses, receiving commands, order documents | Product/vendor mappings, pars/settings, Square sales/catalog/on-hand, sync events | Product master configuration and generic integration transport | High risk. Reference data → read/order edit → generation → receiving → emergency |
| Cash & Store Funds | Cash reconciliation, change box current state, change forms, master safe, par replenishment; store teams and management | Cash actual/verification facts; current change/safe balances and movement commands; replenishment orchestration | Store identity, non-sellable inventory current/latest take, Square cash events | Non-sellable catalog/history; generic audit viewer | Cross-domain delivery and several current-state writers. High risk. Stored history read first; writes after ownership cleanup |
| Customer & Forms | Customer requests and exchange/return submissions; store teams and management | Request/exchange submission facts and request catalog semantics once decided | Stores, principals | Cash change forms, employee discipline notes | Low–medium risk. Exchanges first; customer requests after aggregate-count decision |
| Employees | Employee directory, log taxonomy, entries, visibility; leads/management/admin | Employees, log categories and entries | Principals/roles for authorization; stores only if a future approved association exists | Authentication identities, general audit log | Moderate permission risk, low integration risk. Early after permission foundation |
| Audits | Canonical review queues and audit-history presentations spanning domains; leads/management/admin | Audit workflow definitions and review annotations only if later approved | Immutable submissions, count history, sync events, domain-specific audit facts | Source business facts or their correction commands | Mostly read projections; low risk. Build incrementally with source modules |
| Reports & Analytics | Historical, live, and derived analysis plus exports; management/admin/owners | Report definitions, snapshot/cache metadata if later approved | Domain facts/config and Square data | Operational mutations, except explicit handoff to canonical order creation | Read-only but integration/calculation risk. Build with captured fixtures before transactional handoffs |
| Administration | Users/access, stores, count setup, task/checklist configuration, non-sellable catalog, vendors/products/pars/settings/doc templates, dashboard preferences | Configuration records and their audit trail | Operational history for impact previews | Daily task completion, counts, cash verification, orders in execution | Security and shared-reference risk. Foundation pieces early; ordering config before ordering writes |
| Integrations & System Health | Square connectivity, sync commands/events, failures/retries, schema/deployment health; administrators/owners | Integration command/event contracts, health projections, operator runbooks | Domain commands and external responses | Domain decisions such as what quantity should be written | Highest external-side-effect risk. Read-only health early; unified writes only after stabilization |

### Cross-domain command: store-par delivery

**Confirmed V1:** delivery consumes `store_par_delivery_lines`, updates `change_box_inventory_lines`, and creates a submitted `non_sellable_stock_take` with lines. See [V1 dependency map — Shared-table ownership conflicts](./v1-module-dependencies.md#shared-table-ownership-conflicts).

**Proposed V2 policy:** Cash & Store Funds owns the replenishment orchestration because the operator is balancing store funds/change. Inventory owns the resulting non-sellable stock fact. The orchestration must call explicit domain commands inside one transactional boundary or a recoverable saga; it must not write the Inventory tables directly from a route. The queue cannot be migrated while nonempty without an approved conversion plan.

## 4. Complete V2 sitemap

### Hierarchy

```text
Overview

Store Operations
  My Store
    Today
    Drafts
  Procedures
    Daily Chores
    Opening Checklist

Inventory
  Counts
    Start / Continue Count
    Count History
    Count Detail
    Recounts & Discrepancies
    Full Store Count
  Non-sellable Stock
    Current Stock Take
    History

Purchasing & Ordering
  Planning
    Demand & Stock Coverage
    Generate Orders
  Purchase Orders
    Orders
    Order Detail
    Receiving
    Documents
  Inventory Adjustments
    Emergency On-hand

Cash & Store Funds
  Cash Reconciliation
    Reconcile
    Verification History
  Change Box
    Count
    Current Balance
    Change Forms
  Master Safe
    Current Balance & Audit
    Change Usage
  Store Replenishment
    Par Reset
    Delivery Queue

Customer & Forms
  Customer Requests
    Submit Request
    Request History
    Request Catalog
  Exchanges & Returns
    Submit Form
    Form History

Employees
  Employee Logs
    Add Entry
    History
  Directory

Audits
  Procedure Audits
    Daily Chores
    Opening Checklists
  Inventory Audits
    Count Discrepancies
    Non-sellable Stock
    Count Group Coverage
  Funds Audits
    Change Box
    Master Safe
  Integration Audits
    Inventory Sync History

Reports & Analytics
  Sales & Finance
    COGS
    Sales Transactions
    Gross Sales by Store
    Sales by Vendor
    Employee Sales
  Inventory Analytics
    Stock Value On Hand
    Inventory Velocity
    Targeted SKU Demand
    Stock Coverage
  Operational Reports
    Count Variance Export
    Recount Closeouts
    Master Safe Change Usage

Administration
  People & Access
    Users
    Roles & Permissions
    Employee Directory & Categories
  Stores & Procedures
    Stores & Credentials
    Daily Chore Templates
    Opening Checklist Templates (future editor; V1 has no route)
  Inventory Configuration
    Count Groups & Campaigns
    Store Count Rotation
    Non-sellable Item Catalog
  Purchasing Configuration
    Vendors
    Product / SKU Mappings
    Par Levels
    Ordering Settings
    PDF Templates
  Experience
    Dashboard / Overview Configuration (pending decision)

Integrations & System Health
  Square
    Connection & Store Sync
    Campaign / Vendor / Product Sync
    Inventory Write Queue & Failures
    Sync History
  System
    Application / Schema Version
    Operational Runbooks
```

“Opening Checklist Templates” is labeled future because V1 has configuration data/default creation but no UI editor; this is not presented as an existing capability.

### Capability assignment register

Every capability in the [V1/V2 feature parity ledger](./v1-v2-feature-parity-ledger.md) is assigned once below. “Redirect” means the V1 URL or route group will eventually need a compatibility redirect; timing remains milestone-specific. Permission/scope always filters the canonical page even when a shortcut is visible.

| V1 capability | Canonical V2 location | Disposition | Secondary shortcut | Redirect / visibility rule |
|---|---|---|---|---|
| Login/logout and sliding session | Administration foundation / shared session | Preserve | User menu logout | Keep `/login` and `/logout` compatible; all users |
| Root role/capability redirect | Overview or My Store Today | Replace | None | `/` redirects by effective access and persona |
| Autosave/session expiry UX | Shared form/session pattern | Redesign | None | No page route; all authenticated users |
| Management dashboard cards | Overview | Redesign | Domain navigation | Redirect `/management/home`; scope and permissions filter widgets |
| Dashboard layout editor | Administration → Experience | Preserve | Overview settings | Redirect settings routes; admin-capability hidden |
| Role dashboard category visibility | Administration → People & Access | Consolidate | Experience settings | Redirect access category routes; users-capability hidden |
| Store workflow home/status | Store Operations → My Store → Today | Redesign | Overview attention | Redirect `/store/home`; assigned-store users only |
| Rotating blind count generation | Inventory → Counts → Start / Continue | Preserve | My Store Today | Redirect store daily-count/generate; assigned-store write only |
| Count draft autosave | Inventory → Counts → Count Detail | Preserve | My Store Drafts | Compatibility action while V1 drafts exist; owner store only |
| Count submission/variance | Inventory → Counts → Count Detail | Preserve | Overview exception after submit | Compatibility action; owner store only |
| Recount queue and 3-match closeout | Inventory → Counts → Recounts & Discrepancies | Preserve | Overview attention | Redirect relevant views; store/admin scope and high-risk action permission |
| Management session list/detail | Inventory → Counts → Count History / Detail | Redesign | Audits → Inventory Audits | Redirect management session routes; permission and selected scope |
| Force recount | Inventory → Count Detail → Actions | Preserve | Recount queue | Compatibility action; high-risk permission |
| Unlock count | Inventory → Count Detail → Actions | Investigate | None | Preserve current MA enforcement until owner decision |
| Manual count Square pushes | Inventory → Count Detail → External Sync | Preserve | System Health failure queue | Remain V1-only until write gateway; literal ADMIN behavior preserved initially |
| Session variance CSV | Reports → Operational Reports → Count Variance | Preserve | Count Detail Export | Redirect export only after column/audit parity |
| Full admin store count | Inventory → Counts → Full Store Count | Consolidate | Overview stale draft | V1-only until Square-write milestone; admin hidden |
| Count groups/campaign assignments | Administration → Inventory Configuration → Count Groups | Preserve | Inventory count setup warning | Redirect group routes; `management.groups` hidden |
| Store count rotation override | Administration → Inventory Configuration → Store Rotation | Preserve | Store detail shortcut | Redirect store rotation action; `management.groups` hidden |
| Campaign Square sync | Integrations & System Health → Square → Catalog Sync | Preserve | Count Group settings | Redirect/manual compatibility; group/admin visibility |
| Count group coverage audit | Audits → Inventory Audits → Count Group Coverage | Preserve | Count Group settings | Redirect audit route; group/admin visibility |
| Non-sellable stock take | Inventory → Non-sellable Stock | Preserve | My Store Today; Audits | Redirect store/management routes; assigned-store write, scoped review |
| Change box count | Cash & Store Funds → Change Box → Count | Preserve | My Store Today; Funds Audits | Redirect store/management routes after current-state ownership gate |
| Store change form | Cash & Store Funds → Change Box → Change Forms | Preserve | My Store Today; Customer & Forms may link, not own | Redirect form routes; assigned-store write |
| Change box audit | Audits → Funds Audits → Change Box | Preserve | Cash current balance | Redirect after balance parity; admin hidden |
| Master safe audit/par | Cash & Store Funds → Master Safe → Current Balance & Audit | Preserve | Funds Audits | Redirect after cash domain milestone; admin hidden |
| Master safe change usage report | Reports → Operational Reports → Master Safe Change Usage | Preserve | Master Safe page | Redirect report route; admin hidden |
| Store par reset/queue/delivery | Cash & Store Funds → Store Replenishment | Preserve | Overview queued delivery | Remain V1-only until cash+inventory contract; admin and single-store write only |
| Cash expected calculation | Cash & Store Funds → Cash Reconciliation → Reconcile | Preserve | Overview cash attention | Remain V1-only until integration fixtures; one-store write context |
| Cash actual verification/batches | Cash & Store Funds → Cash Reconciliation | Preserve | Audits/history shortcut | Redirect after stored/live parity; admin and selected store |
| Daily chore store sheet | Store Operations → Procedures → Daily Chores | Preserve | My Store Today | Redirect store chore routes; assigned-store only |
| Daily chore management audit | Audits → Procedure Audits → Daily Chores | Preserve | Chore template impact/history | Redirect management chore routes; scoped review |
| Daily chore task editor | Administration → Stores & Procedures → Chore Templates | Redesign | Chore operational empty/config warning | Redirect task routes; admin hidden |
| Opening checklist store form | Store Operations → Procedures → Opening Checklist | Preserve | My Store Today | Preserve GET-submit compatibility redirect; assigned-store only |
| Opening checklist audit | Audits → Procedure Audits → Opening Checklists | Preserve | Checklist page history | Redirect management routes; scoped review |
| Customer request submission | Customer & Forms → Customer Requests → Submit | Preserve | My Store Today | Redirect store request routes; assigned-store only |
| Customer request administration | Customer & Forms → Customer Requests → Catalog / History | Investigate | Overview unresolved requests | Redirect only after aggregate-count decision; scoped read, configured write permission |
| Exchange/return form | Customer & Forms → Exchanges & Returns | Preserve | My Store Today / Overview | First business slice; redirect store and management routes after parity |
| Employee log entry/history | Employees → Employee Logs | Preserve | Overview follow-up if later supported | Redirect logs route; lead visibility and effective access |
| Employee/category administration | Administration → People & Access → Employee Directory & Categories | Preserve | Employees directory settings | Redirect mutation routes; ELA behavior initially |
| Vendor sync | Integrations & System Health → Square → Vendor Sync | Preserve | Administration Vendors | Redirect action after safe sync framework; admin hidden |
| Vendor SKU mapping edit/import/autofill | Administration → Purchasing Configuration → Product / SKU Mappings | Preserve | Vendor page | Redirect mapping routes; admin hidden |
| Vendor contacts/email state | Administration → Purchasing Configuration → Vendors | Investigate | PO document/send area only after evidence | No active V1 route; hidden pending production data/intent audit |
| Ordering math/vendor settings | Administration → Purchasing Configuration → Ordering Settings | Consolidate | Generate Orders settings summary | Implicit V1 behavior remains until config migration |
| Manual/dynamic par levels | Administration → Purchasing Configuration → Par Levels | Preserve | Purchasing planning | Redirect par routes after mapping/schema gate; admin hidden |
| PO generation | Purchasing & Ordering → Planning → Generate Orders | Preserve | Demand/coverage handoff | Remain V1-only until calculation parity |
| Stock coverage→PO | Purchasing & Ordering → Planning → Generate Orders | Preserve | Report selection handoff | Old report POST remains until transactional parity; literal ADMIN initially |
| PO draft edit/line refresh/add/delete | Purchasing & Ordering → Purchase Orders → Order Detail | Preserve | Overview stale draft | Redirect per-record only when record ownership/version is known |
| PO invoice tracking | Purchasing & Ordering → Order Detail → Invoice | Preserve | None | Redirect with PO detail; admin hidden |
| PO PDF templates/download | Administration → Purchasing Configuration → PDF Templates; download at Order Documents | Replace | Order Detail | V1 PDF route/file remains until storage decision and artifact parity |
| Barcode receiving quantities | Purchasing & Ordering → Purchase Orders → Receiving | Preserve | Order Detail | Remain V1-only until receiving milestone; single PO/store context |
| Receive/send quantities to stores | Purchasing & Ordering → Receiving → Complete / Retry | Preserve | System Health failures | Remain V1-only until idempotent write gateway |
| Receipt tables and unused statuses | Purchasing & Ordering → Receiving | Investigate | None | No redirect; hidden pending production/reference audit |
| Emergency on-hand editor | Purchasing & Ordering → Inventory Adjustments → Emergency On-hand | Preserve | System Health sync outcome | Remain V1-only until final Square-write milestone; admin only |
| Reports hub | Reports & Analytics | Redesign | Domain-level report shortcuts | Redirect `/management/reports`; backend auth still authoritative |
| Sales/COGS reports and CSVs | Reports & Analytics → Sales & Finance | Preserve | Overview only for supported exceptions, not totals | Redirect individually after fixtures/export parity; permission-filtered |
| Inventory value/velocity/demand/coverage | Reports & Analytics → Inventory Analytics | Consolidate | Purchasing Planning for actionable handoff | Redirect individually after calculation/export parity |
| Count/sync reports | Audits → Integration/Inventory Audits | Consolidate | Reports Operational; System Health | Redirect after event-view parity; permission-filtered |
| User administration | Administration → People & Access → Users | Preserve | User menu for self-service only | Redirect users routes; `management.users` hidden |
| Store credentials/self password | Administration → Stores & Procedures → Stores & Credentials; user menu password | Redesign | None | Split redirects only after access decision; current GP behavior preserved initially |
| Role/principal permission overrides | Administration → People & Access → Roles & Permissions | Preserve | User detail | Redirect access routes; `management.users` hidden |
| Audit/auth logs | Integrations & System Health → System → Audit Trail | Preserve | Domain record history panels | No generic V1 route; authorization/retention decision required |
| Audit Queue placeholder | Audits → Overview of Audit Queues | Candidate for Retirement | Overview | Keep V1 route or redirect to Audits until explicit retirement decision |
| Store sync CLI | Integrations & System Health → Square → Store Sync | Preserve | Administration Stores | CLI stays operational until an approved UI/automation replaces it |
| Bootstrap/schema/seed | Integrations & System Health → System → Deployment & Schema Health | Replace | None | Not a user route; replacement requires stabilization milestone |

No capability is marked Retired. The least confident assignments are vendor contacts/email state, unused receipt/status structures, the curated customer request count, and Audit Queue; all remain Investigate or Candidate for Retirement.

## 5. Navigation architecture

The V2 shell should use one responsive information architecture, not separate “store” and “management” applications. The authorization result, active store scope, and viewport determine what is visible. This is a proposed presentation model; the existing backend remains authoritative ([`v1-permission-map.md` §Roles and defaults, §Capability resolution, and §Navigation visibility versus backend enforcement](./v1-permission-map.md)).

### Persona and navigation model

| Persona | Default landing | Typical primary navigation | Scope behavior |
|---|---|---|---|
| Store employee | Store Operations → My Store → Today | Overview/My Store, Store Operations, permitted Inventory tasks, permitted Cash tasks, Customer & Forms | Assigned store, locked; V1 does not currently model a general multi-store assignment for this persona |
| Store lead | Overview | Store Operations, permitted Inventory, Customer & Forms, Employees, Audits | Intersect requested scope with effective access; assignment semantics require a product decision |
| Management | Overview | Operational domains, Audits, Reports & Analytics; configuration only when separately permitted | Single, multi-store, or all accessible stores |
| Administrator | Overview | All domains allowed by effective capabilities, including Administration and System Health | Single, multi-store, or all accessible stores |
| Owner | Overview | Proposed executive views and high-risk administration | **Persona only:** no V1 `OWNER` role exists; map to current effective ADMIN access until a role decision is approved |

### Desktop behavior

- A persistent left rail holds product identity, primary domains, store scope, and the user/session menu.
- Parent domains expand to reveal a small number of task-oriented destinations. Expansion is remembered as a presentation preference, not authorization state.
- Exactly one leaf route is active. Its parent remains visibly active and expanded. Matching uses the route’s canonical identifier, not substring matching.
- Collapsed mode shows icons plus accessible tooltips. Badges are reserved for actionable counts and failures.
- The page header contains title, optional description/breadcrumb, global search, scope summary, and one primary action. Filters stay in the content area.

### Mobile behavior

- The rail becomes a modal drawer. It closes after successful navigation and preserves focus correctly.
- Store scope remains visible in the header; users cannot accidentally change scope while a dirty form is open.
- Deep nesting is limited to two levels. Long operational tables switch to task-specific cards or horizontally scroll only when columns must remain comparable.

### Permission and scope rules

- Navigation is generated from effective capabilities and object/scope access, using the existing precedence of principal override, role override, and fallback. Hiding a link is never authorization ([`v1-permission-map.md` §Capability resolution and §Navigation visibility versus backend enforcement](./v1-permission-map.md)).
- Routes return an explicit forbidden state when a deep link is unauthorized. They do not silently send users to an unrelated page.
- High-risk actions remain separately gated even when their containing page is visible.
- The existing V2 shell’s store selector is presentation-only, as confirmed by the Milestone 2 brief, and must not be described as real data scoping until a business module implements and verifies the contract.

### Evolution from the Milestone 1 placeholders

Overview, Inventory, Store Operations, Audits, and Customer & Forms retain their names. Ordering becomes **Purchasing & Ordering**; Reports becomes **Reports & Analytics**; Admin becomes **Administration** and gives system/integration concerns to **Integrations & System Health**. **Cash & Store Funds** and **Employees** become first-class domains. Placeholder pages remain non-operational until their own approved milestones.

## 6. Overview: action and exception dashboard

Overview is a prioritized work queue, not a miniature report hub. Every tile must answer: what needs attention, where, why, and what authorized action comes next. A tile owns no mutation; it links to the canonical domain workflow.

| Proposed widget | Source of truth | Roles / scope | Actionability | V1 support; schema/service need |
|---|---|---|---|---|
| Today’s procedures | Daily chore and opening-checklist records | Store users: assigned store; management: selected stores | Continue draft, start missing procedure, review submitted record | Confirmed V1 records/routes; aggregation service required, no initial schema change anticipated ([`v1-route-inventory.md` §Store routes and §Chores, checklists, cash/change audits, forms, and employee logs](./v1-route-inventory.md); [`v1-data-map.md` §Store operations and forms](./v1-data-map.md)) |
| Count discrepancies and recounts | Count sessions and recount queue | Store/lead/admin according to current action rules | Continue draft, perform recount, review variance | Confirmed V1; aggregation service required, no initial schema change anticipated; preserve three-match and literal-admin rules ([`v1-module-dependencies.md` §Dependency matrix and §Poor candidates for isolated rebuild](./v1-module-dependencies.md)) |
| Failed external writes | `square_sync_events` and domain command outcome | Admin/high-risk operators; selected scope | Open failure, inspect safe retry eligibility | Confirmed event data; read service required. A normalized command/outcome schema is future work before safe retry ([`v1-integrations.md` §Square write safety](./v1-integrations.md)) |
| Open and partially received orders | Purchase-order header/lines and receiving state | Purchasing users; selected scope/vendor | Open draft, receive, resolve failed allocation | Core V1 records confirmed; query service required. Receipt/status production audit and possibly schema normalization are required before trusted counts ([`v1-data-map.md` §Purchase orders, receiving, and emergency inventory](./v1-data-map.md)) |
| Cash requiring verification | Stored actual/verification data plus live Square expected calculation | Cash-authorized management; selected stores/date | Reconcile one store/day, inspect variance/history | Confirmed V1; fixture-backed aggregate service required, no initial schema change for read-only view; snapshot policy may later require schema work ([`v1-integrations.md` §Square → Integration points](./v1-integrations.md)) |
| Unresolved customer requests | Customer-request records | Store submitters; scoped reviewers | Submit or open request history | V1 submission/admin confirmed. “Unresolved” is unsupported until semantics are decided; service and possibly schema work are required ([`v1-discovery-risk-register.md` main risk table and §Weakest coverage areas](./v1-discovery-risk-register.md)) |
| Stale drafts | Domain drafts and timestamps | Draft owner or authorized reviewer | Resume or inspect; never bulk-delete by default | Drafts/timestamps confirmed in several modules; aggregation service and approved threshold required, no initial schema change anticipated |
| Queued store replenishment | Store-par queue/delivery records | Admin/cash operators; single-store action | Open delivery workflow | Confirmed V1; read service needs no initial schema change, but action waits for a cross-domain ownership/transaction contract ([`v1-module-dependencies.md` §Shared-table ownership conflicts](./v1-module-dependencies.md)) |
| Employee follow-ups | Employee logs | Lead/management under existing access | Open log history | V1 logs confirmed; recent-activity service can use current schema. A true follow-up/resolution queue is future schema/service work ([`v1-route-inventory.md` §Chores, checklists, cash/change audits, forms, and employee logs](./v1-route-inventory.md)) |
| System health | Sync events, mappings, schema/app version checks | Admin/system operators | Inspect integration or deployment issue | V1 partly supports data/CLI; health aggregation service required. Version/command metadata may require later schema work ([`v1-jobs-and-scripts.md` §Server and setup tasks and §Data imports and synchronization scripts](./v1-jobs-and-scripts.md)) |

Dashboard configuration should control layout and eligible categories only. It must not grant data access, widen store scope, or change workflow permissions.

## 7. Shared UX patterns

| Pattern | V2 contract |
|---|---|
| Page header | Breadcrumb when needed, plain-language title, scope/date context, one primary action, then secondary overflow actions |
| Breadcrumbs | Show stable domain → section → record context when users can arrive by search/shortcut; links preserve safe read scope but not dirty form state |
| Store selection | Use the §9 scope contract, show resolved scope in header/filter/export, and require a visible single store for ordinary writes |
| Date selection | Use store-local boundaries, clear inclusive range labels, useful presets, and shareable URL values; disclose timezone in results/exports |
| Status filtering | Use §8 controlled labels, allow multiple values where useful, preserve unknown raw values as “Unknown (value)” for investigation, and show active chips |
| Global and local search | Global search lives in the shell header and navigates across authorized domains; local search lives in the filter bar and narrows the current workflow per §10 |
| Filter bar | Show active filters, reset, shareable URL state, result count, and explicit applied scope; do not apply hidden defaults without labeling them |
| Tables | Server-side sort/filter/page for large sets; sticky identifying column where useful; saved views later |
| Mobile table alternative | Use task-specific cards with the same identifying facts/status/action; horizontal scroll only where column comparison is essential |
| Forms | Persistent labels, inline help, field-level and summary errors, dirty-state warning, disabled state with reason, and idempotent submission |
| Autosave | Show Saving/Saved/Failed plus last successful time. V1’s 30-second form autosave and 15-second session behavior are preserved where present, not generalized without evidence ([`v1-application-map.md` §Cross-cutting observations](./v1-application-map.md)) |
| Drafts | Draft owner, store, last save, and resumability are explicit; stale is a policy threshold, not silent deletion |
| Submission | Revalidate authorization and server state, prevent duplicates, show a durable result/record ID, and distinguish local submission from external sync |
| Status badges | Use the controlled vocabulary in §8; color supplements text and icon, never replaces it |
| Primary and secondary actions | One emphasized next action; secondary actions remain visible only when common and permitted |
| Overflow menus | Hold infrequent non-primary actions, maintain keyboard/touch access, and never hide the only recovery path |
| Confirmation dialogs | Name the object and consequence; require stronger confirmation for irreversible or external writes; never use a dialog for routine navigation |
| Destructive actions | State affected records/downstream effects, re-check authorization and current state, require reason where auditable, and provide undo only when real |
| Loading | Preserve layout with skeletons for initial loads; show localized progress for mutations; no indefinite spinner without recovery guidance |
| Empty | Distinguish no records, no filter matches, no access, and configuration missing; offer an authorized next action |
| Error | Explain what was and was not saved, provide correlation/reference information when available, and expose only safe retries |
| Audit/history | Record timeline uses actor, time, scope, transition, and external outcome; presentation derives from immutable evidence when available |
| Exports | Repeat scope, filters, timezone, generated time, and data-freshness note in the artifact; permissions match the source view |
| External sync | Show Pending/Succeeded/Failed separately from the business state. A successful local save is not presented as a successful Square write |

Offline operation is not a confirmed V1 capability and is outside this blueprint unless separately approved.

## 8. Status vocabulary

Status is domain state, not decorative copy. Raw V1 values remain stored and interpreted by compatibility adapters until a module proves a safe transition.

| Presentation status | Definition and use | V1 mapping / caution |
|---|---|---|
| Draft | Editable work not submitted | `DRAFT`; distinguish from autosave failure |
| Submitted | User finalized a record for review or processing | `SUBMITTED`; do not label Completed merely because entry ended |
| Pending | A queued external or asynchronous operation has not reached a terminal outcome | Domain pending/sync event; not a substitute for “Draft” |
| Needs Review | A human decision or verification is explicitly required | Derived from a documented queue/reason; must link to the record and required review |
| In Progress | Work has begun but is not complete | Contextual; only when the source records progress |
| In Transit | Allocated goods have left a source but are not received at destination | Purchasing/distribution only |
| Partially Received | Some ordered/expected quantity was received; receiving remains open | Proposed normalization; investigate V1 receipt/status semantics |
| Partially Completed | A multi-target command completed for only some targets | Use for distribution/batch outcomes, never as a synonym for Partially Received |
| Completed | The local business workflow reached its defined terminal success | May map from `COMPLETED`; does not imply external sync success |
| Cancelled | Workflow intentionally ended without completion | `CANCELLED`; preserve actor/reason |
| Succeeded | A technical operation completed successfully | `SUCCESS` or successful sync event; technical state only |
| Failed | A technical or business operation reached a recoverable/non-recoverable failure | `FAILED`; display safe retry and partial effects |
| Submitted to Square | External inventory command was accepted according to recorded V1 evidence | `PUSHED`; do not overstate downstream finality |
| Inactive | Configuration is unavailable for new use but retained historically | `active = false`; preferred to hard delete |
| Verified | A reviewer confirmed a cash/history fact | Boolean/history event; not a universal workflow status |
| Resolved | A defined issue was closed with actor, time, and resolution evidence | Use only after the domain defines resolution; do not infer from age or disappearance |
| Needs Attention | Derived presentation flag that links to the underlying state/reason | Never stored as a replacement for the source state |

`NORMAL`/`LOW` are confidence or quantity classifications, not workflow statuses. “Resolved” is used only when the domain has a defined resolution fact; it must not be inferred from age or absence.

## 9. Store-scope model

The scope selector is a shared query contract, not a permission control.

| Scope mode | Intended use | Write rule |
|---|---|---|
| Assigned store (locked) | Individually authenticated store-employee workflows | Writes use the employee account’s server-authorized assigned store; no client override |
| Single store | Detailed management review and most operations | Default for all commands and external writes |
| Multiple stores | Comparison, queues, audits, and reports | Read by default; multi-store commands require an explicit purpose-built workflow |
| All accessible stores | Portfolio-level Overview and analytics | Read by default; “all” means the server-calculated authorized set |

- Effective scope is the intersection of requested stores and server-authorized stores. An empty intersection is forbidden, not an empty success response.
- Authentication identity is always the individual employee. `store_id` is assignment/scope data, never a shared authentication identity. The current one-store `principals.store_id` behavior remains the compatibility baseline; future multi-store assignment requires a separately approved model and migration.
- Canonical URLs use repeatable `store_id` query parameters for explicit selections and `scope=all` only as a request for the authorized set. Exports repeat the resolved store names/IDs.
- Scope changes update the URL and invalidate affected data. Record-detail pages derive their store from the record and show it visibly.
- Remembered scope is a convenience preference only. Whether it persists per device, per user, or per domain is an open decision; it never expands authorization.
- Aggregated views provide a store breakdown before an operator enters a single-store action.
- Timezone/date boundaries follow each record’s store unless a report explicitly declares another basis.

## 10. Search architecture

Global search is a permission-filtered navigator; module search is a workflow filter.

- **Global search:** grouped results for records and destinations (stores, purchase orders, vendors, count sessions, employees, and other approved entities). Each result includes type, store/scope, status, date, and a short differentiator. Unauthorized records are neither returned nor countable.
- **Module search:** purpose-built fields and filters—for example SKU/GTIN/name in Inventory, PO/vendor/invoice in Purchasing, employee/category/date in logs, and store/date/status in Audits.
- **Product/SKU search:** where fields exist, match product name, variation name, SKU, GTIN/barcode, vendor, brand, and Square variation ID. Results show product + variation, identifiers, vendor/brand, active state, and store context; missing V1 fields are not synthesized.
- **Reports:** the reports catalog is globally searchable, but report data is queried through explicit filters rather than mixed into global results.
- **External data:** search uses an approved local/catalog snapshot or bounded server lookup. It must not issue an unbounded live Square request on every keystroke.
- **Performance:** use server-side pagination, normalized search keys, and measured indexes only after query evidence. Debounce typeahead, cap result groups, and disclose stale snapshot time.
- **URLs:** durable filters and result selections are shareable where authorization permits; sensitive query text should not be retained unnecessarily.

Search indexing is optional stabilization work (§14). The first module may use direct indexed database queries and exact/prefix matches.

## 11. Data and history principles

V2 must declare the class and authority of every important datum before presenting or mutating it.

| Data class | Confirmed V1 examples | V2 handling principle |
|---|---|---|
| Historical fact | Submitted counts, checklist/chore submissions, exchange/return submissions, employee logs | Append evidence; corrections create attributed events or superseding versions rather than rewriting history |
| Mutable current state | Change-box inventory lines, master-safe/current cash state, recount queue/rotation state | One declared writer/owner per state; updates use concurrency protection and audit evidence |
| Configuration | Tasks, count groups, permissions, store mappings, par levels | Version when changes alter interpretation of historical facts; prefer inactive over hard delete |
| External snapshot | Square-derived catalog/location/vendor data and stored PO/count line details | Store source, fetched/effective time, identifiers, and enough immutable values to explain a historical result |
| Live external calculation | Cash expected values and some Square-backed report inputs | Label live/stale/error explicitly; do not silently substitute local data |
| Derived projection/cache | Overview counts and report aggregates | Rebuildable from declared sources; never the only audit evidence |
| Audit event | Authentication, permission, workflow transition, external command result | Append-only, correlated to actor/scope/object/command; retention and viewer access require policy |

The discovery found that some historical reports resolve current Square catalog, vendor, or cost information rather than immutable historical values ([`v1-reports-inventory.md` §Validation priorities for V2 parity](./v1-reports-inventory.md)). Before a report is migrated, choose and label one of three semantics: **as recorded then**, **recalculated with current reference data**, or **live external view**. Never mix them without disclosure.

Additional principles:

- Preserve `NULL` versus zero and unknown versus empty; presentation can explain them but must not collapse their meaning.
- Store local timestamps in an unambiguous form and render store-local dates with timezone context.
- Historical records keep stable identifiers even if a configuration record becomes inactive.
- Hard deletion is prohibited for historical/financial/audit records unless retention and legal requirements explicitly require it. Configuration deletion policy remains an open decision.
- Imports, exports, and external writes record provenance and correlation identifiers.
- A V2 cutover may read V1 data, but simultaneous V1/V2 writers to the same current-state aggregate require an explicit locking/ownership plan.

## 12. Permission architecture

This is the desired architecture, not a new role system. V2 must reuse the V1 roles, users, capabilities, and effective-permission precedence documented in [`v1-permission-map.md` §Roles and defaults and §Capability resolution](./v1-permission-map.md).

1. **Authenticate once.** Continue using the existing session and principal. Authentication changes are not part of a business-module migration.
2. **Resolve effective capabilities centrally.** Principal override wins over role override, which wins over the existing fallback. Navigation and page affordances consume the same resolution result as backend policy.
3. **Authorize on the server.** Each request checks capability, object ownership/store membership, action risk, and resolved store scope. UI visibility is explanatory only.
4. **Separate view from action permissions.** A user may see a queue without being able to force recount, push inventory, administer users, or perform another high-risk action.
5. **Preserve literal-role behavior until decided.** V1 endpoints guarded by literal `require_role(Role.ADMIN)` bypass capability overrides. They cannot be silently converted during a UI migration ([`v1-permission-map.md` §Hard-coded checks outside capabilities](./v1-permission-map.md)).
6. **Do not invent Owner.** “Owner” is a persona label in this blueprint; it is not a database role. Current owners must continue through their existing role/effective permissions until governance approves a model.
7. **Attribute actions to people.** V2 submissions, counts, checklists, cash actions, customer requests, exchanges, receiving actions, and future operational events record the authenticated employee principal. A free-text employee name may remain a business field but cannot replace actor attribution.
8. **Deny safely.** Return a clear 403/no-access state, log high-risk denials, and avoid leaking record existence through search counts or error differences.

A required pre-module deliverable is a characterization matrix of V1 page/action behavior by role and override, including GP/MA/ELA differences and all literal-admin routes. Any proposed capability consolidation needs product-owner approval and its own compatibility plan.

## 13. Integration architecture

Square remains the only confirmed network integration in V1 ([`v1-integrations.md` §Integration overview and §Square](./v1-integrations.md)). V2 should treat it as an external system with explicit authority boundaries:

- **Catalog, locations, vendors, orders/payments/refunds/drawers/team:** Square is the external source for the V1 reads that currently use these endpoints. V2 records fetched time and distinguishes cached snapshot from live data.
- **Sellable on-hand:** Square is the intended external authority. Local count/receiving/emergency workflows create commands and evidence; they do not imply success until the recorded external outcome supports it.
- **Local operational state:** drafts, submissions, non-sellable/change inventory, audit history, configuration, and reconciliation verification remain locally owned unless a specific source contract says otherwise.
- **Cash expected:** a derived live-external calculation; actual collection/verification and reconciliation history are local facts.

### Proposed command boundary

All V2 external mutations should eventually pass through one tested Square gateway with:

1. a stable domain command ID and deterministic idempotency key;
2. captured request intent without storing secrets;
3. Pending → Succeeded/Failed technical outcome independent of business status;
4. bounded retry with backoff only for classified safe failures;
5. per-target outcomes for partial batches;
6. correlation to actor, store, record, response metadata, and follow-up action;
7. a System Health view that exposes safe retry or manual resolution.

This is a target state. V1 currently has duplicated `urllib` clients, limited retry behavior, and a mixture of random and deterministic idempotency keys ([`v1-integrations.md` §Square → Integration points and §Square write safety](./v1-integrations.md)). No write-heavy workflow moves until characterization fixtures prove equivalent calculations and effects. At cutover, V1 and V2 must not concurrently issue the same command.

Vendor email/contact intent is not confirmed by active routes. PDF artifacts are stored on local disk under the V1 generated purchase-order area. Email delivery and new storage are not assumed; both require explicit decisions ([`v1-integrations.md` §Filesystem and document generation and §Notification/email status](./v1-integrations.md)).

## 14. Technical stabilization prerequisites

### Required before the first business-module code milestone

| Prerequisite | Why required | Completion evidence |
|---|---|---|
| Versioned schema baseline and production-schema comparison | Startup DDL and repeated schema application make environment drift possible | Approved baseline, schema diff process, rollback/runbook; no unreviewed startup mutation |
| Environment-gated bootstrap and seed behavior | V1 bootstrap reapplies schema and demo seed on every run | Characterization plus explicit non-production gate ([`v1-jobs-and-scripts.md` §Server and setup tasks and §Seed/backfill behavior](./v1-jobs-and-scripts.md)) |
| Auth/permission characterization tests | V2 must preserve GP/MA/ELA and literal-role behavior | Role × override × action matrix with passing compatibility tests |
| Store-scope contract | Multi-store UI is unsafe without server-side scope semantics | Approved URL, authorization intersection, assigned-store, export, and timezone rules |
| Shared status/error/audit contracts | New modules otherwise create conflicting meanings | Approved vocabulary, event envelope, correlation/error presentation, retention owner |
| V1 characterization fixtures for the selected module | Existing behavior is the parity target | Golden routes, validations, data effects, permissions, mobile behavior, and edge cases |
| Cutover and rollback rule | Parallel writers can corrupt current state or duplicate external effects | Per-route ownership, feature exposure, rollback trigger, and reconciliation checklist |
| Secret-safe operational configuration | Discovery and troubleshooting must not expose tokens | Environment/config inventory with redaction and rotation ownership |

The versioned baseline may introduce migration tooling as a separately reviewed stabilization change, but this documentation milestone adds no migration. Removing startup DDL occurs only after the versioned replacement is proven.

### Required before any Square-writing module

- Recorded request/response fixtures and calculation parity.
- Deterministic idempotency and command correlation.
- Retry classification, partial-outcome handling, and operator runbook.
- Sandbox/test-account validation and explicit production-write guard.
- Metrics/alerts for failures, latency, stale data, and reconciliation drift.
- Single-writer cutover ownership for each affected current-state aggregate.

### Optional accelerators

- Consolidated read-only Square client before write gateway work.
- Background worker/job framework where measured workflows need durable async execution.
- Dedicated search index after direct database search fails measured latency/scale needs.
- Cached Overview projections after source queries and freshness budgets are understood.
- Component catalog and visual regression coverage beyond the reusable Milestone 1 shell.
- Central object storage for PDFs after retention/access requirements are approved.

Optional accelerators must not become hidden prerequisites for the low-risk first vertical slice.

## 15. Recommended implementation sequence

Milestone numbers below are planning labels after this blueprint; approval can renumber them. Each milestone ends before the next begins, and V1 remains the operational fallback until its parity and cutover definition is satisfied.

| Milestone | Scope and why now | Prerequisites | Explicit non-goals | Database impact | External integration impact | Parity validation | Product-owner decisions required | Definition of done |
|---|---|---|---|---|---|---|---|---|
| **M3 — Foundation stabilization and contracts** | Establish schema baseline, permission characterization, individual-account actor contract, real store-scope contract, status/audit/error contracts, module flag/cutover pattern. It removes ambiguity before a real workflow. | Blueprint approval; environment access for safe schema comparison | No business page replacement; no role normalization; no production account/data migration; no Square gateway | Baseline/tooling may require a separately reviewed migration setup; no business schema redesign | None; characterize and guard only | Auth/session, role/override, per-person actor attribution, scope, and V1 route characterization tests | Literal ADMIN policy; scope persistence/assignment; audit retention; shared-principal migration governance | Required §14 gates for a local-only module pass; rollback/cutover template approved; V1 behavior unchanged |
| **M4 — Exchanges & Returns vertical slice** | Rebuild store submission plus authorized management list/detail. Best first module because V1 is local, append-oriented, mobile, and does not write Square or shared balances ([`v2-recommended-sequence.md` §Safest first read-only module and §Modules that can be rebuilt relatively independently](./v2-recommended-sequence.md)). | M3; route/data/permission fixtures; final terminology | No customer-request consolidation; no analytics; no retirement of V1 route | Prefer none; propose schema work only if a proven parity blocker is separately approved | None | Field validation, store attribution, submission, list/detail, permissions, mobile, history, V1/V2 record comparison | Edit/correction policy; category/field labels; cutover duration | All golden cases pass, authorized users complete workflow on mobile/desktop, V1 compatibility remains, rollback tested |
| **M5 — Daily Chores** | Exercise drafts/autosave, repeated tasks, Today/Overview attention, and management audit without external writes. | M4 pattern retrospective; chore fixture set; template ownership decision | No opening checklist merge; no generic workflow builder | Likely none initially; template versioning changes require separate design | None | Default-task generation, autosave timing/failure, completion, date/store boundaries, audit view | Template versioning; late/overdue definition; who can edit tasks | One store and multi-store review reach parity; saved-state and stale-draft behavior are observable |
| **M6 — Opening Checklists** | Reuse procedure patterns but keep its distinct checklist/default and GET-submit compatibility semantics. | M5 shared patterns; opening-checklist fixtures | No consolidation that erases checklist semantics; no new template editor unless approved | None preferred; versioned templates only after decision | None | Default creation, drafts/submission, historical audit, legacy GET compatibility, scope | Template editing future; correction policy; completion/overdue rules | Store and audit workflows pass parity; compatibility route documented and tested |
| **M7 — Customer Requests and Employee Logs** | Add two local, bounded workflows after procedure patterns. They exercise catalog/history and lead-sensitive access. Implement as separate slices within the milestone. | M3 permission matrix; request/log fixtures; unresolved-count decision | No CRM, messaging, performance management, or invented resolution workflow | None preferred; any resolution state is excluded unless approved | None | Submit/history/admin catalog for requests; entry/history/category access for logs; role/scope cases | Meaning of request counts/resolution; employee-log visibility/correction; category ownership | Each slice has independent parity/cutover evidence; Overview shows only supported attention states |
| **M8 — Read-only Audits and record history** | Consolidate verified history views without changing writers; exposes cross-domain evidence and gaps. | Audit taxonomy/retention/access decisions; completed source modules or V1 adapters | No generic mutation queue; no deletion; Audit Queue not retired | Usually none; an audit-event schema, if needed, is a separate proposal | Read-only display of recorded sync events; no retries | Chore/checklist/count/funds/auth/sync audit records, timezone, actor, scope, exports | Audit Queue disposition; generic audit viewer scope; retention/redaction | Every exposed event traces to a source; missing evidence is labeled; no unauthorized search leakage |
| **M9 — Reports & Analytics read-only migration** | Move reports individually after semantics and fixtures classify historical/live behavior. | Historical snapshot policy; per-report classification; production-like fixtures | No report rewrite en masse; no totals on Overview without actionability | None for faithful adapters; snapshots/projections require later approved design | Bounded Square reads only for reports explicitly classified live | Formula, date boundary, current-vs-historical reference, CSV columns/order, permissions | Classification of every report; export contract; stale/live policy | Each migrated report has signed fixture parity and a disclosed data-semantic label; unmigrated reports remain V1-only |
| **M10 — Administration and reference configuration** | Migrate users/access, stores/procedures, inventory and purchasing configuration after consuming modules establish real needs. | Permission normalization decision; configuration history/delete policy | No new role system; no implicit hard deletes; no vendor email feature | Possible version/history changes only through approved migrations | Vendor/catalog sync remains V1-only or read-only until gateway | GP/MA/ELA and literal-role behavior, CRUD validation, active/inactive history, imports | Management vs Administration terminology; capability consolidation; owner persona; vendor-contact intent | Configuration screens preserve effective access and downstream behavior; all destructive effects audited |
| **M11 — Cash & Store Funds local workflows** | Rebuild change forms/counts, audits, master safe, then reconciliation UI after financial rules are characterized. | Current-state ownership/locking; cash fixtures; timezone and verification policy; rollback runbook | No store-par delivery initially; no silent recalculation; no broad permission normalization | Possible concurrency/audit support via approved migration | Square reads for expected cash only after fixtures; no inventory writes | Expected formula, actual/verification/batches, balances, reports, null/zero, store-day boundaries | Financial correction policy; expected-data freshness; who can verify; balance authority | Independent cash slices pass penny-level fixtures and concurrent-write tests; V1/V2 single-writer cutover enforced |
| **M12 — Non-sellable stock and store replenishment** | Migrate local non-sellable workflow, then the coupled store-par delivery only after cash/current-state contracts exist. | M11; shared-writer transaction map; inventory/funds ownership decision | No sellable Square inventory mutation; no bulk cross-store command | Likely transactional/concurrency changes require approved migration | None expected | Non-sellable take/history; replenishment queue/delivery effects on both change inventory and non-sellable stock; rollback | Which domain owns orchestration; partial delivery/correction; terminology | Atomic or explicitly recoverable cross-domain effects prove parity; reconciliation report covers both sides |
| **M13 — Purchasing planning and PO lifecycle** | Migrate vendor mappings/pars, generation, draft editing, invoice and PDF/download before receiving writes. | Historical cost policy; configuration M10; PDF storage decision; calculation fixtures | No Square inventory writes; no receiving completion | Potential snapshot/version metadata via approved migration | Square catalog/vendor reads through characterized adapter | Ordering math, coverage handoff, line refresh/add/delete, invoice, PDF byte/content equivalence as required | Cost snapshot semantics; PO status vocabulary; PDF storage/retention; vendor contact scope | Generated orders and documents match signed fixtures; V1 record redirects are record-safe and reversible |
| **M14 — Integration gateway and receiving** | Introduce the write command boundary, then barcode receiving and send-to-store quantities. This is the first planned Square inventory writer. | All Square-writing gates in §14; M13; single-writer cutover; sandbox validation | No emergency on-hand; no count pushes; no generalized event bus | Command/outcome storage may require approved migration | Square inventory writes with deterministic idempotency, bounded retry, partial outcome, health UI | Quantity allocation, repeat submission, timeout/retry, partial target failure, audit correlation | Retry authority; partial-receipt semantics; operator escalation; current-inventory authority | Sandbox and failure-injection suites pass; no duplicate writes; reconciliation and rollback runbooks exercised |
| **M15 — Counts and exceptional inventory writes** | Migrate rotating/recount/full counts, manual pushes, campaign sync, and emergency on-hand last because they combine complex state machines, overrides, and high-risk external writes. | M14 proven gateway; three-match characterization; literal-admin decision; count-group/rotation configuration | No automatic optimization beyond V1; no retirement of legacy actions until sustained reconciliation | Possible concurrency/event/snapshot support via approved migration | Square batch inventory and catalog sync under gateway | Blind counts, autosave, variance, recount closeout, force/unlock, random-vs-deterministic compatibility, full count, emergency write, CSV | Force/unlock policy; owner-only actions; correction/reversal; campaign sync ownership | All role/state/error fixtures pass; duplicate/partial writes are safe; V1 and V2 totals reconcile through an agreed observation window |
| **M16 — Final consolidation and route retirement decisions** | Review duplicate navigation, placeholders, compatibility routes, and operational ownership only after all destinations exist. | Prior module parity and usage evidence | No silent retirement; no forced redirect for unsafe POST semantics | None unless separately approved cleanup occurs later | None beyond monitoring | Route inventory and parity ledger re-audit; saved links; exports; permission deep links | Explicit decision for every consolidation/retirement candidate | Product owner signs each redirect/retirement; rollback window expires; documentation and runbooks are current |

### Early-slice evaluation

| Candidate | Value as early slice | Main complication | Recommendation |
|---|---|---|---|
| Exchanges & Returns | Real mobile submit + management review; local and append-oriented | Correction/category policy | **First business-module milestone** |
| Daily Chores | Exercises Today, autosave, task configuration, audits | Default task creation, date semantics, mutable templates | Second business workflow after shared patterns are proven |
| Opening Checklists | Similar operational value and mobile use | Template/versioning and legacy GET-submit behavior | Follow Daily Chores, remain a distinct workflow |
| Customer Requests | Simple submission/history | Aggregate request count and “resolved” meaning are ambiguous | Early only after the semantic decision |
| Employee Logs | Local and bounded | Lead/management access nuance and sensitive history | Pair at planning level with Customer Requests, validate separately |

Cash Reconciliation is not recommended next: it combines live Square reads, financial correctness, store/date boundaries, verification history, and mutable current-state concerns. Counts, receiving, store-par delivery, and emergency on-hand remain later because they write externally or share balance/state owners.

## 16. Decision register

All entries are open until an accountable product owner records a decision. “Safe default” governs planning only and does not modify V1.

| ID | Question | Why it matters | Affected modules | Safe default until decided | Recommendation | Status |
|---|---|---|---|---|---|---|
| D01 | Should literal ADMIN-only actions remain owner-only, become capability-controlled, or use both? | Current literal checks bypass overrides and can diverge from navigation | Counts, sales/analytics, catalog sync, Administration | Preserve every literal ADMIN check and hide consistently | Define named high-risk action capabilities, retain an explicit admin/owner gate only where governance requires it, and migrate endpoint-by-endpoint | Open |
| D02 | Is “Owner” a persona mapped to ADMIN or a future role? | No V1 `OWNER` role exists | Navigation, permissions, executive reporting | Treat Owner as a persona using existing effective access | Keep persona-only through early milestones; evaluate a role only with an approved responsibility matrix | Open |
| D03 | Are reports analytical, historical, live, or classified individually? | Present-day Square/vendor/cost data can alter historical-range output | Reports, Purchasing, Overview | Keep V1 behavior and label known live/current-reference dependencies | Classify each report individually and display its semantics/freshness | Open |
| D04 | Does dashboard configuration remain user-editable? | Layout/category settings can be confused with data access | Overview, Administration | Preserve current eligible configuration; never let it grant access | Keep per-user layout within an admin-approved widget catalog; permissions and scope always win | Open |
| D05 | What records may be hard-deleted? | Deletion can destroy operational, financial, or audit evidence | All domains, especially config and reports | No hard delete of facts/history; preserve V1 behavior where compatibility demands until reviewed | Use inactive/archived configuration and append corrections; document narrow retention deletions separately | Open |
| D06 | Where and how is store scope remembered? | Stale scope can cause wrong-store interpretation or writes | Shell, Overview, every list/export | URL is authoritative; assigned store locked; writes require visible single store | Remember last read scope per user/domain only after server validation; never auto-apply multi/all to writes | Open |
| D07 | What historical values must be snapshotted? | Recalculation can rewrite the meaning of prior reports and orders | Reports, counts, purchasing, cash | Preserve current results/behavior and disclose current-reference calculations | Snapshot transaction-time product/variation/vendor/cost identifiers and values needed to reproduce material outcomes | Open |
| D08 | Should product language use Management, Administration, or both? | V1 routes mix persona, authorization, and configuration terminology | Navigation, routes, permissions | “Administration” for configuration; persona label “management” | Use Administration as a domain; use manager/management only for people or review context | Open |
| D09 | Should Store Operations and Management share one navigation structure? | Separate trees create duplicate homes and ambiguous ownership | Shell, all operational domains | One V2 tree filtered by access; V1 trees remain | Adopt one shared task/domain navigation with scoped management views | Open |
| D10 | Which V1 capabilities should consolidate without losing semantics? | Reports, counts, audits, and configuration have duplicate entry points | Inventory, Audits, Reports, Admin | Assign one canonical home plus shortcuts; keep old routes | Consolidate count history with inventory audits, inventory analytics with purchasing handoff, and configuration under Administration; validate individually | Open |
| D11 | Which V1 capabilities may eventually retire? | Audit Queue and apparently unused receipt/contact structures lack confident purpose | Audits, Purchasing, Administration | Nothing is retired; keep reachable or hidden as currently appropriate | Candidate review: Audit Queue placeholder, unused receipt/status surfaces, vendor email/contact concept; require production/reference audit and explicit sign-off | Open |
| D12 | Who owns current inventory and shared store-par delivery state? | Store-par mutates cash inventory and non-sellable stock; several workflows write Square | Inventory, Cash, Purchasing, Integrations | Keep V1 writers and sequence; no V2 writer | Square authoritative for sellable on-hand; local domains own facts/commands; a funds-domain orchestrator coordinates store-par with an atomic/recoverable contract | Open |
| D13 | What does customer-request count/resolution mean? | Overview cannot show an “unresolved” queue from ambiguous aggregate data | Customer & Forms, Overview | Show history/submission only; no unresolved widget | Define request instances and resolution evidence, or explicitly retain count-only semantics without an unresolved claim | Open |
| D14 | Where should generated PO PDFs live and how long? | V1 local files affect multi-instance operation, access, backup, and redirects | Purchasing, Administration, System Health | Keep V1 local storage and routes; do not invent email delivery | Approve access-controlled durable object storage and retention before V2 document generation | Open |
| D15 | How should corrections work for submitted local forms? | First slice needs an auditable answer without silently overwriting facts | Exchanges, chores, checklists, employee logs | Submitted records immutable; admin correction remains V1 behavior if any | Append a correction/superseding event with actor, reason, and prior value | Open |
| D16 | How will shared V1 store principals transition to individual employee accounts? | V2 requires person-level attribution but production identities/data cannot be rewritten casually | Authentication, all operational domains, audit, store scope | Preserve V1 principals and one `store_id`; require individual accounts for new V2 exposure; no historical re-attribution | Inventory shared principals, provision individual accounts, preserve immutable historical actor IDs, label legacy shared actors, and cut over per store with rollback | Decided direction; migration plan open |

The five decisions that block the most downstream work are D01 (high-risk permission policy), D03 (per-report semantics), D07 (historical snapshots), D12 (current-state/Square ownership), and D06 (store-scope persistence and write rules).

## 17. V2 route plan (proposal only)

All routes in this section are proposed. This milestone does not add or change any route.

### Canonical hierarchy

```text
/v2/overview
/v2/store-operations/today
/v2/store-operations/chores[/<record-id>]
/v2/store-operations/opening-checklists[/<record-id>]
/v2/inventory/counts[/<session-id>]
/v2/inventory/recounts
/v2/inventory/full-store-counts[/<session-id>]
/v2/inventory/non-sellable[/<record-id>]
/v2/purchasing/planning
/v2/purchasing/purchase-orders[/<po-id>]
/v2/purchasing/purchase-orders/<po-id>/receiving
/v2/purchasing/inventory-adjustments/emergency-on-hand
/v2/funds/cash-reconciliation
/v2/funds/change-box/{count,changes}
/v2/funds/master-safe
/v2/funds/store-replenishment
/v2/customer-forms/customer-requests
/v2/customer-forms/exchanges-returns[/<record-id>]
/v2/employees/logs[/<record-id>]
/v2/audits/{procedures,inventory,funds,integrations,access}
/v2/reports/{sales-finance,inventory,operations}
/v2/admin/{users,roles-permissions,stores,procedures,inventory,purchasing,experience}
/v2/system/{square,audit-trail,deployment-health}
```

Plural resources use stable record identifiers. Actions should prefer resource state transitions (`POST` command endpoints) over verbs embedded in page URLs; dangerous commands remain explicit and idempotent.

### V1 route-group transition map

The authoritative endpoint list remains [`v1-route-inventory.md` §Root and authentication through §Users, access controls, sessions, groups, and system administration](./v1-route-inventory.md). This table maps route groups, not a claim that routes exist.

| Existing V1 group | Proposed canonical V2 group | Transition rule |
|---|---|---|
| `/login`, `/logout`, `/` | Existing auth plus `/v2/overview` or Today | Preserve compatibility; root landing uses effective access |
| `/management/home`, dashboard settings/access | `/v2/overview`, `/v2/admin/experience`, `/v2/admin/roles-permissions` | GET redirect only after widget/access parity; settings split by ownership |
| `/store/home` | `/v2/store-operations/today` | Redirect after Today parity and assigned-store test |
| Store daily count and management count-session routes | `/v2/inventory/counts`, `/recounts`, `/full-store-counts` | Remain V1-only through M14; record redirects only when the same record is readable in V2 |
| Store/management non-sellable routes | `/v2/inventory/non-sellable` | M12 compatibility after writer ownership |
| Store and management change-box/master-safe/cash routes | `/v2/funds/*` | Remain V1-only through M11/M12; `/management/change-box-count` is an audit/history concept while store count is an operation |
| Store chores and management task/audit routes | `/v2/store-operations/chores`, `/v2/audits/procedures`, `/v2/admin/procedures` | M5 redirects split operation, audit, and configuration |
| Opening checklist routes | `/v2/store-operations/opening-checklists`, `/v2/audits/procedures` | Preserve legacy GET-submit compatibility until clients/bookmarks are audited; never redirect a mutation as a blind GET |
| Customer requests and exchange/return routes | `/v2/customer-forms/*` | Exchanges first in M4; customer requests in M7; keep independent compatibility |
| Employee log/category routes | `/v2/employees/logs`, `/v2/admin/users` or people configuration | Split daily records from configuration in M7/M10 |
| Ordering tool, PO, receiving, par, vendor/mapping routes | `/v2/purchasing/*`, `/v2/admin/purchasing` | Planning/PO in M13; receiving V1-only until M14; config redirects after M10/M13 |
| `/management/reports` and individual report routes | `/v2/reports/*` or `/v2/audits/*` | Migrate one report at a time; count/sync evidence canonical under Audits/System Health |
| User/group/access/store credential routes | `/v2/admin/*` | M10 after permission matrix; password self-service stays in user menu |
| Vendor/catalog/store sync and logs | `/v2/system/*` | CLI/old actions stay until a safe equivalent exists; no redirect implying an unbuilt UI |
| `/management/audit-queue` | `/v2/audits` | Compatibility only; no retirement until D11 is decided |

### Compatibility and redirect rules

- V1 GET pages may redirect only after the target has data, permission, export, and deep-link parity. Use a temporary redirect during staged cutover so rollback remains possible.
- Never convert a legacy mutation by redirecting a POST into a different method or repeating an unsafe command. Keep the V1 handler, or make it invoke the same idempotent domain command after that command exists.
- Old record URLs need a deterministic record mapping and must preserve authorization and scope. Unknown/deleted records return the proper state rather than landing on a list.
- V1-only routes stay operational and visible to authorized users until their module definition of done and explicit cutover are complete.
- Compatibility routes emit metrics so owners can evaluate bookmarks/clients before any retirement decision.

### Candidate fetch/command endpoints to formalize later

These names illustrate contracts; they are not implementation commitments:

```text
GET  /v2/api/overview/attention?store_id=...
GET  /v2/api/search?q=...&store_id=...
GET  /v2/api/inventory/products?q=...&store_id=...
GET  /v2/api/reports/catalog
GET  /v2/api/purchasing/purchase-orders?store_id=...&status=...
POST /v2/api/exchanges-returns
POST /v2/api/purchase-orders/<id>/receive-commands
GET  /v2/api/integration-commands/<command-id>
```

Fetch endpoints accept explicit scope, apply authorization server-side, return freshness/timezone metadata, and use stable pagination. Command endpoints require CSRF/session protection, idempotency, validation, and auditable actor/scope. The `/v2/api` prefix must be checked against current router naming before adoption.

Known naming conflicts to resolve include “management” as both persona and namespace, “Ordering” versus Purchasing & Ordering, change-box count as operation versus audit, count/sync reports versus audits, mixed user/group configuration, the Audit Queue placeholder, and the legacy opening-checklist GET mutation. None is resolved by deleting a V1 route in this milestone.

## 18. Blueprint acceptance checklist

- [ ] Product owner confirms the product definition, users, and outcomes.
- [ ] Every V1 capability in the parity ledger appears exactly once in the §4 assignment register.
- [ ] Every capability has one canonical domain and any shortcut is explicitly secondary.
- [ ] Cross-domain readers/writers, especially store-par delivery and Square inventory commands, have named ownership and gates.
- [ ] Navigation/default landing is reviewed for store employee, lead, management, administrator, and owner persona.
- [ ] Navigation visibility is clearly separated from backend authorization.
- [ ] Assigned, single-store, multi-store, and All Stores behavior is approved for reads, writes, exports, totals, URLs, and preferences.
- [ ] Shared UX patterns cover desktop/mobile, filters, tables, forms, autosave, actions, errors, exports, history, and sync state.
- [ ] Status definitions preserve different business meanings and map raw V1 labels without rewriting stored values.
- [ ] Historical facts, snapshots, live calculations, corrections, deletion, and null/zero semantics are approved.
- [ ] Every Overview item names a source, roles/scope, action, and confirmed-versus-future support.
- [ ] Permission mismatches and literal-role checks remain unchanged until an approved normalization plan exists.
- [ ] Square authority, idempotency, retry, partial failure, health visibility, and single-writer cutover are acknowledged.
- [ ] Required stabilization work is separated from optional improvements.
- [ ] Each planned milestone states scope, prerequisites, non-goals, data/integration impact, parity proof, decisions, and done criteria.
- [ ] Every unresolved classification or retirement candidate is in the decision register; nothing is silently retired.
- [ ] The next coding milestone is the narrow M3 stabilization gate, followed by the testable Exchanges & Returns business slice.
- [ ] Review confirms this document and the parity-ledger overlay contain no credentials or secrets.
- [ ] Review confirms this milestone changed documentation only and did not change routes, schema, permissions, integrations, or production behavior.
