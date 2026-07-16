# V2 feature exposure and cutover contract

## Mechanism

`app/v2/feature_exposure.py` is a deliberately small exposure gate for V2 business modules. All unfinished feature keys are disabled by default.

- `V2_ENABLED_FEATURES`: comma-separated globally exposed keys.
- `V2_PRINCIPAL_FEATURES`: comma-separated `<principal_id>:<feature_key>` tester entries.
- `require_v2_feature(<key>)`: route dependency that returns 404 when not exposed.

Exposure is not authorization. A business route must depend independently on authentication, capability/action authorization, and store scope. Principal exposure uses the individual authenticated account and grants no role/capability.

No existing V1 navigation or V2 placeholder route uses this gate. Milestone 4 Exchanges & Returns is the first consumer through `exchanges_returns_v2`; the key remains disabled by default.

## Lifecycle

1. **Local development:** key enabled globally only in local config, with V1 route untouched.
2. **Limited testers:** principal entries for named individual employee tester accounts; authorization still enforced.
3. **Staged default:** explicit environment-level key after parity/cutover approval; V1 remains linked and operational during observation.
4. **V1 redirect/cutover:** handled by the module cutover plan only after read/write ownership, active drafts, exports, audit, and rollback pass.
5. **Rollback:** disable exposure first; route users and in-progress record IDs to their owning version; stop V2 writes before restoring V1 single-writer ownership.

Configuration changes do not require source edits. Invalid principal entries are ignored, but deployment validation should reject configuration mistakes before staged use. A database-backed flag system is intentionally deferred until scale/audit needs justify it.
