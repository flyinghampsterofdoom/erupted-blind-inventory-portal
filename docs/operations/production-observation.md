# Production observation qualification (Phase 3.5)

This is an observation contract, not deployment authorization. No workflow can
activate it, deploy, change Render, migrate, or obtain business-service access.

## Why existing capabilities are insufficient

Normal authenticated GETs call `load_session_from_token(renew=True)` and commit
`web_sessions.last_seen_at/expires_at` in middleware. `/session-status` avoids
renewal but does not prove database/schema/source or functional authorization.
The existing Ed25519 forecast telemetry GET bypasses sessions but only measures
process resources. Its credential must never acquire additional authority.
Owner `/management/deployment/readiness` already performs database reads but
still traverses renewing session middleware. It was not in deployed `6b81c95`.
Startup schema checks and startup cron identity logs are historical evidence,
not fresh connectivity or proof of successful operational work. Generation
records cover generated work, but not successful no-op or failed cron ticks.

## Smallest local extension

`GET /management/deployment/observation` extends the existing deployment router.
It is 404 unless `DEPLOYMENT_OBSERVATION_PUBLIC_KEY` is explicitly configured.
No production key or identity is created by this implementation.

The separate Ed25519 signature binds purpose `erupted-readiness-observation-v1`,
method GET, the exact path, integer timestamp (30-second skew limit), and a
32-character lowercase hex nonce. Headers are `X-Observation-Timestamp`,
`X-Observation-Nonce`, and `X-Observation-Signature` (base64). The canonical JSON
body is defined by `deployment_observation_service.signing_body`. Query arguments
are rejected. This key cannot authenticate browser/business requests or select
accounts, SQL, actions, or endpoints. Browser cookies are ignored on this exact
path for all methods; unsupported methods remain 405. No business principal is
installed in request state. Original route authorization is unchanged.

Only one observation runs per process at a time. Each opens a fresh connection
to the existing database, with PostgreSQL `default_transaction_read_only=on`,
transaction read-only mode, 2-second connect timeout, 750ms statement timeout,
250ms lock timeout, and a four-second query-issuance deadline. A final in-flight
statement can extend that deadline by at most its statement timeout. DNS/kernel
transport behavior and source-file hashing are outside the SQL deadline.
`SELECT 1` and at most two `alembic_version` rows establish fresh readiness.
The actual deployed source's `SUPPORTED_REVISIONS` is authoritative. A mismatched
revision prevents authorization sampling. Every path rolls back/closes; no new
schema, migration, persistent session, or data write is used. PostgreSQL rejects
writes even if an observation dependency accidentally attempts one.

Fixed samples use the configured purchasing Owner and the first active Lead and
fixed-store employee accounts, through existing purchasing, ordering, management,
scheduling-board and store-capability dependencies. No usernames, IDs, store
names, business objects, or exception text are returned. The actual store-scope
resolver verifies Lead selection/unauthorized-store denial and employee assigned
scope locking. Role/principal permission overrides remain effective: an unexpected
privilege grant fails the policy expectation. Missing samples are INCONCLUSIVE.
This is sampled policy execution, **not browser login, full business-page, all-user,
or live schedule-derived inventory working-context acceptance**. Those behaviors
remain covered by isolated existing HTTP/inventory regression suites; production
acceptance must not claim them from this probe alone. No production actor is
created just to complete a sample. Owner/Lead management and purchasing capability
expectations are explicit; an intentional custom policy must be reviewed rather
than silently accepted as a healthy default.

Responses contain source status/digest/revision, bounded database results,
aggregate policy booleans, nonce, timestamp, duration, and application SQL count.
Schema details are signature-protected, never public; errors are reduced to safe
codes. The underlying application DB role is unchanged; transaction read-only
mode limits this fixed code path, not the privileges of unrelated application
connections. Never put a production DB URL or signing key in GitHub Actions.

