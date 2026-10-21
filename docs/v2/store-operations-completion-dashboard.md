# Store Operations daily completion dashboard

## Route structure

- `/v2/store-operations` is the employee Current Store dashboard.
- `/v2/store-operations/daily-logs` remains the Daily Store Log form and receipt route.
- `/v2/store-operations/daily-logs/history` remains the management history route.

The Store Operations navigation header points employees to the dashboard and management users to Daily Store Log history. Daily Store Log is available from a dashboard action and is not duplicated as a sidebar child.

## Employee presentation

The dashboard uses the validated Current Store session context and the current server-derived Pacific store date. Missing, unchecked, unknown, or inactive Current Store context redirects to `/v2/current-store`.

It displays five daily requirements:

- `daily_chore_list`
- `inventory_count`
- `non_sellable_stock_take`
- `change_box_count_am`
- `change_box_count_pm`

AM and PM Change Box Counts are separate completion facts on the dashboard but share one Change Box Count navigation module.

## Completion contract

`app/services/v2_store_operations_completion_service.py` defines the reusable completion boundary. A completion source receives:

- database session
- store ID
- Pacific business date

It returns `complete`, `incomplete`, or `unavailable`, plus an optional authorized route and action label.

The dashboard does not inspect unrelated tables directly. Each future module must register an authoritative source backed by that module’s submitted/completed records. Current Store, navigation visibility, and client input cannot declare completion.

Unimplemented activities currently resolve to `unavailable` and render as `Coming Later`. No placeholder rows or fabricated red/green statuses are created.

## Permission and management boundaries

Each activity uses its matching Store Operations child permission. The explicit Store Operations all-children permission may reveal all activity positions. The dashboard remains useful with any authorized subset.

Current Store does not change `principal.store_id`, permissions, management scope, prior records, or scheduling data. Management completion reporting across stores remains out of scope and separate from this employee dashboard.
