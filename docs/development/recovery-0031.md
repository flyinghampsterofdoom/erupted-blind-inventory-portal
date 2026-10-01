# Emergency application recovery on schema 0031

This branch is derived directly from production `35d6c7f3683a1503dbab65a32fabddfdb1486828`.
It is an operationally bounded recovery application, not a second forward-release
candidate. It requires **exactly `20260930_0031`** and never downgrades the database.
Its migration files remain the production parent's historical graph, ending at
0027; they are not the operational schema contract and must not be executed.

## Recovery contract

- Existing accounts with passwords can log in; pending passwordless accounts fail
  authentication cleanly. Inactive linked employees cannot log in or keep using a
  session. Authentication/session writes and unrelated inventory/ordering work
  remain supported under their existing authorization and Square policy.
- HR, employee, scheduled-assignment and catalog reads remain available.
- All Scheduling and HR HTTP mutations, legacy account/password/permission edits,
  and store credential changes return 503. No Scheduling mutation is supported.
- ORM flush and bulk insert/update/delete guards also reject protected-domain
  writes. These protect application paths; this is not a substitute for database
  operator access controls. Do not run ad-hoc SQL or historical maintenance scripts.
- The schedule board does not rebuild warning rows. Recovery banners explicitly
  describe planned assignments; new commitment/Lead exceptions are not displayed.
  Do not use this old view to infer actual replacement, Lead, or fairness outcomes.
  Legacy readiness/automation/transfer-review pages are unavailable.
- Cron's script is permanently inert. The service function also rejects direct
  automation calls. Keep the service suspended and execution gate false.
- Password email/setup and encrypted integration editing are unavailable. Existing
  settings, hashes, nullable pending accounts and reset-token evidence are retained.
- Schema validation cannot be disabled. It checks one exact revision, all legacy
  column names, and the reviewed new-table/principal contract, including partial
  unique indexes and the snapshot trigger/function/enabled state. Arbitrary new
  heads, missing columns or disabled snapshot protection are rejected.

`app/rollback_schema_0031.json` was generated from the canonical migrations on a
new disposable local PostgreSQL database. Physical principal column ordering is
normalized because the existing production baseline has a documented order
variation. No other new-object differences are normalized.

## Rehearsal

The canonical repository owns `scripts/release/rehearse_rollback.py` and its
separate-process probe. They refuse non-loopback targets, use a unique disposable
database, and remove it in `finally`.

The rehearsal upgrades 0027 -> 20260929_0028 -> 20260928_0028 -> 0029 -> 0030 ->
0031, retaining a baseline password and synthetic identities. It then inserts
snapshot, Lead outcome, commitment, encrypted-setting, reset-token and throttle
fixtures. Against the recovery application it checks startup, authentication,
HR/profile, schedule HTML/API, ordering/catalog reads, a real count-group mutation,
all registered Scheduling/HR mutation routes, legacy password paths, direct ORM
and bulk writes, configured cron, mandatory schema validation and drift rejection.
Finally it compares retained-evidence fingerprints and restarts canonical.
No real email, Square request, production identity or operational DB is used.

## Deploying this recovery build (requires separate deployment authorization)

1. Put web into maintenance mode and stop request writers. Suspend production cron
   and keep `SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false`; auto-deploy remains off.
2. Confirm one Alembic row `20260930_0031`. If migration did not complete, do not
   deploy this build. Escalate to the checkpoint/partial-migration recovery plan.
3. Use Python 3.10.13 and `sh scripts/release/build.sh` on both services. Keep the
   existing DATABASE_URL, app secret, secure-cookie configuration and integrations.
4. Web pre-deploy: `python -c "from app.schema_contract import assert_supported_schema; assert_supported_schema()"`.
   Web start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
5. Deploy **this branch's recorded immutable SHA**, not the old production SHA:
   `render deploys create srv-d6ccg9ktgctc738u1c1g --commit <RECOVERY_SHA> --wait -o json`.
6. Verify startup, HTTP/login, the recovery banner, HR/catalog reads, and 503
   rejection of Scheduling/account writes. Verify schema and retained evidence.
   Reopen unaffected workflows only after these checks.
7. Cron command: `sh scripts/release/cron.sh`. Deploy the same recovery SHA while
   execution is independently gated off; keep cron suspended afterward. If the
   platform requires resume to build, confirm the false execution gate first and
   suspend again after artifact activation. The recovery script is inert even if
   the gate is accidentally true.
8. Do **not** run Alembic downgrade, stamp 0027, reset passwordless hashes, delete
   new evidence, or use Render's old baseline artifact as a post-0031 rollback.
9. To leave recovery, deploy a tested repaired forward candidate at schema 0031,
   validate it, then separately restore scheduling writes/cron. Keep auto-deploy
   off until the tracked release branch and coordinated migration workflow agree.

The recovery SHA must be pushed and made buildable before the forward production
migration is authorized. Local rehearsal does not make an unpublished commit
available to Render.
