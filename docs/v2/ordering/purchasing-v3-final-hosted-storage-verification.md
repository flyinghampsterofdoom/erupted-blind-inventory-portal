# Purchasing V3 — final hosted storage verification

2026-10-18. **Both hosted R2 checks passed. Production configuration gates remain. Do not deploy or activate yet.**

## Results

| Required check | Result / evidence |
|---|---|
| Private object creation and retrieval | PASS at the configured R2 S3 endpoint. Signed writes/reads returned 200; unsigned reads were rejected with HTTP 400, `InvalidArgument: Authorization`. Existing display-session isolation also passed. This is not proof that separate public bucket endpoints are disabled; see the configuration gate below. |
| Immutable writes | PASS. Duplicate conditional writes and a different-body replacement attempt returned 412 `PreconditionFailed`. The first original remained byte-identical. |
| Checksum validation | PASS. A separate deliberately incorrect test object failed the production reader with `checksum mismatch`, despite containing a structurally valid PDF. |
| Independent backup / restoration | PASS. Actual hosted bytes were archived independently, verified, restored locally, uploaded from that restored copy to a second test key and retrieved byte-identically. The damaged test object was explicitly excluded as unavailable. |
| Separate web/cron process access | PASS using proposed matching settings in two independent local Python processes, each reading the hosted test PDF with a separate cache. These were isolated processes, not production deployments. |
| Actual production web/cron configuration | NOT MATCHING. Web has all existing R2 settings; cron has none. A secret-reference-only configuration plan is prepared. No production settings were changed. |
| No existing production-object interaction | PASS. Every object GET/PUT in both checks was confined to the newly generated verification namespaces. Initial LIST operations were limited to each empty test namespace. No live PO or existing media object was read, replaced or deleted. |
| Historical preservation | PASS. All 50 exact production PO ID/path pairs matched before and after. Only bounded PostgreSQL READ ONLY queries were used. |
| Combined release compatibility | PASS. All 425 compared application, migration, dependency and release-script files match the owner-approved PDF revision byte-for-byte. Approved Purchasing V3 ancestry is retained. No application changes, new migrations or dependency changes were needed. |

The two formerly skipped cases ran successfully:

- `test_real_isolated_private_r2_pdf_preservation`
- `test_real_r2_private_delivery_and_authentication_separation`

The hosted/local storage and signage run finished **34 passed, zero failures, zero skips**. A separate release-entrypoint/schema-contract/local-migration run finished **80 passed, zero failures, zero skips**. The preceding relevant Purchasing regression remains **852 passed, 7 subtests passed**, with its formerly skipped R2 check now exercised here. Runs overlap; do not add counts as unique coverage. Four previously accepted scheduling fixture failures remain outside this correction; scheduling source is unchanged.

The first hosted invocation expected exactly 403 for unsigned S3 reads and therefore failed two assertions. Diagnosis established R2's exact response was HTTP 400 with error code `InvalidArgument` and message `Authorization`. The final test accepts only that precise authorization rejection or 403 `AccessDenied`; unrelated 400 errors do not pass. No application code was changed for this provider behavior.

## Isolation and retained evidence

Successful run namespace:

`verification/purchasing-v3/16fbdebbe8de4f2986ec65a5c9fd946a/`

Each test used its own empty child namespace. The PDF helper translates synthetic logical names into actual keys beneath `/pdf/`; signage uses `/signage/`. No provider request uses live `generated/purchase_orders/` or `digital-signage/` object paths. Application key validation, hashing, conditional PUT and read-back code remain unchanged. The mapping exists only in tests.

Four objects from the successful run and two from the earlier authorization-status assertion run are retained, clearly identified under their separate verification roots. No delete operation was used. They contain synthetic test data and have no production database association. The earlier root is recorded in `initial-run-prefix.json`; no extra object search was performed.

Evidence in `output/purchasing-hosted-storage/`:

