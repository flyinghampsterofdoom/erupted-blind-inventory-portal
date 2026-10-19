# Render deployment verification and cron recovery

This replaces assumptions in the ad hoc deployment runners. No command in this
document authorizes a deployment. Production execution requires a new owner
instruction naming the exact release. Never deploy the active development HEAD
implicitly, run an application smoke-test writer, or manually trigger cron.

## Evidence and classifications

- **PASS** requires actual evidence for the particular gate. Render accepting a
  request, finishing a build, or returning a deployment SHA does not prove that
  the new application or cron command ran successfully.
- **WAIT** is only expected deployment progress, recognized old-instance rollover,
  or awaiting the first normal scheduled execution within the reviewed deadline.
  It is never a pass. Cron readiness can take almost a full hour; do not shorten
  the schedule, manually run the job, or interpret this expected wait as a failure.
- **STOP / application or identity failure** includes unexpected revision/digest,
  invalid runtime source, wrong service/role, schema mismatch, and actual HTTP 500.
- **STOP / authentication** requires normal owner sign-in or provider credential
  renewal. Never invent a session, change session expiry in SQL, or reuse an
  expired token. A login redirect is not a successful smoke test.
- **STOP / provider evidence or infrastructure** includes malformed JSON or dates,
  unknown provider status, missing pagination metadata, transport deadlines, and
  unavailable evidence. It does not establish an application defect, and cannot
  be promoted to PASS. Follow the owner's specific stop/recovery policy; do not
  automatically retry deployment or improvise a verification bypass.

## Complete preflight before activation

1. Rehearse the exact release from `scripts/release/prepare_checkout.py` using a
   new detached checkout. It transfers committed objects only, preserves all
   active-workspace edits, and refuses an existing destination. Build with pinned
   Python 3.10.13 and hash-locked requirements. Compare the full manifest, not a
   shortened SHA. All release shell entrypoints must be committed executable;
   never set `core.fileMode=false` to hide a difference.
2. Run the entire verifier suite, including the captured Render fixture replay:
   `python -m pytest tests/test_runtime_identity.py tests/test_release_workspace.py tests/test_deployment_provider_contracts.py tests/test_template_feature_binding.py`.
   Fixtures contain the actual nanosecond log timestamp that previously failed,
   real service/deploy/event shapes, and the successful restored cron run pairs.
   The simulated provider covers web rollout, old-instance response, disconnection,
   cron WAIT then successful execution, and baseline recovery. It never contacts
   production, creates services, or triggers real jobs.
3. Capture original web/cron deploy IDs and full commit SHAs, current branch,
   autodeploy, schedule, start/build commands, environment/group/secret-file
   references and protected configuration hashes. Keep credential values out of
   reports. Autodeploy must already be off. Record all intended restoration data
   before any mutation. Verify the database backup in isolated PostgreSQL and
   fingerprint protected business records, including order #1 and PDF references.
4. Establish an ordinary, active owner session. GET `/session-status` reports its
   actual expiry and principal; use that value rather than the code's default TTL.
   Production was configured for **30 minutes**, not the default 60. Recheck the
   protected read-only runtime/page through the standard authentication mechanism
   before half the remaining session lifetime, at most every five minutes during
   a long cutover. This permits ordinary middleware renewal; it does not permit
   bypassing expiry. If it expires, require a new normal login and stop dependent
   checks. `/session-status` itself intentionally does not renew the session.
5. Verify the remote release branch equals the full approved commit immediately
   before each build. Web can use explicit `commitId`; cron cannot. For cron use a
   uniquely pinned branch and verify both the returned build commit and its final
   live metadata. Branch drift or an ambiguous mutation response is a STOP.
6. Recognize all provider progress states: `created`, `queued`,
   `build_in_progress`, `pre_deploy_in_progress`, `update_in_progress`.
   `build_failed`, `pre_deploy_failed`, `update_failed`, `canceled`, and
   `deactivated` cannot pass. An unknown status is an evidence-format failure.
   Poll one recorded deploy ID with a bounded deadline; never issue another POST
   because a request timed out. Discover its result with GET before deciding how
   to recover.
7. Deploy web only after all preservation gates pass. Once metadata is live, allow
   120 seconds of GET-only runtime stabilization. A known previous revision,
   route 404 during rollover, or 429/502/503/504 can be transient. Unexpected
   revision/digest, wrong owner/service/schema, or genuine HTTP 500 stops at once.
   No SSH connection is part of the verification path.
8. Verify owner access and actual served asset hashes; inspect order #1 and fresh
   Square counts through read-only endpoints. Re-run the independent arithmetic
   oracle against preserved evidence, not by generating recommendations. Do not
   update saved drafts, create orders, record payments, or adjust inventory.
