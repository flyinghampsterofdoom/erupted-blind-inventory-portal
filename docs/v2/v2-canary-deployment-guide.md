# V2 canary deployment guide

This guide defines the owner-preview baseline for a controlled production canary. It does not approve a deployment or change canonical ownership. Begin with one implemented module and one named individual principal.

## 1. Choose and qualify the canary

1. Select one module, product owner, operational owner, observation window, and rollback contact.
2. Use an active individual principal; never use a shared store login as a V2 operational canary.
3. Verify required effective capabilities and authorized store/object scope independently from exposure.
4. Complete the module's P0 items in the [technical debt register](./v2-technical-debt-register.md).
5. Complete the [production release checklist](./v2-production-release-checklist.md) through pre-deployment.

## 2. Principal-scoped exposure

Add only the approved pair to `V2_PRINCIPAL_FEATURES`:

```text
V2_PRINCIPAL_FEATURES=<principal_id>:<feature_key>
```

Multiple approved entries are comma-separated. Do not add the key to `V2_ENABLED_FEATURES` during a single-principal canary. Supported implemented keys are:

| Key | Surface gated | Additional requirements |
|---|---|---|
| `exchanges_returns_v2` | V2 submit/history/detail | `store.access` or `management.access`; store scope |
| `daily_store_logs_v2` | Current Store landing, submit/history/detail/actions | `store.access` or `management.access`; Current Store or management scope |
| `staff_scheduling_v2` | Weekly board and scheduling APIs | `scheduling.*`; authorized store scope |
| `digital_signage_v2` | Signage administration | `digital_signage.*`; R2 for media |
| `touchscreen_v2` | Touchscreen administration | `touchscreen.*`; R2 and fresh Square cache where used |
| `ordering_v1_links_v2` | Links to unchanged V1 ordering pages | matching navigation permission and `management.admin` |
| `ordering_intelligence_v2` | Ordering Intelligence dashboard and separately authorized lifecycle management | effective `management.admin`; authorized store scope; reviewed synchronous Square-read expectations; lifecycle mutation additionally requires explicit principal `ordering.lifecycle.manage` |

Exposure does not grant permission. Unknown or malformed principal entries are ignored by runtime parsing, so validate the deployed value explicitly.

For the owner lifecycle canary, first reverify the existing owner principal and its current principal-scoped `ordering_intelligence_v2` exposure. If the pair is already present, preserve `V2_PRINCIPAL_FEATURES` exactly and add only a principal permission override allowing `ordering.lifecycle.manage`. Do not add a role override or global feature exposure. Rollback removes or denies that one principal capability while retaining lifecycle rows and audits.

## 3. Validation steps

1. As an unexposed control principal, confirm the module navigation is absent and direct authenticated V2 routes return 404.
2. As the canary principal, confirm the intended navigation appears and the module opens.
3. Confirm a principal lacking the business capability remains denied even if exposed.
4. Confirm unauthorized store IDs, records, and object IDs cannot be accessed.
5. Exercise the module's safest representative workflow and capture record IDs, timestamps, screenshots if useful, and audit correlation.
6. Confirm V1 routes remain directly accessible and unchanged.
7. Review application errors, database errors, authentication events, and V2 audit metadata.
8. For Digital Signage or Touchscreen, separately validate provisioned device behavior, media storage, cache freshness, and credential revocation.
9. For Ordering lifecycle, verify sparse Active parity before any action, then exercise explicit No Future Reorder, archive, and restore on owner-selected real products only. Confirm versions, notes, prior state, actor, UTC time, and audit correlation. Do not manufacture archive records for performance evidence.

After the owner has archived a meaningful real set, repeat the established single-store and all-store Ordering diagnostic. Record active, No Future Reorder, and archived counts; inventory-count and inventory-change submitted variation IDs; inventory-change calls/pages and returned changes; Square requests and elapsed time; total request time; recommendation count; and response bytes. Treat this as post-classification evidence, not a pre-deployment success threshold, and do not claim percentage improvement before it exists.

## 4. Success criteria

- No regression in the agreed V1 smoke paths.
- Unexposed and unauthorized principals cannot reach the V2 management/user surface.
- The canary principal sees only authorized stores and actions.
- The representative workflow persists exactly once with correct actor attribution and audit evidence.
- No unexplained application, PostgreSQL, R2, Square, authentication, or permission errors occur.
- Owner confirms the workflow and result are operationally accurate.
- Rollback steps are executable within the agreed response window.

Any unexplained data difference, scope expansion, duplicate write, missing audit event, secret exposure, V1 regression, or non-recoverable failure is a failed canary.

## 5. Rollback

1. Remove the `<principal_id>:<feature_key>` entry and redeploy/restart through the approved configuration path.
2. Confirm the V2 navigation disappears and the gated route returns 404 for that principal.
3. Direct the user to the unchanged V1 workflow when an equivalent exists.
4. Preserve and reconcile V2-created records; do not delete or rewrite history to make counts agree.
5. Revoke affected display/device credentials separately for Digital Signage or Touchscreen. Their `/display/*` and `/touchscreen/*` runtimes are not disabled by the management feature key (TD-003 and TD-004).
6. Roll back application code only if necessary and only to a revision compatible with the additive database schema.
7. Record cause, impact, audit evidence, and re-entry criteria.

## 6. Promotion

Promotion is deliberate, not automatic:

1. Complete the observation window and reconcile all records and audit events.
2. Obtain product and operational owner approval for a larger named-principal cohort.
3. Add principals incrementally and repeat control/capability/scope checks.
4. Use `V2_ENABLED_FEATURES=<feature_key>` only after the approved target population, support model, rollback, and device implications are understood.
5. Treat V2 canonical ownership and V1 retirement as separate later decisions governed by a module cutover record.
