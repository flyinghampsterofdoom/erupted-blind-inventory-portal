# A1 evidence-preserving application rollback

This is a separate compatibility build from exact production commit
`35d6c7f3683a1503dbab65a32fabddfdb1486828`, on branch
`codex/scheduling-a1-rollback-compat`. It is not the unchanged previous application.
It must not be merged wholesale into A1: its native migration head remains 0027.
No production settings, schemas, schedules, or data were changed for this work.

## Schema compatibility finding

The prior contract already represents accepted revisions as a set, but populated
it with only native HEAD_REVISION (`20260927_0027`). The application startup hook
calls `assert_supported_schema`; unknown/unversioned schemas fail. No ordered
revision range or migration-specific forward-compatibility metadata exists.
The existing named Render compatibility profile concerns initial baseline schema
recognition/stamping, not permission to accept a later runtime schema.

This build adds one explicit accepted revision: `20260928_0028`. Native head,
Alembic graph, structural comparison, migration and stamping rules remain unchanged.
Future heads are not automatically accepted. No validation becomes a warning.

Independent migration inspection and PostgreSQL before/after introspection show
that A1 adds only snapshot storage, its constraints, and its immutability function
and statement trigger. Existing tables, columns, types, nullability, checks,
indexes, enums, extensions, and noninternal triggers are unchanged. No business
rows are backfilled. UPDATE/DELETE/TRUNCATE protection targets only the new table.

The new RESTRICT foreign keys retain referenced periods and principals. They also
install ordinary PostgreSQL internal referential-integrity triggers; this is not
permission to delete historical parents. Normal previous-version scheduling does
not hard-delete those parents. Ordinary edits, period version updates, board
reads, and generation at native schema are supported. Compatibility does not mean
that arbitrary destructive administrative SQL remains allowed.

## Narrow generation safety guard

The rollback build contains NO snapshot model, serializer, capture hooks, or A1
migration. Consequently it must not generate uncaptured proposals on 0028, even
when the table is empty. A live transaction query checks for exactly native 0027
before these entry points perform writes:

- per-period regeneration (all current form/API routes);
- internal generated-period creation;
- rolling-horizon filling;
- manual horizon generation;
- the automation tick used by both API and cron.

On 0027, normal generation proceeds unchanged. On 0028 or another unexpected
revision, a clear SchedulingConflict is raised. There is no cached startup flag
or override switch; disabling the existing optional startup check cannot bypass
this guard. Database errors also fail closed. This applies to supported generation
entry points, not privileged scripts that bypass the service architecture.

The entire automatic tick is blocked on 0028, including automatic publication.
This avoids a mixed partially successful rollback tick. Manual existing draft
editing and manual publication retain their original authorization and behavior.
The generation buttons are unchanged and return the existing error presentation.
Cron will report failure unless paused operationally; it cannot silently generate.

## Validated cross-build handoff

Tests use disposable loopback PostgreSQL databases only. They run independent
Python processes with separate source roots, preventing A1 code from leaking into
the rollback process:

1. Create native 0027 database and seed a representative draft.
2. Start compatibility application's actual startup hook; read/edit a schedule and
   run native generation normally.
3. Run A1 migration and A1 capture using worktree commit
   `7dae388b807cefd4e4a352057bb70a579a86e772`.
4. Start compatibility application on the same 0028 database; confirm its ORM has
   no snapshot model. Read/edit existing schedule; test all generation entry points
   are blocked, even with the optional startup schema check disabled.
5. Attempt snapshot UPDATE, DELETE and TRUNCATE; PostgreSQL rejects each.
6. Compare stored payload text and checksums byte-for-byte with pre-rollback values.
7. Restore the A1 process on the same database, without migration/downgrade; verify
   old checksums and continue proposal sequence 1 -> 2.

The test also compares native table definitions before/after 0028 and verifies the
only additional noninternal trigger belongs to snapshot storage. Unknown future
heads and unversioned/older unapproved schemas remain rejected at startup.

## Release topology and operational procedure

- Pre-A1: original production application + 0027.
- A1: reviewed A1 application + 0028.
- Post-evidence emergency rollback: this exact compatibility build + retained 0028.
- Recovery: reviewed A1 (or later compatible build) + retained 0028.

Retain the compatibility commit as a separately tested local branch/artifact;
record its immutable SHA in the release approval. Future authorized remote
retention/build preparation should use a non-production reference, not a main
merge. Deployment must select the approved commit for both web and automation
services; never substitute original production 35d6c7f for this compatibility build.