9. Only after web passes, deploy cron at the same exact approved commit. Preserve
   `0 * * * *`, existing commands, all environment values, and execution flags.
   Inspect authenticated Render logs scoped to that resource after the new deploy
   finished. Require the actual `ERUPTED_RUNTIME_IDENTITY` role/revision/digest and
   successful completion. A pending normal run returns WAIT. Keep owner auth fresh
   through ordinary GETs while waiting, not through database changes.
10. Perform final business/configuration comparisons and report exact service IDs,
    deployment IDs, source revision, actual execution proof and all limitations.

## Timestamp and transport contract

`rfc3339.timestamp` validates a real calendar/time and a known timezone, converts
whole seconds to UTC, and retains an arbitrary fractional part as an exact
`Fraction`. Whole seconds, 1–12+ fractional digits, UTC Z/z and explicit signed
hour/minute offsets compare consistently. No float or microsecond truncation is
used. A one-nanosecond boundary remains distinct. Missing/invalid timezone,
impossible dates and malformed fractions fail verification. RFC3339 `-00:00`
still identifies a known UTC instant and is accepted. Leap-second notation is not emitted by the
captured provider and is currently rejected rather than assigned an invented
ordering; it is not a tested provider contract.

Render's `lastSuccessfulRunAt` has whole seconds while log records can have nine
fractional digits. A fast run completed in the same second needs a matching,
ordered provider success marker; otherwise it remains WAIT. A subsequent run's
marker cannot complete an earlier proof. Explicit failed-run markers stop.

GET transport has one bounded deadline, per-call timeouts, limited retry for
connection resets/partial reads/429/5xx, and bounded pagination with repeated-cursor
protection. Authentication and malformed evidence are not silently retried as
application rollover. No POST/PATCH uses this automatic retry helper.

## Supported cron recovery (not native rollback)

Render rejected native cron rollback with HTTP 400: `rollbacks only supported for
type server`. Do not call `/rollback` for cron. Keep the web native rollback and
cron baseline-rebuild procedures separate.

1. Use the **previously captured approved baseline SHA**, never infer a recovery
   revision from a moving branch or whichever deployment is currently live.
2. Publish a unique temporary recovery branch at that exact existing commit with
   no force push, reset, or removal of unidentified work. Record its original
   provenance and verify its remote tip immediately before submission.
3. Persist a private recovery journal outside the checkout. Mark submission
   attempted **before** the POST. If the response is lost, inspect recorded
   provider history with GET; do not resubmit or start another job. The reusable
   `cron_recovery.recover` primitive enforces this through `RecoveryJournal`.
4. Capture original source branch and a fingerprint of schedule, commands,
   autodeploy, effective environment, environment-group and secret-file
   configuration. Change **only** the existing cron service's source branch to
   the temporary baseline branch, with autodeploy remaining off.
5. Recheck the pinned tip, request exactly one build of that existing cron service,
   and require returned commit == baseline SHA. Do not supply unsupported cron
   `commitId`, create a second cron service, call a run endpoint, cancel an active
   run, or change cadence/credentials. Existing in-progress runs are left alone.
6. Poll that exact recovery deploy ID to live, accepting only known progress
   states and stopping on failure, unexpected SHA, or deadline. Accepted/created
   is not success. Restore the original branch setting even if submission fails,
   compare the complete protected configuration, and retain ambiguous evidence.
7. Verify execution after activation, not merely deployment acceptance. For a
   baseline with the proof logger, require its actual revision/digest/resource/role
   and a successful natural execution. For legacy `8d227f8`, which has no logger,
   explicitly report the narrower provider evidence: sole active restored artifact
   at the approved SHA, no intervening/later deployment, and a matched successful
   natural run (same opaque `cronJobRunId`, start and end after activation). Never
   claim a runtime source digest for that legacy image. `verify_legacy_restoration`
   validates these captured event relationships.
8. Only then report recovery verified and remove the known temporary branch after
   confirming the service no longer references it. Do not clear a journal or
   delete evidence merely to allow a repeated submission. If natural execution
   has not occurred, report WAIT rather than successful recovery.

This method was actually used to restore cron to `8d227f8` under the previous
recovery authorization. The current task tests it with simulated mutations and
fresh read-only evidence only; it does not repeat that production operation.

Render documents its [single-run guarantee](https://render.com/docs/cronjobs):
updates affect future runs and a manual trigger can cancel the active run. This
procedure preserves one existing service and never manually triggers execution.

## Baseline application issue found during fresh recovery verification

Authenticated overview, Purchasing V3 and inventory-count history returned 200.
The legacy PO list returned 500 and ordering intelligence timed out before logging
the same `NameError: settings is not defined`. Both templates invoke the shared
`purchasing_v3_enabled` helper in `app/main.py`; its settings name lacked a module
binding in both restored `8d227f8` and candidate `e95f3fe`. A single local
`from app.config import settings` restores that binding. A Jinja invocation test
uses the actual application's registered helper with both feature-flag values.
No purchasing formulas, records, routes or production configuration are changed.
The production issue remains until separately authorized deployment; report it
as an actual baseline application error, not an infrastructure verifier failure.