- `hosted-and-local.xml`, `run.log`, `release-contracts.xml/log`: final tests.
- `pdf.json`, `signage.json`, `request-audit-*.json`: actual object keys, operations, HTTP statuses and checks. Credentials and signed headers are excluded.
- `independent-pdf-backup.zip` and `independent-pdf-restore/`: independent file archive and restored original. SHA-256: `aaa3a2c1659703a43b0c2c8aadd3e93f538e63312a779d473afdee73274452a1`.
- `web-cron-proposed-runtime.json`: independent-process retrieval evidence using the proposed common configuration.
- `before.json`, `after.json`: unchanged production service revisions/configuration presence and the unchanged 50-reference hash `ee0dc44760878acf2ae2888f32b0c7d2a86c04bd82b324f2ba81b525b92b21be`.
- `release-compatibility.json`, `completion.json`: source comparison, final combined revision and readiness disposition.
- `deployment-storage-plan.json`: exact setting names and references to existing secrets, without secret values.

Production remains web/cron revision `529df6f566b780e2e8ab5d470c160fb481d06715`, database schema `20261016_0037`. No production purchasing/payment/database records, schedules, feature flags or Square data were changed. The only hosted writes were the authorized isolated test objects.

## Final combined release

The release descends from approved Purchasing V3 `84747d0b2ce379610ba1833022218c678e5fd0a0` and approved PDF preservation `2292ba2651ba2708af6572ec65f12a71cb8b258c`. The final commit is recorded in `completion.json` after committing this report and the bounded test changes. Nothing was pushed or deployed.

This preparation changes only tests and documentation. The application/deployment tree is identical to the approved PDF implementation. The same migration path remains:

`20261016_0037 → 20261017_0032 → 20261017_0033 → 20261017_0034 → 20261017_0035 → 20261018_0038`

Existing production migrations, authentication, Square policy, scheduling and release entrypoints remain preserved. The PDF correction adds no migration.

## Exact remaining deployment prerequisites

1. **Bucket privacy/retention confirmation.** Confirm in authenticated Cloudflare settings that public development access and public custom-domain access are disabled, and no lifecycle rule expires `generated/purchase_orders/`. The available S3 credentials prove authenticated access and rejection of unsigned requests at that endpoint; they do not expose Cloudflare's separate public-domain settings. The attempted read-only dashboard check reached sign-in, so no console configuration was inspected or changed. Owner confirmation or an authenticated read-only console check remains required. Cloudflare documents independently configurable [public bucket endpoints](https://developers.cloudflare.com/r2/buckets/public-buckets/).
2. **Configure cron to match web during the authorized maintenance window.** Copy the existing web values for `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_REGION` to cron; retain the existing private bucket and unrelated environment settings. Use `PURCHASE_ORDER_PDF_STORAGE=r2` on both (also the application default). Verify actual equality in memory and retrieve only the existing test object from both deployed runtimes before activation. Do not make production PDF reads fall back to local storage. The prepared plan contains secret references only.
3. **Independent production file-backup arrangements.** The hosted backup/restore mechanism is verified. Confirm the external protected destination, custodian, cadence and retention for real PDF archives independently of SQL backups. Back up and restore-check the first separately authorized new PO PDF immediately. No production backup automation was activated and none of the 50 missing originals is claimed as backed up.
4. **Authorize the exact final revision and maintenance window.** Then execute the existing eight-step plan: fresh PostgreSQL 18 backup/restore verification and writer quiescence; reviewed forward migrations; coordinated web/cron deployment; matching storage and owner settings; read-only compatibility smoke checks before feature activation; separately authorized operational verification; preserved recovery/rollback gates. No PO creation, payment recording, receiving or Square quantity mutation belongs in smoke tests.

Keep `PURCHASING_OWNER_PRINCIPAL_ID=6`, Chase Visa/Card 1 account 2, Card 2 absent and Monday recording blocked. Preserve Needs Attention, unknown costs/attributions, incomplete valuation warnings, and the 50 missing original references. Card 2 configuration, recovery of missing historical originals and resolution of every SKU exception are not newly imposed interface-deployment gates.

**Readiness determination: hosted integration verification is complete and the combined application is compatible. Owner configuration confirmation is still required before unconditional deployment readiness. No further feature development is needed.**