When deploying/rolling back is separately approved:

1. Quiesce generation and finish/drain in-flight generator requests before changing
   builds/schema. Suspend scheduling automation for the transition. An already
   running pre-A1 cron process has no new guard; the new artifact cannot protect it.
2. Verify schema, deployed commit, evidence count/checksums, and reviewed artifacts.
3. Keep 0028 installed after evidence exists. Do not run rollback-build migration,
   stamping, or local bootstrap commands against it: that build's Alembic graph
   deliberately ends at 0027. Its runtime startup does not migrate the database.
4. Select the compatibility application and cron artifact. Its technical generation
   guard remains active even if automation is inadvertently resumed.
5. Verify normal application startup and operational reads; verify generation is
   blocked and snapshot evidence unchanged. Treat generation refusal as intentional.
6. Recover to reviewed A1 and validate startup and evidence before resuming
   generation. Subsequent proposals continue existing per-period sequence.

### Render pre-deploy prerequisite (read-only inspection)

The current web-service preDeployCommand explicitly asserts revision equals
`20260927_0027`. It would prevent *either* A1 or compatibility startup on 0028,
regardless of application compatibility. No production command was changed.
The approved deployment plan must replace that pinned command with a bounded,
read-only check appropriate to the selected build. For example:

```python
from app.db import engine
from app.schema_contract import current_revision, SUPPORTED_REVISIONS
revision = current_revision(engine)
assert revision in SUPPORTED_REVISIONS, revision
print('Read-only schema check passed:', revision)
```

This checks the shipped explicit set and does not bypass checks through an
optional configuration setting. Do not replace it with automatic migration,
open-ended version comparisons, or a warning. The inspected web start command is
uvicorn; cron invokes run_schedule_automation.py. Neither implicitly migrates.

### Four rollback cases

| Case | Safe procedure |
|---|---|
| Application rollback before evidence exists | Compatibility build can run against 0028 unchanged; generation stays blocked. Original application requires explicitly validated downgrade to 0027 first. |
| Application rollback after evidence exists | Compatibility build on retained 0028; no schema downgrade, no generation, ordinary emergency operations available. |
| Schema downgrade before evidence exists | Only reviewed A1 migration tooling, after generation is quiesced and table emptiness verified. It is optional, never automatic. |
| Schema downgrade after evidence exists | Prohibited as ordinary rollback. Migration refuses evidence destruction. Preserve 0028 and use compatibility application. |

A1 implementation needs no code change for this strategy. Its deployment/rollback
runbook must reference this artifact and the pre-deploy prerequisite. Independent
A1 RC review can resume; this document is not deployment approval.

## Scope and tests

Only two runtime files change: app/schema_contract.py and
app/services/v2_scheduling_policy_service.py. One focused test module and this
runbook are added. No migration, model, UI, authorization, fairness, transfer,
attendance or A2 change is included. Every existing policy-service function was
AST-compared with production after removing the new entry-guard statements:
all original function bodies are identical.

Validation:

- Focused bounded-compatibility / real cross-build handoff plus existing schema
  contract tests: **21 passed**.
- Complete existing Scheduling foundation/repair plus PostgreSQL migration suite:
  **345 passed** (338 Scheduling + 7 migration tests).
- Separate unchanged-baseline diagnostic: reproduced the pre-existing constraint
  failure described below on native 0027 (one expected-failure diagnostic passed).
- All 48 existing policy-service functions are AST-identical to production after
  removing only the five new generation entry guards.
- Python compilation and `git diff --check`: passed.
- Only existing FastAPI startup-event deprecation warnings were emitted.

Environment: unreachable default application DATABASE_URL; empty Square token;
mock snapshot provider; disposable local PostgreSQL via TEST_POSTGRES_ADMIN_URL.
Cross-build testing additionally sets A1_SOURCE_DIR to the untouched A1 worktree.
No production generation or migration occurred.

### Incidental pre-existing generator finding

The first handoff fixture used an unlocked manually created unassigned position.
After generation designated that row Lead, a second regeneration could fail
`schedule_shifts_lead_assigned_ck` while clearing its employee before clearing the
Lead flag. This was reproduced separately on unchanged baseline application code
and native 0027, not caused by 0028 or this compatibility guard. It was not fixed
in this narrowly scoped task. The successful handoff uses an ordinary assigned,
manually locked emergency edit. Independent A1 review should retain this finding
for generator-defect triage rather than interpret it as a schema incompatibility.
