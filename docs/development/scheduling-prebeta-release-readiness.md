# Scheduling V2 pre-beta: schema 0032 release readiness

> **Superseded procedure:** This 0032 report is historical evidence only. The completed [final schema-0033 release rehearsal](scheduling-prebeta-0033-release-readiness.md) and [deployment/recovery checklist](scheduling-prebeta-0033-deployment-checklist.md) supersede its artifact identities and deployment sequence. Do not use the 0032 procedure for the current candidate.


October 13, 2026. **Migration rehearsal passed. Production release remains on hold
for the prerequisites below.** No production schema/data, deployment, feature
exposure, account permissions, scheduling configuration or automation was changed.
No provider mutations or Git pushes occurred.

## Results

A consistent read-only production snapshot at `20260930_0031` was streamed directly
into a disposable, restricted local PostgreSQL 18 database. No raw dump file was
written. Database connections to production forced read-only transactions; Render
requests used GET only. The clone denied public connections and used a private
loopback PostgreSQL instance with temporary credentials. Production credentials
were not copied into a worktree or application test environment.

The source server is PostgreSQL 18.3; rehearsal used 18.6. The workstation's older
default server is unsuitable for this restore. The disposable database, private
cluster data/WAL and temporary credentials were removed after acceptance. Source
archives and aggregate/schema-only evidence remain.

Migration **0031 → 0032** preserved all original rows across **172 business tables**,
using fingerprints of the original columns and records. Existing foreign-key
definitions and sequence values were preserved. Selected retained records:

| Record | Count |
| --- | ---: |
| Employees | 115 |
| Principals/accounts | 15 |
| Stores | 9 |
| Schedule periods | 8 |
| Schedule assignments | 143 |
| Inventory count sessions | 903 |
| Employee/account links | 1 |

Employee, store, period and inventory references had zero orphaned links after
rehearsal. All other inventory and historical tables were included in the full
preservation check, not only the inventory-session total.

The migration preserved X=8 and initialized Y=7. Only on the clone, the intended
9/8 policy was configured. Generation created **nine complete future weeks**,
October 18 through December 13, left every new week unpublished and created no
additional weeks on repeat generation. Existing records remained intact. All nine
weeks passed authoritative assignment validation. Generated weeks had no serious
warnings, double-coverage shifts or store-days with multiple employees.

A Lead approved and published the next week on the clone; the dashboard became
Yellow because the other generated weeks still awaited publication. Recovery and
forward restart then retained a new approval, two generation outcomes, an approved
preference request, an emergency access grant and nine immutable generator
snapshots. All **14 protected tables**, including existing and new records, were
unchanged across recovery and restart.

The web artifact started, authenticated synthetic test accounts, read an existing
schedule and retained store-scoped inventory reads/draft saves. The recovery
artifact passed the same checks, rejected every registered Scheduling/HR/account
mutation front door with 503, blocked direct ORM/bulk changes and permanently
blocked automation, migration and stamping. Missing 0032 columns, disabled
snapshot protection and disabled schema validation were rejected. Recovery did
not replace working-store authorization with the old account-store assumption.
A subsequent candidate restart passed against the retained schema and evidence.

**88 release-tool and execution-gate tests passed.** Both exact artifact variants
passed the repository build command and dependency checks in a fresh, hash-locked
Python 3.10.13 environment on macOS arm64. Linux x86-64 wheels were downloaded and
verified against the required hashes. Linux runtime execution was not performed:
the local Docker daemon is unavailable. The previously approved 1,296-test local
acceptance remains the application-development acceptance record.

Evidence: [sanitized rehearsal results](evidence/scheduling-prebeta-release-rehearsal.json)
and [release/gate test output](evidence/scheduling-prebeta-release-tests.txt).

## Immutable local release identities

Use these exact source commits, rather than a later documentation-only HEAD.
Every archived source file was checked against its recorded Git commit: 614 files
for the common web/cron candidate and 616 for recovery. Archives contain source,
locked dependencies and build instructions; they are not Linux container images.
No branch or artifact has been published by this task.

| Artifact | Local branch | Source commit |
| --- | --- | --- |
| Web and cron | `codex/scheduling-prebeta` | `70989bfbdc65bcaa9c431a90978d88e6af798c65` |
| Bounded recovery | `codex/scheduling-prebeta-recovery-0032` | `7e6c824b8be84eb993175f933a4b6bd1afb51bbd` |

Protected local source archives:

- [Web/cron archive](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-20261013/artifacts-final/web-cron.tar.gz), SHA-256 `3f71cd8df546159f1196e9f0f455337a62205f90c19d3d73c042470119246d10`.
- [Recovery archive](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-20261013/artifacts-final/recovery.tar.gz), SHA-256 `f94499d22c06ce0545a5f0c09049cdb2ee1084b18f525e3b15d3433b79f6b423`.

The old schema-0031 recovery artifact and currently deployed 0031 applications
are not valid recovery targets after this migration.

## Blockers and required configuration

1. **Published coverage is absent.** The eight production periods are all drafts,
   ranging from July 12 to October 11. There is no published current or next week.
   An authorized Lead must review actual current-week coverage and approve/publish
   the current and next operating weeks before releasing employee working-store
   access. Do not bulk-publish historical drafts to manufacture continuity or
   fairness credit. Newly generated future weeks require Lead review/publication.
