# Staff Scheduling boundary

Staff Scheduling V2 now has a default-disabled backend foundation. It remains outside Daily Store Logs and does not change that module's ownership or attribution. See [V2 Staff Scheduling foundation](./staff-scheduling-v2-foundation.md).

The scheduling foundation supports authorized employee/store shifts and append-only audit history. The management weekly card interface and movement UX are implemented behind the same default-disabled feature; month views, self-service, and administrative configuration pages remain future milestones.

Daily Store Logs do not contain shift types, schedule IDs, schedule-match fields, warning fields, timed assignments, or schedule tables. If scheduling is added later, a missing or mismatched assignment may warn but must not silently rewrite historical Daily Store Log attribution. Any integration requires its own product plan, schema review, permissions, migration, tests, and cutover approval.

Until scheduling exists, employees explicitly select a Current Store for their authenticated session. Future scheduling may suggest or preselect that context. A schedule mismatch should warn rather than block cross-store work, and the employee-confirmed Current Store at submission remains permanently recorded on the resulting log. Scheduling must not become retroactive authorization or rewrite earlier Current Store choices.
