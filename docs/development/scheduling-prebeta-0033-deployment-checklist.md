# Schema 0033 deployment and recovery checklist

October 13, 2026. **Procedure only; production execution requires separate authorization.** The release rehearsal does not authorize deployment, production clearing, feature exposure, permissions/configuration changes, or automation activation.

Use the exact identities in [the final readiness report](scheduling-prebeta-0033-release-readiness.md). Do not substitute the later documentation-only branch HEAD, an untested merge, or a prior 0032 archive.

## Before a release window

- Approve the candidate/recovery identities, rollout owner, maintenance window, and recovery decision-maker. Publish the reviewed source only under the approved release action; retain auto-deploy disabled.
- Confirm both sealed archive hashes and the full file manifests. If deploying from a merge commit, verify its release files exactly match the accepted manifest; otherwise repeat acceptance for the changed artifact.
- Confirm Python 3.10.13, hash-locked binary dependencies, strict schema validation, production environment, and demo seeding disabled. Preserve production provider configuration and credentials through the existing secure configuration mechanism; do not put them into Git, images, reports, or rehearsal environments.
- Keep cron suspended and `SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false` on **both web and cron**. If the platform requires unsuspending cron to build or promote it, stop for specific authorization; do not resume it as a deployment convenience.
- Obtain explicit approval for X=9/Y=8, the single-employee store policy, employee/account mapping, Lead publication access, and the intended feature-exposure scope. None was changed during rehearsal.
- Resolve current/next-week published coverage before opening employee-specific inventory access. The inspected production database has eight unpublished periods and no current/next published coverage. Do not publish old historical drafts to manufacture completed work or fairness credit.
- Take a **fresh** recoverable database checkpoint and secure configuration backup after freezing all production writers. Verify restoreability, access controls, and the operator's recovery procedure. Rehearsal clones were destroyed and are not rollback backups.

## Authorized migration and promotion order

1. Freeze web/application writes, inventory submissions, and background writers. Verify the actual target database, exactly one version row at `20260930_0031`, expected deployment/configuration identities, and the checkpoint. Maintain the freeze through migration and startup acceptance.
2. From the accepted candidate's Python 3.10.13 runtime, migrate once outside build/startup:

   ```sh
   PYTHONPATH=. python -m app.schema_contract upgrade --revision 20261013_0033
   ```

   Alembic must apply 0032 then 0033. Never stamp the revision or disable checks.
3. Require exactly one `20261013_0033` version row. Compare original-column fingerprints and original-record counts, foreign keys, sequences, employee/account/store links, scheduling revisions/assignments, and inventory/audit evidence against the frozen checkpoint. New context versions must initialize to zero; legacy policy X=8 must remain X=8/Y=7 until separately approved configuration changes are applied. Verify snapshot/count-observation protection and `schedule_periods.cleared_at`.
4. Promote the exact candidate to web and cron. Retain `sh scripts/release/build.sh`, the read-only schema pre-deploy check, the existing uvicorn web start command, and `PYTHONPATH=. python scripts/run_schedule_automation.py` for cron. Keep cron suspended and both execution gates false. Confirm actual deployed source identity, Python version, dependency checks, schema startup, authentication, and logs.
5. Under the approved configuration/identity/exposure plan, set X=9/Y=8 and review the configured Double Coverage store selector. The 28 active coverage rules already require one employee. Preserve historical capabilities and evidence. Do not automatically link employees, grant permissions, or widen exposure.
6. Perform controlled manual generation of nine **future** weeks. Recreate/review current-week coverage separately where needed; the rolling horizon excludes the current week. Require authoritative assignment validation and Lead review, approval, and publication of the current and next operating weeks. Verify the simple publication indicator and review queue. No automatic publication is permitted.
7. Verify effective scheduled working stores for the approved employee identities. Confirm retained completed counts and pending drafts, stale-context rejection, and server-side store authorization. Use Emergency Select Store only for expressly authorized, expiring exceptions. Same-day swap and clearing transition tests were performed on disposable data; do not mutate production assignments as a smoke test without specific authorization.
8. Read both Admin clearing previews and verify literal Admin-only authorization. **Do not submit `CLEAR` on production as a release smoke test.** The destructive workflow has already passed representative-data acceptance. Confirm historical evidence remains accessible and clearing controls are absent for other roles.
9. Reopen only workflows which pass acceptance. Leave automation disabled and cron suspended. Any later automation activation requires its own authorization and verification; it permits generation only, never automatic publication.

## Stop conditions

Stop promotion/reopening immediately for any of the following:

- Missing release authorization, writer freeze, recoverable checkpoint, or an unapproved configuration/exposure/permission change.
- Artifact hash/source mismatch, changed release files, failed regression/build/runtime check, incorrect Python version, unsupported schema, multiple/missing revision rows, or an unexpected migration plan.
- Missing clear-marker column, unexpected constraint drift, disabled evidence protection, lost business/history rows, detached foreign keys, orphaned links, changed original assignment/inventory records, or reset metadata changes beyond the documented operation. The recovery guard accepts only the two exact equivalent PostgreSQL status-check cast forms; it does not ignore arbitrary constraint differences.
- An attempted external-provider mutation, production writer credentials reaching a rehearsal runtime, or any rehearsal step requiring production writes. Stop and request specific authorization; do not work around the boundary.
- Missing current/next-week publication, insufficient approved employee/account mappings, absent Lead publication authority, incorrect effective stores, unexpected access expansion, lost draft values, stale submissions being accepted, or cleared schedules influencing future fairness/store authorization.
- A platform requirement to activate/unsuspend cron, disable schema checks, stamp a revision, delete evidence, or perform an unapproved production reset.

Preserve diagnostic evidence and the current database before choosing recovery. Keep writers frozen and automation disabled.

## Recovery and rollback limits

**After a committed 0033 migration, recover with schema-compatible code.** The old 0031 applications and prior 0032 recovery artifacts are not valid recovery targets. Downgrade to 0032 is refused once clear markers exist. Downgrade to 0031 is also refused by retained approval/generation/preference/grant evidence or a nine-week policy. Never delete evidence or alter version labels to force a downgrade.

1. Freeze writes again if necessary, suspend cron, force both execution gates false, and preserve a new checkpoint of the failed/current database. Preserve inventory work and every other write after the pre-release checkpoint.
2. Confirm schema 0033 and the retained object/evidence contract. Under separately authorized recovery action, promote the exact bounded recovery artifact to web/cron. Verify strict startup checks, the recovery banner, rejected Scheduling/HR/account/reset/grant mutations, and inert cron even if its execution flag is accidentally true.
3. Verify authentication, effective-store resolution, retained counts/drafts, clear markers, and selection of **existing** valid emergency grants. Grants still expire. Recovery cannot create/extend grants, change identities, generate schedules, approve/publish, clear schedules, migrate, or stamp. Reopen only unaffected workflows. Missing published coverage remains an operational blocker; recovery does not bypass it.
4. Repair forward using a new disposable clone of the retained post-failure database. Test, seal, and review the repaired schema-compatible artifact; promote its exact identity under authorization. Verify retention and reopen workflows only after acceptance. Leave automation off.

If migration fails, use the old application only after proving complete transactional rollback to the exact 0031 schema and retained-data state. Partial, inconsistent, or unknown migration state requires continued write freeze and reviewed forward repair. Restoring a pre-release checkpoint is a separate disaster-recovery decision and must reconcile post-checkpoint inventory/business writes; it is not a routine application rollback.
