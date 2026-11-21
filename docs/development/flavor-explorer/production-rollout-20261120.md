# Product Explorer production rollout — complete request-fixture alignment

The owner approved the combined production rollout and consolidated compatibility package. All earlier public references and private failure receipts remain immutable. No production migration, import, deployment or cron update has occurred.

The staff release includes durable PostgreSQL storage, real permissions, read-only Square observations, E-liquid exclusions, Compatibility and manufacturer-first photos with own-photo fallback, replacement/removal and retained history. Application, migration and frontend bytes remain unchanged from the reviewed earlier candidates.

Initial qualification covered197 unique Python cases and eight frontend suites, followed by107 incremental navigation/CI/historical-migration checks and25 release-tool/native restore checks. The third and fourth candidates each passed fresh full-snapshot restore and migration rehearsals:248 restored tables matched,246 existing business tables retained all rows, and the37-table/49-guard0058 schema passed as a non-superuser. Owned snapshots and clusters were removed after stop.

Hosted CI isolated repeated published-manager HTTP timing failures while the functional tests passed. The fourth candidate aligned the fixture's batched flag resolver but missed production's request-to-database permission-cache handoff. The final fixture now uses both parts of the production path. A real page-request regression verifies exactly two fresh permission-override reads per request and fresh role/principal allow-deny precedence across changes. All17 relevant native tests, including the unchanged1500ms HTTP assertions, passed. The existing1000ms service assertion, application access rules, shared lock, heap/nursery, concurrency, RSS and time limits are unchanged.

The final candidate uses a new immutable reference/output directory. Its release-tool changes are only fixed pins; the reviewed namespace-safe single-transaction restore and10,000-row fixed-width SHA256 streaming remain unchanged. Original recovery and cron artifacts are reused only after exact validation and already have full hosted CI PASS. Final exact-artifact CI remains required before production actions.

Six canonical manufacturer images (421,564 bytes across21 variations),28 current owner exclusions and the complete private retailer association seed remain in the original encrypted release bundle. The public seed contains manufacturer facts/references without retailer associations or private SKU identifiers. The private bundle is not duplicated.

The [consolidated compatibility package](production-0058-consolidated-amendment.md) preserves database/owner/permission checks, scheduling business code, commands, settings and cadence. The scheduled-job compatibility build triggers no manual run; the daily-evidence job needs no update. No new persistent backup destination is created.

Remaining gates: final exact-artifact full CI; current final restore/preflight proof; compatible web bridge and cron build with original branch restored; fresh locked admission; atomic0058 migration and curated import; final Explorer deployment; real-owner readiness, served source/media hashes and business/control preservation. Normal owner access is maintained through protected requests. This frozen report does not claim those production gates have passed.
