# V2 presentation status contract

## Boundary

`app/v2/statuses.py` provides presentation metadata for future V2 modules and a Jinja helper named `v2_status`. It does not change a database enum, stored value, or V1 screen.

Every status has a readable label, explicit meaning, icon identifier, semantic tone, and category. Text/icon accompany color. Unknown raw values remain visible as `Unknown (<raw value>)`.

## Registry

| Label | Meaning | Icon | Tone | Category |
|---|---|---|---|---|
| Draft | Editable, not submitted | edit | neutral | business |
| Submitted | Finalized locally for review/processing | send | info | business |
| Pending | Technical operation queued/nonterminal | clock | warning | sync |
| Needs Review | Documented human review required | review | warning | business |
| In Progress | Work started, not complete | progress | info | business |
| In Transit | Goods left source, not received | truck | info | business |
| Partially Received | Some expected quantity received | package-open | warning | business |
| Partially Completed | Some command targets completed | split | warning | business |
| Completed | Local workflow terminal success | check | success | business |
| Cancelled | Ended intentionally without completion | cancel | neutral | business |
| Succeeded | Technical operation terminal success | check-circle | success | sync |
| Failed | Technical operation failed | error | danger | sync |
| Submitted to Square | Evidence records a Square submission | external | info | sync |
| Inactive | Configuration retained, unavailable for new work | archive | neutral | business |
| Verified | Authenticated reviewer confirmed a fact | verified | success | business |
| Resolved | Defined issue closed with evidence | resolved | success | business |
| Needs Attention | Derived flag pointing to a source reason | alert | warning | business |
| Unknown | Raw value has no mapping | help | neutral | business |

Raw V1 `SUCCESS` maps to Succeeded and `PUSHED` maps to Submitted to Square. Other raw values map by normalized name when exact. Domain adapters may add distinctions but may not merge Partially Received with Partially Completed or local Completed with external Succeeded.

## Usage rules

- Business and sync status are displayed separately when both exist.
- Status does not authorize an action.
- Entry/exit rules belong to the domain contract.
- `Needs Attention` is derived and never replaces the underlying status/reason.
- `Resolved` and `Verified` require authenticated actor/time evidence.
