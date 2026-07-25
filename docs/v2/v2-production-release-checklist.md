# V2 production release checklist

Use this checklist for every production release containing V2 code or schema. Copy it into the release record and attach evidence; do not mark an item complete based only on repository state. The current schema head is `20260725_0007`.

## Release identity

- [ ] Release commit and branch recorded
- [ ] Release owner and operational owner recorded
- [ ] Included V2 modules and excluded modules listed
- [ ] Approved feature exposure values recorded without secrets
- [ ] Module canonical-owner states confirmed; no state is inferred from deployment

## Pre-deployment

### Repository

- [ ] `git status --short --branch` is clean and the intended branch/commit is checked out
- [ ] Release commit is present on the deployment remote
- [ ] No unreviewed business-logic, feature-flag, schema, or V1 changes are included
- [ ] Documentation, parity ledger, debt register, and readiness report match the release commit

### Migration verification

- [ ] Alembic chain is linear from `20260715_0001` through `20260725_0007`
- [ ] A fresh disposable PostgreSQL database upgrades successfully to `head`
- [ ] `alembic_version.version_num` equals `20260725_0007`
- [ ] Existing target schema is validated against the correct disposable reference
- [ ] The Render compatibility profile is used only for its documented first-baseline recognition case
- [ ] Additive migration review and backup/recovery plan are recorded
- [ ] No baseline replay, destructive downgrade, backfill, rewrite, or deletion is implicit

### Test and environment validation

- [ ] `.venv/bin/python -m pytest -q -rs` passes
- [ ] PostgreSQL integration tests pass with `TEST_POSTGRES_ADMIN_URL`
- [ ] Real R2 test passes with isolated credentials if Digital Signage or Touchscreen media is included
- [ ] `ENVIRONMENT`, cookie security, application secret, database URL, and schema check are production-safe
- [ ] Square mode and credentials match the release scope; no unapproved V2 Square write is possible
- [ ] R2 bucket, endpoint, region, credentials, CORS/network access, and limits are validated when required
- [ ] Feature values contain only approved keys and valid `<principal_id>:<feature_key>` entries
- [ ] Every canary principal exists, is active, is an individual account, and has required capabilities/store scope
- [ ] Every excluded module remains unexposed

## Deployment

- [ ] Record the pre-deployment application and database state
- [ ] Run `python -m app.schema_contract upgrade --database-url <target>` through the approved deployment mechanism
- [ ] Confirm migration completed once and target revision is `20260725_0007`
- [ ] Deploy the reviewed application commit
- [ ] Confirm service startup passes schema revision validation
- [ ] Verify login, root routing, static assets, V1 management home, and V1 store home
- [ ] Verify health/log state contains no startup, migration, storage, or permission errors
- [ ] Apply only the approved principal-scoped feature exposure for the canary

## Post-deployment

### Smoke testing

- [ ] Critical V1 authentication, store, management, and selected external-integration paths still work
- [ ] Every included V2 module returns 404 for an unexposed principal
- [ ] Every included V2 module opens for the approved principal with the required capability
- [ ] Denied capability and unauthorized store/object cases fail as documented
- [ ] One safe representative read and, when approved, one reversible or append-only action succeeds
- [ ] Device/display runtime is tested when Digital Signage or Touchscreen is included

### Owner and audit verification

- [ ] Product/owner reviewer confirms the visible workflow and result
- [ ] Canary principal identity, role, capabilities, and store scope are rechecked
- [ ] V2 audit event contains the authenticated actor, action, entity, store scope, correlation, and safe metadata
- [ ] No secrets, passwords, device tokens, or raw credentials appear in logs/audit metadata
- [ ] V1 remains directly accessible and canonical unless a separate approved cutover says otherwise

### Rollback validation

- [ ] Disabling the principal feature entry removes the management/user V2 route from that principal
- [ ] V1 fallback path is confirmed without reconstructing V1 data or routes
- [ ] Device/display credential revocation is rehearsed for modules whose runtime is not feature-gated
- [ ] Application rollback target is known and compatible with the additive schema
- [ ] V2-created records are preserved and reconciled; rollback does not delete history
- [ ] Release outcome, incidents, and decision to hold/promote/rollback are recorded

See the [canary deployment guide](./v2-canary-deployment-guide.md), [deployment and rollback plan](./v2-deployment-and-rollback-plan.md), and [release readiness report](./v2-release-readiness-report.md).