Rotate/revoke through a separately authorized production configuration change:
replace the public key, or clear it to disable the endpoint. The old signature
then fails immediately; there is no multi-key overlap or persistent identity.
Keep the private key on the authorized operator's machine, outside this repository
and CI. A signed request can be replayed within the 30-second window, but only
repeats the same bounded read. The caller must match the nonce and reject stale
responses (120-second bound), redirects, oversized bodies, and unexpected sources.
Transport must be verified HTTPS with no cookies or redirect following.

## Cron completion evidence

The existing cron runner emits `ERUPTED_CRON_EXECUTION_V1` JSON with an opaque run
ID, source digest/revision, service identity, schema, timestamp and duration.
`started` alone never proves execution. `committed` is emitted only after the
existing business transaction commits. Its work category distinguishes generated
work, a legitimately complete horizon (`no-work-due`), blocked work and an inactive
policy. Only generated/no-work-due can pass expected work. Disabled and failed
receipts never pass. Existing locking, scheduling, actor attribution, transaction,
and execution gates remain intact. No second scheduler or heartbeat table exists.

Receipts are retained in existing Render logs, alongside durable scheduling work
records. Archive sanitized receipts/provider metadata with release evidence while
logs are retained. Exact provider retention was not changed or assumed. A lost
terminal log, crash after commit, log expiration or partial provider log page is
INCONCLUSIVE, never proof of failed business work or permission to rerun a job.
A failed receipt reports invocation failure; it does not prove rollback after an
ambiguous commit. Do not manually trigger production cron to collect evidence.

## Phase 3 integration

`verification.production_readiness` returns independent application, database,
authorization, web/cron compatibility, cron execution and overall results.
`scripts.release.phase3.observations` evaluates sanitized evidence offline:

```
python -m scripts.release.phase3.observations --evidence evidence.json --output qualification.json
```

The evidence object contains `observed_at`, `expected` (exact deployed revision,
source-tree digest, schema), `nonce`, `http` (status/body), `cron` and `receipts`.
Cron fields are `enabled`, `schedule`, `service_id`, `revision`, `deployed_at`,
and `last_success`. Collection must authenticate provider/source provenance;
the offline evaluator cannot prove provenance of arbitrary edited JSON files.
Archive evidence and its digest under the release's immutable evidence journal.
The CLI refuses to overwrite results and exits 2 unless every required gate passes.
It has no credentials, transport or production mutation capability.

Cron configuration, runtime source/schema, recent execution, and expected work
are separate gates. A terminal receipt must match the service, deployment window,
exact source, schema, and provider successful-run time (within five minutes).
Freshness follows the configured Render UTC schedule with an explicit ten-minute
scheduler grace, not a hardcoded hourly assumption. Only reviewed hourly and weekly
simple schedules are supported; others are INCONCLUSIVE until reviewed. A new
weekly deployment before its first due time is NOT YET OBSERVABLE. Missing evidence
after a due time is INCONCLUSIVE. Neither declares the application unhealthy.
Configuration/revision/observed execution failures remain FAIL. Overall PASS
requires all independent gates; missing observations never become a successful
release. No coordinator rollback/maintenance authority is inferred from this
report, and no existing release gate is relaxed.

## Activation prerequisites (separate authorization required)

1. Review and integrate the exact CI-validated source through protected main.
2. Approve a controlled web/cron deployment of a sealed immutable candidate,
   preserving disabled auto-deployment and the current weekly schedule. No new
   migration is introduced by Phase 3.5; compare the actual DB schema first.
3. Approve installing only the separate observation public key on web. Do not
   create a privileged user or reuse/widen the existing telemetry credential.
4. From the operator's machine, collect a fresh signed, cookie-free observation
   and read-only Render source/config metadata. Record time, duration and counts.
   Reject network/credential/unknown evidence without declaring application failure.
5. Wait for the next **natural** weekly cron execution. Correlate the committed
   receipt with provider success and archive it; inspect existing durable work
   evidence only through an explicitly non-mutating authorized path if needed.
6. Keep sampled policy health separate from full functional production acceptance.
   If existing-role samples are unavailable or custom policy differs, review the
   gap; do not create actors or weaken permissions to force a PASS.

Until those observations exist, production readiness is not fully qualified.
Phase 4 and deployment automation remain out of scope.
