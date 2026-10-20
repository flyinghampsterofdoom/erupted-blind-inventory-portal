# Scheduling combined release candidate

Review only. Do not deploy, migrate production, change cron, grant permissions,
or activate password recovery delivery until the exact combined release is authorized.

## Scheduling boundary

Retain the existing allocator, authorization, database and immutable evidence.
Owner comparison defaults to changed rows. The Assignments selector can show all
rows without losing approval/publication revision choices. Missing legacy captures
remain unknown, and the history timeline remains complete.

Weekly maintenance reuses the existing scheduling-only cron entrypoint and run
ledger. Under the existing advisory lock, a complete configured future horizon is
a no-op. Exactly one missing tail week is generated and independently validated
through the approval hard-rule gate before the savepoint can succeed. Any other
missing-week pattern fails for explicit manual recovery. Failed generation rolls
back its attempted week and retains sanitized FAILED evidence. Existing weeks,
approvals, employee settings and published assignments are never rewritten.
Manual initial generation and explicit catch-up retain their existing workflow.
No automatic approval or publication. No routine cross-week optimization.

Authorized Leads see Monday pending/overdue status in the existing review queue;
they do not gain Admin configuration or original-evidence comparison permission.
Recent maintenance outcomes and missing-week readiness remain visible there.

The employee scheduling redesign, draft freshness warnings, stable repairs,
approval integrity, immutable lifecycle captures and audit history are part of
the scheduling candidate. No employee settings or identity/role changes are bundled.

## Staged provider configuration

After exact-commit authorization, reuse Render cron
`crn-da6pd2qd0e5s73dbudr0`, command
`PYTHONPATH=. python scripts/run_schedule_automation.py`, same plan and service.
Change the cron schedule from hourly to `0 10 * * 0` (Sunday 10:00 UTC;
02:00 PST / 03:00 PDT). Keep database policy at nine future weeks and Pacific
business dates. No new paid service or timer. The expression is staged only.

Pause cron execution during migration and web/cron alignment. Take a fresh
backup and verify deployed identities, schema, horizon, current run state,
notification recipients and execution flags immediately before cutover.
Use a maintenance window to pause scheduling and employee writes. Apply reviewed additive migrations 41 → 45. Deploy the same verified SHA to web
and cron, verify build identities/schema without triggering generation, then
change the schedule and restore the existing execution gate. Verify the next
scheduled result and Lead queue. Do not invoke a production generation test as
a side effect of deployment. Unexpected gaps require owner-reviewed manual
recovery. A missed weekly run has no automatic same-week retry; management must
review the existing failure/run evidence and missing-week indicator.

Do not downgrade away captured lifecycle evidence. Rollback must use code that
supports schema 45 and preserves snapshots; the old schema-41 applications are
not a compatible rollback target after migration. The previously tested schema-45 commit
`c6695b5b5299b79e392fb8a9df915457ccf5e030` is the code rollback boundary; keep
automation execution disabled on web and cron during rollback and until the
weekly behavior is reviewed again. Retain database evidence and the weekly
provider configuration. Do not run downgrade or reverse user password resets.

## Password recovery boundary (separate authorization)

The same source candidate contains the earlier password recovery commit
`d87abcca60e76aba8d0139b89802dfe43afb1430` and additive migration 44.
It provides email reset requests, 30-minute single-use hashed tokens, throttling,
session invalidation on actual reset, and preserved roles/employee associations.
Standalone management recovery requires an explicitly confirmed email login;
legacy aliases and unconfirmed accounts are not enrolled or renamed.

It uses the existing Resend integration and encrypted configuration, sender
verification and correct HTTPS portal URL. There is no independent new reset
feature flag. Deploying this full candidate exposes reset routes and can send
reset emails when users request them through the already-enabled provider.
Production email activation is therefore a separate explicit release decision,
not implied by approval of the scheduling UI. Do not disable shared transactional
email to isolate resets. If password recovery is excluded, prepare and retest a
separate scheduling-only source candidate before deployment; this full SHA must
not be described as scheduling-only. No real reset messages or account changes
are part of readiness testing.
