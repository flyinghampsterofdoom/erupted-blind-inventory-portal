# V2 feature exposure and cutover contract

Status date: 2026-07-25. This contract is subordinate to the [V1 Preservation Guarantee](./v1-preservation-guarantee.md). Feature exposure never grants authorization, changes canonical ownership, redirects V1, or approves V1 retirement.

## Mechanism

`app/v2/feature_exposure.py` implements environment-backed exposure:

- `V2_ENABLED_FEATURES`: comma-separated globally exposed keys.
- `V2_PRINCIPAL_FEATURES`: comma-separated `<principal_id>:<feature_key>` entries.
- `require_v2_feature(<key>)`: authenticated route dependency that returns 404 when the key is not exposed to the current principal.

All keys are disabled when both environment values are empty. Exposure is independent from effective capability checks, store/object scope, CSRF, device credentials, and canonical ownership. Invalid principal entries are ignored by the parser; deployment validation must detect configuration mistakes.

## Implemented-key audit

| Key | Implemented surface | Independent enable | Independent disable | Dependencies and findings |
|---|---|---:|---:|---|
| `exchanges_returns_v2` | V2 submit, history, and detail | Yes | Yes | Requires `store.access` or `management.access` plus store/record scope |
| `daily_store_logs_v2` | Current Store landing, completion dashboard, submit, history, detail, actions | Yes | Yes | Requires `store.access` or `management.access`; employee workflow requires validated Current Store |
| `staff_scheduling_v2` | Weekly board and scheduling/Store Shift APIs | Yes | Yes | Requires `scheduling.*`, authorized stores, and scheduling schema |
| `digital_signage_v2` | V2 signage administration | Yes | Partially | Requires `digital_signage.*` and R2 for media. Credentialed `/display/*` player routes remain available when the admin key is disabled (TD-003). |
| `touchscreen_v2` | V2 touchscreen administration | Yes | Partially | Requires `touchscreen.*`, R2 for media, and fresh Square cache for customer results. Credentialed `/touchscreen/*` routes remain available when the admin key is disabled (TD-004). |
| `ordering_v1_links_v2` | Four navigation links to unchanged V1 Ordering pages | Yes | Yes | Requires matching navigation permission and effective `management.admin`; it is not V2 Ordering parity |
| `ordering_intelligence_v2` | GET-only V2 Ordering Intelligence dashboard | Yes | Yes | Requires effective `management.admin` and server-resolved authorized stores; uses synchronous Square reads; creates no PO and performs no write |

No undocumented dependency was found for the first three modules, the Ordering bridge, or Ordering Intelligence. The two Ordering keys are independent. Digital Signage and Touchscreen intentionally use separate device/display credential boundaries, but the absence of a full-module runtime kill switch was not previously explicit. It is recorded in the [technical debt register](./v2-technical-debt-register.md) and must be considered during release rollback.

The ungated `/v2/overview` and placeholder section pages are V2 shell/foundation routes, not independently exposed business modules. They contain no implemented replacement action.

## Navigation interaction

The central registry evaluates exposure per implemented child. A broad section or child navigation permission cannot reveal a gated route when its key is disabled. Exposure cannot reveal a child without its effective permission and required context. Every business route rechecks its own feature and authorization dependencies; navigation is not authorization.

## Lifecycle

1. **Local development:** enable only in local configuration.
2. **Named canary:** add one active individual account to `V2_PRINCIPAL_FEATURES`; authorization still applies.
3. **Larger cohort:** add reviewed principal entries after successful observation and reconciliation.
4. **Global exposure:** add the key to `V2_ENABLED_FEATURES` only after explicit rollout approval.
5. **V2 canonical approval:** use a module cutover record and written owner approval.
6. **V1 retirement:** decide separately after observation; never infer it from global exposure or canonical ownership.

Rollback removes exposure first, preserves V2-created records, and returns users to unchanged V1 routes where an equivalent exists. Digital Signage and Touchscreen also require display/device credential revocation to stop already provisioned runtimes until TD-003 and TD-004 are resolved.

See the [canary deployment guide](./v2-canary-deployment-guide.md), [release checklist](./v2-production-release-checklist.md), and [navigation architecture](./v2-navigation-architecture.md).
