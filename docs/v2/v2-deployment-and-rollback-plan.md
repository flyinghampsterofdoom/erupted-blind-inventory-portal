# V2 deployment and rollback plan

This plan is governed by the [V1 Preservation Guarantee](./v1-preservation-guarantee.md).

Status date: 2026-07-25. Current repository schema head is `20260725_0007`. This document defines deployment invariants; use the evidence-bearing [production release checklist](./v2-production-release-checklist.md) for each release and the [canary deployment guide](./v2-canary-deployment-guide.md) for staged exposure.

## Deployment invariants

- V1 and V2 may run concurrently in the same Render application.
- This architecture assumes one Render application and existing route prefixes, not a separate V2 subdomain or second Render service.
- The application continues serving all existing V1 routes.
- V2 routes remain additive under `/v2/...`.
- Deploying V2 code does not change a module’s canonical-owner state.
- V2 business features remain disabled by default unless their module exposure is explicitly approved.
- Authentication and V1 shared-data access do not require V2.
- V1 does not depend on V2 session context, Current Store, navigation, or feature exposure.

## Schema and data

- Revisions are additive by default.
- No ordinary deployment runs production backfills, historical rewrites, deletions, destructive migrations, or V1 semantic conversions.
- A shared table retains documented V1 ownership until a module-specific data-write decision is approved.
- V2 reads do not transfer ownership.
- Dual writes and automatic cross-version record creation are prohibited by default.
- Square behavior used by V1 remains unchanged unless a separately approved module plan transfers integration ownership.

## Release procedure

1. Record the release commit, included modules, owner, canonical-owner states, and exact approved exposure.
2. Run the full available suite plus opt-in PostgreSQL integration tests; run real R2 verification when media modules are included.
3. Validate or migrate the target to exactly `20260725_0007` using the approved schema command.
4. Confirm V1 route, authentication, permission, and critical workflow smoke tests pass.
5. Start the application and verify both V1 and exposed V2 routes independently.
6. Begin with principal-scoped exposure; do not add redirects or data migration as an incidental deployment step.
7. Record evidence and module canonical-owner states; deployment alone changes none of them.

For the first controlled Render deployment, the unversioned production database must first pass the production-specific recognition procedure in [Render production V1 baseline compatibility profile](./render-production-v1-compatibility-profile.md). After the validated baseline stamp, the existing Render service uses `python -m app.schema_contract upgrade` as its normal pre-deploy command. No compatibility profile is needed to run additive migrations.

## Failure isolation

- A disabled or broken V2 module must not block V1.
- Missing V2 Current Store context affects only V2 Store Operations pages.
- V2 startup/exposure errors must not be masked by silent V1 substitution.
- V1 errors remain visible and are not masked by automatic V2 substitution.
- Users retain direct V1 URLs and original V1 navigation/access paths.
- Disabling `digital_signage_v2` or `touchscreen_v2` disables their administration surfaces but does not revoke already provisioned display/device runtimes; credential revocation is required until TD-003 and TD-004 are resolved.

## Rollback guarantee

Application rollback restores the prior application version while V1 remains operational. It must not require rebuilding V1 routes, templates, services, navigation, data, or Square configuration.

1. Disable affected V2 feature exposure when safe and necessary.
2. Stop the affected V2 writer according to its module plan.
3. Continue directing users to unchanged V1 routes.
4. Restore the prior application release.
5. Reconcile V2-created records without rewriting or deleting V1 history.
6. Use database restoration or a separately approved revision procedure only when an additive schema change itself is unsafe; destructive downgrade is not the default rollback.

V1 recovery work is a release blocker: if rollback would require reconstructing V1, the deployment plan is not acceptable.

Current environment/test evidence is summarized in [V2 test verification](./v2-test-verification.md) and [V2 release readiness](./v2-release-readiness-report.md).