2. **Horizon configuration needs explicit approval to change in production.** The
   migration intentionally retains X=8/Y=7. Set X=9/Y=8 through the new settings
   workflow during the authorized rollout, before controlled generation.
3. **Review the existing Double Coverage store selector.** It remains configured,
   and one scheduling-active employee retains double-coverage capability. The
   rehearsal produced no double coverage, but the store-level selector should be
   cleared for the approved single-employee operating policy. Preserve employee
   history and the architecture's ability to support future double coverage.
   All 28 active coverage rules already require one employee; no rule increase is
   needed. No production coverage setting was changed here.
4. **Employee identity and exposure require an owner-approved rollout.** There is
   one employee/account link, 11 scheduling-active employees and three Lead-capable
   employees. Scheduling exposure is currently a single principal opt-in; global
   exposure enables only `v2_primary`. Confirm the intended employee links, actual
   Lead review/publication access and selected feature exposure before employee
   rollout. Do not backfill identities, expand permissions or change exposure
   automatically. Unlinked legacy shared-store accounts retain their established
   inventory scope; their successful smoke test does not prove employee-specific
   store resolution for unmapped employees.
5. **Release artifacts need publication and isolated Linux acceptance.** Publish
   both reviewed source commits only when authorized. Build and execute web,
   disabled cron and bounded recovery on the deployment platform's Linux target
   without production writer/provider credentials before migration. Wheel/hash
   validation and successful macOS execution do not establish Linux execution.
6. **A fresh recoverable production checkpoint is still required.** Take and verify
   the checkpoint and secured configuration backup under the future write freeze.
   This rehearsal clone was destroyed and is not a production rollback backup.

Missing base patterns and the unconfigured attendance-point policy produced
nonblocking readiness warnings. They did not block nine-week generation. They do
not justify adding escalation, incident or acknowledgment workflows.

## Recommended deployment order — requires separate authorization

1. Approve the release identities, configuration/exposure plan, maintenance window
   and recovery limitations. Publish both artifacts and pass isolated Linux runtime
   acceptance. Keep auto-deploy off, both execution gates false and production
   cron suspended. Do not resume cron merely to build it without explicit approval
   for any platform-required temporary resume.
2. Freeze all production writers, including web and any background workers. Verify
   production identity and exactly schema 0031. Take and verify the fresh checkpoint
   and secured service configuration backup. Establish the restore decision and
   owner before changing the schema.
3. From the exact candidate runtime, run the migration once, outside build/startup:

   ```sh
   PYTHONPATH=. python -m app.schema_contract upgrade --revision 20261013_0032
   ```

   Require exactly one 0032 version row and verify retained records, links,
   constraints and evidence protection. Do not stamp a revision or disable checks.
4. Promote the recorded candidate to web and cron while writers remain controlled.
   Both use Python 3.10.13 and `sh scripts/release/build.sh`. Web retains its read-only
   schema pre-deploy check and existing uvicorn start command; cron retains
   `PYTHONPATH=. python scripts/run_schedule_automation.py`. Keep cron suspended
   and its execution gate false. Verify actual deployed commits and startup.
5. Under the approved rollout plan, configure 9/8 and the single-employee policy,
   perform controlled manual generation, complete authorized identity/exposure
   setup, and have Leads review/approve/publish current and next-week coverage.
   Confirm the continuity indicator, employee schedules and effective inventory
   scope. Verify counts and pending browser drafts through refresh/re-authentication;
   use emergency access only for expressly authorized temporary exceptions.
6. Reopen affected workflows after acceptance. Leave automation disabled. Any later
   recurring-generation activation is a separate authorization and verification
   step; it never authorizes automatic publication.

This sequence is documentation only. None of these production steps was executed.

## Rollback limits and forward recovery

A destructive downgrade to 0031 was rejected with the expected retained-evidence
error, and the clone remained at 0032. Approval, generation, preference and grant
records must not be deleted to make a downgrade succeed. X=9 also exceeds the old
schema's permitted horizon. Do not deploy old 0031 web/cron/recovery code on 0032.

If a deployed candidate fails after migration:

1. Re-establish write freeze, preserve failure evidence and a fresh backup of the
   current database, suspend cron and keep execution gates false. Do not discard
   inventory work or other writes made after the checkpoint.
2. Confirm schema 0032 and its object/evidence contract. Deploy the recorded bounded
   recovery commit to web and cron under the separately authorized recovery action.
   Recovery cron remains inert even if its execution gate is mistakenly true.
3. Require strict startup checks, the recovery banner, authentication, retained
   effective store scope, preserved counts and 503 scheduling/account mutation
   rejection. Only unaffected workflows may reopen. Scheduling, preference
   decisions, identity changes and new emergency authorizations remain paused;
   existing grants still expire and are revalidated. Recovery is reduced service.
4. Repair forward against a new disposable checkpoint containing the retained
   evidence. Test and seal the repaired schema-compatible candidate, promote its
   exact identity, verify retention and restore workflows. A return to the accepted
   candidate after bounded recovery was successfully rehearsed. Keep automation
   disabled until separately authorized.

If the migration fails, require proof of complete transactional rollback to 0031
before considering the old application. For partial, inconsistent or unknown
schema state, keep writers stopped and use a reviewed forward repair or explicitly
authorized checkpoint restore. Reconcile post-checkpoint writes before any restore.
Never force a version stamp, disable schema protection or run historical mutation
scripts from the recovery artifact.
