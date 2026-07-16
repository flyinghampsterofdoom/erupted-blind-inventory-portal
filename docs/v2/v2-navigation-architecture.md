# V2 navigation architecture

## Central registry

`app/v2/navigation.py` is the single navigation registry and evaluator. Templates render its resolved sections and do not contain role-based visibility rules.

Each section definition records:

- stable key, label, and display order
- optional permission that explicitly grants all children
- ordered child definitions
- optional implemented landing route
- feature exposure for implemented routes
- active-route prefixes

Each child definition records:

- stable key, label, parent, and display order
- required effective permission
- implemented route or `Coming Later` state
- optional feature key
- active-route matching
- optional context requirement

Current Store is deliberately absent from permission evaluation. It may satisfy form context, but it never makes a section or child visible.

Under the [V1 Preservation Guarantee](./v1-preservation-guarantee.md), an authorized V2 navigation child may link to an unchanged V1 route when that tool has not been cut over. Such a link preserves V1 authorization and ownership and must not be labeled as a V2 replacement. The original V1 navigation/access path remains until separately approved retirement.

## Permission evaluation

Navigation uses the effective permission flags already calculated from principal override, role override, and fallback defaults.

```text
child visible =
  (child permission OR explicitly declared all-children permission)
  AND feature exposed when the child is implemented behind a feature
  AND required context satisfied

section visible =
  at least one visible child
  OR an authorized and exposed implemented landing route
```

The empty-section rule wins: a section with no visible child or landing route is omitted. A child permission never grants a sibling. Section-wide permission grants children only through the registry’s explicit `all_children_permission` field. Navigation visibility never replaces route authorization, record ownership, store scope, or action permissions.

## Current hierarchy

```text
Overview
└── Operations Overview

Store Operations → Current Store daily completion dashboard
├── Daily Chore List
├── Inventory Counts
├── Non-Sellable Counts
├── Change Box Count
├── Customer Requests
├── Item Errors
├── Customer Rewards Errors
├── Repair Requests
└── Exchange Forms

Inventory
├── Ordering Tool
├── Par / Level Manager
├── Vendor SKU Mappings
├── PDF Templates
├── Current Orders
├── Order History
└── Order Payments

Reports
├── COGS Report
├── Stock Value
├── Inventory Velocity
├── Targeted SKU Demand
├── Employee Recount Push
├── Sales Transactions
├── Gross Sales by Store
├── Sales by Vendor
├── Sales by Employee
├── Master Safe Change Usage
├── Customer Requests
└── Exchange Forms

Scheduling
├── Schedule Board
├── Shift Templates
├── Employee Availability
├── Time-Off Requests
└── Scheduling Rules

Operation Settings
├── Manage Count Groups
├── Employees
├── Access Controls
└── Daily Chore Editor

Store Needs
├── Repair Requests
├── Store Change / Unsellable Needs
└── Change Boxes
```

The employee Store Operations landing route is the Current Store daily completion dashboard. Daily Store Log remains available from that dashboard and is intentionally not duplicated as a child. Management users continue to land on Daily Store Log history. Exchange Forms has separate Store Operations and Reports definitions, even though both currently reach the same exposed records with persona-appropriate routes.

## Placeholder and interaction behavior

Unimplemented authorized children use one consistent disabled `Coming Later` presentation. They have no link, route, data, or business behavior.

Section headers are real buttons with `aria-expanded` and `aria-controls`. Active sections are expanded server-side. User expansion choices persist in browser storage when available. The same hierarchy operates inside the mobile drawer, which closes after navigation.
