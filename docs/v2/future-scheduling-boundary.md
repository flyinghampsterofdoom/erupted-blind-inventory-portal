# Future scheduling boundary

Scheduling is outside Daily Store Logs and Milestone 5.

A future scheduling module may support authorized creation of clocked employee/store intervals such as 8:00 AM–12:00 PM, assignment to an individual employee and store, and movement or reassignment through a card-based interface with append-only audit history.

Daily Store Logs do not contain shift types, schedule IDs, schedule-match fields, warning fields, timed assignments, or schedule tables. If scheduling is added later, a missing or mismatched assignment may warn but must not silently rewrite historical Daily Store Log attribution. Any integration requires its own product plan, schema review, permissions, migration, tests, and cutover approval.

Until scheduling exists, employees explicitly select a Current Store for their authenticated session. Future scheduling may suggest or preselect that context. A schedule mismatch should warn rather than block cross-store work, and the employee-confirmed Current Store at submission remains permanently recorded on the resulting log. Scheduling must not become retroactive authorization or rewrite earlier Current Store choices.
