# V2 feature exposure and cutover contract

This contract is subordinate to the [V1 Preservation Guarantee](./v1-preservation-guarantee.md). Feature exposure never changes canonical ownership, redirects V1, or approves V1 retirement.

## Mechanism

`app/v2/feature_exposure.py` is a deliberately small exposure gate for V2 business modules. All unfinished feature keys are disabled by default.

- `V2_ENABLED_FEATURES`: comma-separated globally exposed keys.
- `V2_PRINCIPAL_FEATURES`: comma-separated `<principal_id>:<feature_key>` tester entries.
- `require_v2_feature(<key>)`: route dependency that returns 404 when not exposed.

Exposure is not authorization. A business route must depend independently on authentication, capability/action authorization, and store scope. Principal exposure uses the individual authenticated account and grants no role/capability.

No existing V1 navigation uses this gate. The centralized V2 navigation registry uses exposure only to reveal implemented feature-backed destinations. Milestone 4 Exchanges & Returns uses `exchanges_returns_v2`; Milestone 5 Daily Store Logs uses `daily_store_logs_v2`. The Ordering navigation bridge uses `ordering_v1_links_v2` to reveal four unchanged V1 GET destinations without adding a V2 Ordering route, data access, or Square call. All three keys remain disabled by default.

Exposure is evaluated independently for each child definition. A visible section-wide or child navigation permission cannot reveal an implemented route whose feature key is disabled. Conversely, exposure alone cannot reveal a child without its effective permission and required context.

The Ordering bridge additionally requires effective `management.admin` before its links are rendered. This is a navigation safeguard using permission flags already loaded for the request; the unchanged V1 routes independently enforce their existing authorization again.

See [V2 navigation architecture](./v2-navigation-architecture.md).

## Lifecycle

1. **Local development:** key enabled globally only in local config, with V1 route untouched.
2. **Limited testers:** principal entries for named individual employee tester accounts; authorization still enforced.
3. **Staged side by side:** explicit environment-level key after review; V1 remains canonical, directly linked, and operational.
4. **V2 canonical approval:** handled only by a module cutover record after the full cutover gate and written owner approval.
5. **Observation:** V1 remains present and directly recoverable after V2 becomes canonical.
6. **V1 retirement approval:** a separate later decision; never inferred from cutover.
7. **Rollback:** disable exposure first when safe; route users and in-progress record IDs to their owning version; preserve unchanged V1 access throughout.

Configuration changes do not require source edits. Invalid principal entries are ignored, but deployment validation should reject configuration mistakes before staged use. A database-backed flag system is intentionally deferred until scale/audit needs justify it.
