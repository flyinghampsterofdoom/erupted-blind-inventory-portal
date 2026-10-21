# October 1 release preparation: schema 0031

READY FOR CONTROLLED DEPLOYMENT, with bounded recovery as defined below.
No push, Render configuration change, deployment, production migration, cron
resume or email delivery was performed during preparation. The old Render incident
is closed. This record does not reopen it.

## Immutable artifacts

- Production parent: `35d6c7f3683a1503dbab65a32fabddfdb1486828`, schema `20260927_0027`.
- Audited candidate before preparation: `882b16ecffc7de44d1da4c4730c03bf93853dc30`.
- Superseded candidate: `32763bd1c33a9d70c948c7bf3187a69fc239ccce`; do not deploy it.
- Candidate: the execution-safety patch commit containing this updated record on
  `codex/admin-v2-canonical-integration`.
  Resolve with `git rev-parse HEAD`; the final handoff records the full immutable SHA.
- Recovery: `f66c988d2bad8ffaec1af9d4a8e81dc3bb3b0e66` on `codex/rollback-schema-0031`,
  derived directly from the production parent. Its only supported operating schema
  is `20260930_0031`. Its historical migration graph still ends at 0027 and must
  never be used to migrate or stamp production.

Neither local branch has been published by this task. Publication and successful
safe-environment builds of both exact SHAs are required before production migration.
This is a deployment-sequence prerequisite, not permission to push now.

## Tests and bounded changes

The execution-safety patch passed **1223 tests and 7 subtests**, with one optional
real-R2 skip, in the same locked environment. It also passed 58 focused gate tests,
444 affected scheduling tests, 17 lifecycle tests, and `pip check`. See
[execution-gate-0031-validation.md](execution-gate-0031-validation.md). Older totals
below describe the initial preparation before that bounded patch.

The two failures were independently reproduced as wall-clock interactions. In
`test_lifecycle_transfer_revalidates_cutoff`, transfer completion now receives the
same explicit September 1 date as creation. In
`test_lifecycle_attendance_replacement_respects_shift_date`, `_now` is fixed at
October 2 02:00 UTC (October 1 19:00 Pacific), after the tested shift ends. Fixture
business dates were not moved into the future. Production stale-transfer, shift
end, employment-cutoff and historical-attribution guards were not changed.

- Focused lifecycle: **17 passed** (296 deselected).
- Affected scheduling/attendance/coverage, snapshots, Longview and Lead: **444 passed**.
- Full suite after the two fixes, original environment: **1165 passed, 1 skipped,
  7 passing subtests**.
- Superseded candidate full suite in a fresh locked release environment: **1169 passed, 1 skipped,
  7 passing subtests**, 432.33 seconds, three existing deprecation warnings.
- The only skip is opt-in real private-R2 integration (`RUN_REAL_R2_TESTS=1`).
- The initial release-entrypoint cases have been expanded by the execution-safety
  patch; see `execution-gate-0031-validation.md` for current totals and evidence.
- Email deferral test uses the real transport entrypoint with no encryption key,
  fails if HTTP delivery is attempted, and verifies saved account, visible inactive
  email status, HR and Integrations access.

The initial preparation changed no canonical application implementation. The
subsequent bounded safety patch adds an execution guard at the script and shared
automation service; scheduling business logic and migrations remain unchanged.
Square policy is unchanged. No credentials were created and no real provider deliveries occurred.

## Runtime and dependency contract

See [release-runtime.md](release-runtime.md) and `requirements.in` for the complete
intentional direct dependency list. Python **3.10.13**, FastAPI **0.129.0**,
Starlette **0.52.1**, and all production dependencies/transitives are fixed in a
universal hash lock. Pytest **9.0.2** is in a separate test lock. Web, cron and
recovery share identical runtime/lock/build files. The build fails on another
Python version, installs only wheels with required hashes, and runs `pip check`.
Fresh macOS installation and full execution passed. Linux x86-64 target wheel
installation/hashes passed; Linux runtime execution and Render builds have not
been performed in this no-deploy pass.

