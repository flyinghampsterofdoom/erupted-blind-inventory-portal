# Product Explorer production rollout — fixture resource cleanup

The owner approved the combined production rollout and consolidated compatibility package. Earlier public references and failure receipts remain immutable. No production migration, import, deployment or cron update has occurred.

Application, schema migration, frontend and private curated inputs remain unchanged. The staff release includes durable PostgreSQL storage, real permissions, read-only Square observations, E-liquid exclusions, Compatibility and manufacturer-first photos with own-photo fallback, replacement/removal and retained history.

Prior qualification includes197 initial Python cases/eight frontend suites,107 navigation/CI/historical-migration checks,25 release-tool/native restore checks and the request-fixture regressions. Multiple fresh full production snapshots restored248 matching tables and applied0058 while preserving every row in246 existing business tables;37 new tables and49 guards passed as a non-superuser. Owned snapshots and clusters were removed after stop.

The complete production permission-map handoff corrected the HTTP timing failures. The fifth candidate then had one direct-service timing failure in each of two hosted runs, on different cases, while641 cases passed. SQL accounted for175–227ms of2061–2083ms totals. Local unchanged-source service measurements passed311–362ms. These observations motivated fixture resource isolation and additional CPU/garbage-collection metrics; they do not establish the full hosted root cause by themselves.

The scheduling database fixture now retires its own compiled caches and reclaims disposable migration metadata before yielding and after teardown. Garbage collection remains enabled during measured service calls and its pauses remain in the original wall-clock interval. No warm-up operation, time subtraction, skip, threshold change, application optimization or resource-limit relaxation is introduced. The service test records CPU time and bounded per-generation collection totals. Every original timing and data-integrity assertion is AST-identical, with an additional GC-enabled assertion.

Twenty-one native checks passed under the original shared lock and limits:17 access/HTTP cases and4 service cases. Service measurements were300–358ms against the unchanged1000ms budget; the1500ms HTTP and800-statement limits remain intact. Fresh role/principal precedence and exactly two initial permission-override reads per request remain tested. Local instrumentation observed disposable fixture objects being reclaimed and continued collections inside measured calls.

Only tests, this report and fixed release-tool output/reference pins changed. Original recovery/cron artifacts are reused only after exact validation and already have full hosted CI PASS. Full CI for the exact new candidate remains required before production actions.

Six canonical manufacturer images (421,564 bytes across21 variations),28 current owner exclusions and the private retailer association seed remain in the original encrypted bundle, which is not duplicated. The public seed contains manufacturer facts/references without private retailer associations or SKU identifiers.

The [consolidated compatibility package](production-0058-consolidated-amendment.md) preserves database/owner/permission checks, scheduling code, commands, settings and cadence. The cron compatibility build triggers no manual run; the daily-evidence job needs no update. No new persistent backup destination is created.

Remaining gates: exact-artifact full CI; fresh final restore/preflight proof; compatible web bridge and cron build with original branch restored; fresh locked admission; atomic0058 migration and curated import; final Explorer deployment; real-owner readiness, served media/source hashes and business/control preservation. This frozen report does not claim those production gates have passed.