Render's `PYTHON_VERSION` overrides `.python-version`: set both services to
`3.10.13` explicitly. [Render runtime documentation](https://render.com/docs/python-version).

## Migration graph

There is exactly one canonical head: `20260930_0031`. The exact rehearsed sequence:

1. Existing `20260927_0027`.
2. `20260929_0028`: employee access, nullable password hashes, encrypted settings,
   password reset tokens and auth throttles.
3. `20260928_0028`: immutable generator snapshots, branching from 0027.
4. `20260930_0029`: merge of both 0028 heads, no additional DDL.
5. `20260930_0030`: audited Lead outcomes.
6. `20260930_0031`: auditable replacement commitments.

These are five added migration files, unchanged from the audited candidate.
The two 0028 revisions are sibling branches, not chronological revision sorting.
Upgrade to the explicit final head; do not stamp or individually fabricate state.

## Why recovery pauses scheduling and account changes

The old application has no models or service lifecycle for 0030 Lead outcomes or
0031 commitments. Its shift/attendance/employee changes cannot revalidate or void
those records through the new audited workflow. Allowing those writes would leave
actual-coverage and fairness evidence inconsistent even if foreign keys still
pass. Passwordless pending accounts also require authentication compatibility.
Consequently, the bounded recovery disables all Scheduling/HR/account mutations,
removes the board's warning-cache write, and makes automation permanently inert.
This is intentionally reduced service while a repaired forward release is prepared.

Recovery accepts only 0031 and validates the new table/principal shape, partial
indexes and snapshot trigger/function/enabled state, plus legacy column presence.
Schema validation cannot be disabled. HTTP and ORM guards protect normal
application paths. They do not authorize ad-hoc SQL or historical scripts.
Existing password authentication, HR and planned-schedule reads, ordering/catalog
reads and unrelated inventory workflows remain available. Banners warn that old
planned assignments do not display actual replacement/Lead exceptions. Recovery
is not suitable for operating live scheduling through the old UI.

The disposable-local rehearsal upgrades the complete chain, seeds synthetic new
evidence and nullable accounts, starts recovery in another process, checks login,
reads, every registered Scheduling/HR mutation rejection, a committed inventory
count-group write, ORM/bulk-write protection, configured cron and direct automation
rejection. Missing schema columns, disabled snapshot protection and disabled schema
validation are rejected. Retained identities, schedules, snapshots, commitments,
Lead outcomes, encrypted settings, reset tokens and throttle fingerprints remain
unchanged. Canonical then starts again against the same database. Schema stays
0031 and the disposable DB is removed. See
[evidence/recovery-0031-rehearsal.json](evidence/recovery-0031-rehearsal.json).

To reproduce from canonical with the locked test environment and local PostgreSQL:

```sh
PYTHONPATH=. TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://justinrawlinson@localhost/postgres \
python scripts/release/rehearse_rollback.py \
  --rollback-path /Users/justinrawlinson/Developer/Erupted/worktrees/rollback-0031 \
  --report /tmp/erupted-rollback-release-rehearsal.json
```

The tool refuses non-loopback administration targets; it creates its own unique DB.
Do not use the development-only `--write-contract` flag in verification runs.

## Controlled deployment sequence — future authorized operation only

1. Obtain deployment/publication authorization. Publish both release branches and
   record both full SHAs; verify Render can fetch each. Build both in an isolated
   safe environment with no production writers/provider credentials before migrating.
2. Before **any Render candidate build/deployment**, install and verify the false
   execution safeguard described in the section below. Keep auto-deploy off. Leave validation cron `crn-datauak9v7es7380nt70` untouched.
   Keep production cron `crn-da6pd2qd0e5s73dbudr0` suspended. Set and verify
   `SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false` before any later resume/build.
3. Configure web `srv-d6ccg9ktgctc738u1c1g` and production cron with Python 3.10.13,
   build `sh scripts/release/build.sh`, and the published candidate branch. Preserve
   DATABASE_URL, app secret, secure cookies and authorized integrations; production
   environment, demo bootstrap off and schema checks on must remain in force.
   Web pre-deploy is the read-only command below; cron command is
   `sh scripts/release/cron.sh`. Email may remain unconfigured.

   ```sh
   python -c 'from app.schema_contract import assert_supported_schema; assert_supported_schema()'
   ```

   Web start remains `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Establish maintenance/write freeze, stop all writers, and take a verified
   pre-migration DB checkpoint/backup plus a securely stored service configuration
   backup. Confirm target DB identity and exactly one baseline revision 0027.
   Record how to restore the checkpoint before proceeding. Do not overlap baseline
   writers with migration or candidate writers.
5. From the exact candidate runtime, with secured production DATABASE_URL, execute
   once (never in every start/build):

   ```sh
   PYTHONPATH=. python -m app.schema_contract upgrade --revision 20260930_0031
   ```

   Verify exactly one 0031 row and expected schema/evidence protection. Stop on error.
6. Deploy the recorded exact candidate SHA to web, then production cron:

   ```sh
   render deploys create srv-d6ccg9ktgctc738u1c1g --commit <CANDIDATE_SHA> --wait -o json
   render deploys create crn-da6pd2qd0e5s73dbudr0 --commit <CANDIDATE_SHA> --wait -o json
   ```

   If the platform requires cron resume to build, first independently verify the
   false execution gate and intended branch/build/cron command. Resume only within
   this authorized deployment, then immediately suspend after activation. Verify
   actual running SHA, not merely requested SHA; do not run an old cron on 0031.
7. While writes remain controlled, verify HTTP/startup, authorized login/roles,
   HR, own published schedule, board/coverage/Lead/fairness and ordering reads.
   Verify schema0031 and retained evidence. Use controlled synthetic/staging data
   for mutation acceptance; do not invent operational production assignments.
   Reopen web only after acceptance. Email activation is not a gate.
8. Enable cron only after both running SHAs match the accepted candidate, schema
   checks pass and due scheduling work/policies have been reviewed. Set execution
   gate false while resuming the service, verify disabled execution, and only then
   set the gate true as the separately accepted activation step. Keep
   auto-deploy off until a separately approved coordinated release workflow exists.

## Exact post-migration recovery sequence

If the candidate fails after reaching 0031, even five minutes later:

1. Restore maintenance/write freeze; stop candidate writers. Suspend production
   cron and keep execution gate false and auto-deploy off. Preserve failure logs
   and a fresh evidence backup; do not discard new production writes.
2. Verify the DB is still exactly 0031. Retain the production connection/secrets,
   Python/build/pre-deploy/start configuration above. Choose the published recovery
   branch for any service-resume build and verify its exact SHA.
3. Deploy the immutable recovery application (not the old baseline artifact):

   ```sh
   render deploys create srv-d6ccg9ktgctc738u1c1g --commit f66c988d2bad8ffaec1af9d4a8e81dc3bb3b0e66 --wait -o json
   render deploys create crn-da6pd2qd0e5s73dbudr0 --commit f66c988d2bad8ffaec1af9d4a8e81dc3bb3b0e66 --wait -o json
   ```

   Apply the same independently verified false-gate/resuspend precaution if a
   cron build requires resume. Recovery cron itself is permanently inert.
4. Require recovery's full schema gate, startup/login, visible recovery banner,
   HR/catalog reads and 503 scheduling/account-write rejection. Verify 0031 and
   retained evidence. Reopen only unaffected workflows. Scheduling/account changes
   remain unavailable; communicate this limitation to operators.
5. Repair and test forward against 0031; repeat recovery rehearsal as needed,
   deploy that repaired candidate, verify evidence, then restore scheduling/cron.
   Never downgrade, stamp 0027, reset pending hashes or delete retained evidence.

If migration failed and the schema is not exactly 0031, this recovery build must
refuse startup. Keep writers stopped. Verify whether the transaction restored the
complete 0027 state before considering the baseline application. Otherwise use
the verified pre-migration checkpoint under write freeze and the recorded restore
plan. Any writes after the checkpoint must be preserved and reconciled before a
restore; do not perform an unreviewed destructive restore or fake a revision stamp.

## Deferred email activation

Email is safely deferrable. Without a settings row/key, account creation preserves
the pending account, delivery reports disabled/incomplete, and unrelated Admin
remains functional. Pending accounts cannot authenticate until password setup.
Existing accounts retain login. No production key, DNS or Resend configuration
was modified and no real email was sent.

Later, under separate activation authorization:

1. Generate a Fernet key using `Fernet.generate_key()` from `cryptography.fernet`
   in a secure operator environment. Store it in the secret manager and set
   `INTEGRATION_ENCRYPTION_KEY` on the web service (and any future email worker).
   Back it up independently of DB ciphertext. Do not log it or commit it; do not
   rotate it casually without a ciphertext re-encryption plan.
2. Add the intended sender domain/subdomain in Resend. Publish exactly its supplied
   verification/SPF/DKIM records and wait for verified sending status. Preserve
   existing Microsoft 365 receiving MX and SPF configuration; do not replace root
   mail records with a new sending service. Use a dedicated sending subdomain where
   appropriate. [Resend domain verification](https://resend.com/changelog/domain-verification-events).
3. Create a sending-only API key restricted to that domain. Turn off open and
   click tracking in domain configuration for these password links.
   [Resend API contract](https://github.com/resend/resend-openapi/blob/main/resend.yaml),
   [tracking configuration](https://resend.com/blog/open-and-click-tracking).
4. In Admin → Settings → Integrations (`/admin/settings/integrations`), enter
   From address on the verified domain, display name, optional Reply-to, and the
   actual HTTPS employee portal origin with no path/query/fragment. Select Replace
   API Key and save the key encrypted; then deliberately enable Resend and save.
   Resend values live in the database, not invented RESEND_* environment variables.
5. Send Test Email only to an explicitly authorized mailbox. Confirm inbox delivery,
   then test one authorized setup/reset lifecycle, one-hour expiry, one-time use,
   valid HTTPS origin and secret-free logs. Provider acceptance alone is insufficient.

## Remaining limits

There are no unresolved local preparation failures. The operational recovery
scope is reduced service, not full old scheduling behavior. Publishing artifacts,
safe-environment platform builds, backup verification and production acceptance
remain mandatory steps in a later authorized deployment. Real R2 and email provider
integration were deliberately not exercised here. Production was not modified.


## Execution-safety patch and pre-flight correction

The October 1 deployment attempt stopped before publication/migration because the
baseline cron lacked an independent execution gate. This patch supersedes candidate
`32763bd1c33a9d70c948c7bf3187a69fc239ccce`; recovery remains
`f66c988d2bad8ffaec1af9d4a8e81dc3bb3b0e66` unchanged.

Live production and separate validation cron currently both use
`PYTHONPATH=. python scripts/run_schedule_automation.py`. The new candidate keeps
that entrypoint and guards it before database/configuration imports. The release
shell wrapper delegates to the same Python script. Script, module and imported
`main()` calls use the same gate. The shared `run_schedule_automation` service
also checks before its first SQL statement, protecting the manual
`POST /v2/scheduling/api/automation/run` route and internal service calls.
Intentional manual schedule editing/generation is a separate authorized workflow;
this switch controls automatic ticks, not all scheduling writes.

`SCHEDULE_AUTOMATION_EXECUTION_ENABLED` is read from process environment on each
invocation. After trimming whitespace and lowercasing, only `true`, `1`, `yes`,
`on` permit execution. Missing, empty, false, zero, no, off and every unrecognized
value disable it. Disabled script execution returns exit 0 and logs/prints
`SCHEDULE_AUTOMATION_DISABLED`. The service/API returns `ok=false`,
`status=disabled`, `execution_enabled=false` and empty generated/published/blocked
ID lists, before SQL, locking, generation, publication or fairness/attendance/
coverage work. Enabled script execution checks schema before selecting an actor.
Existing application policy, authorization, timing and publication guards remain.
An environment change does not cancel an already-running tick.

### Before any Render candidate build/deployment

Production configuration is **not** corrected by this local patch. Keep production
cron suspended and auto-deploy off. On the next authorized release, set
`SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false` on production cron and verify the
saved value. The old live baseline does not contain the new Python guard, so
setting the variable alone is insufficient. Before any build or resume, configure
the following baseline-compatible transition command (one line):

```sh
PYTHONPATH=. python -c 'import os, runpy; enabled=os.environ.get("SCHEDULE_AUTOMATION_EXECUTION_ENABLED", "").strip().lower() in {"true", "1", "yes", "on"}; runpy.run_path("scripts/run_schedule_automation.py", run_name="__main__") if enabled else print("SCHEDULE_AUTOMATION_DISABLED: no automation work performed.")'
```

This transition command is only a bridge for the immutable old baseline. Candidate
protection is implemented in Python at both entrypoint and shared service, so it
does not depend solely on the transition shell command. Independently execute the
configured command in the service environment with the false value, without
resuming scheduled work; require exit 0, disabled marker and no application/DB
work. If the platform cannot provide that verification while preserving suspension,
STOP before builds/migration and resolve the verification mechanism. Never assume
configuration presence proves enforcement.

After installing the new candidate, configure `sh scripts/release/cron.sh` and
repeat that check in its actual runtime. Leave validation cron untouched. During
all builds/deployments preserve the false gate. If a service must be resumed to
build, first verify the transition command and false gate; resuspend after build.

### Activate or disable deliberately

Activation has two distinct requirements: the service is operational/resumed as
required, **and** the process gate is true. First resume only with the verified
false gate. After web acceptance, matching web/cron SHA, schema0031, valid policy
and due-work review, set the gate explicitly to `true`, verify the effective value
on the next execution, and inspect its first accepted result and logs. Auto-deploy
remains off. Existing long-lived web processes must also receive an explicit gate
value if manual HTTP automation is later authorized; absent remains disabled.

To disable again: (1) suspend cron immediately to prevent new ticks;
(2) set its gate to `false` and verify the saved/effective value;
(3) identify any tick already running, stop it safely or let it finish under the
approved write freeze, and verify no automation writer remains;
(4) independently invoke the disabled command in the effective service environment
and require the disabled marker, exit 0 and no work. Keep suspended until a new
activation gate passes. For recovery, also stop request writers and preserve
evidence as already required; never assume changing env retroactively cancels work.

Recovery is independently safe even with the gate true: its exact Python script
always reports `RECOVERY_DISABLED` and exits 0, its common automation service
always raises before touching a database, and its protected-domain mutation guards
remain installed. It does not need a new recovery commit.
