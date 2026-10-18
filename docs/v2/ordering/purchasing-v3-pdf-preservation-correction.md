# Purchasing V3 PDF preservation correction

2026-10-18. **Local implementation only. No deployment or production configuration/data changes.**

The 50 original production PO references are unchanged. Missing originals remain documented exceptions, not backed-up files and not regenerated replacements. No further search for those files was performed.

## Implemented correction

- `app/services/purchase_order_pdf_storage.py` stores new vendor PDFs in the existing private R2 infrastructure, under the existing `generated/purchase_orders/` namespace. The relative `PurchaseOrder.pdf_path` association remains compatible; no schema migration, table or historical backfill is introduced.
- New names include the PO ID and SHA-256 of the exact PDF bytes. Conditional `PutObject(IfNoneMatch='*')` prevents replacement. Duplicate requests verify the existing bytes. Upload/read-back verification finishes before the caller assigns the path. Failed storage prevents order submission from committing; an unreferenced file after a later database rollback is harmless and is not reused as another order's original.
- The existing Submit action still publishes the vendor PDF. The ReportLab page layout, columns, totals, pack counts, terms and rendering code are unchanged. A row lock refreshed from the database serializes concurrent submission, including callers holding stale ORM objects.
- Downloads perform no generation, flush, path replacement or commit. Missing/corrupt originals return HTTP 404 with **Original PDF Unavailable**. Download storage access failures return HTTP 503 rather than silently serving a cached substitute. Changes to PO update timestamps do not replace an original.
- New files undergo full-byte hash verification and basic PDF structure checks on retrieval. Legacy files have no original checksum; obvious structural corruption is rejected, but authenticity/arbitrary historical byte changes cannot be proven without a trusted original hash. No hash is invented for the 50 absent files.
- The existing generated directory is retained as a disposable production cache. Each new-document read verifies the shared R2 original. The production writer never falls back to an ephemeral filesystem. Explicit filesystem storage is restricted to development/test/local with an absolute configured directory.
- A recovered legacy original can be restored at its exact existing R2 key or original local path. Retrieval needs no PO/database rewrite. Recovery must use verified original bytes, never newly rendered content.

## Actual hosting configuration

Read-only Render API and bounded PostgreSQL READ ONLY checks are recorded in `output/purchasing-pdf-correction/hosting-readonly.json`.

| Check | Result |
|---|---|
| Web and cron revision | Both remain `529df6f566b780e2e8ab5d470c160fb481d06715` |
| Production schema | Still `20261016_0037` |
| Historical references | PASS — all 50 exact ID/path pairs match the pre-correction manifest |
| Persistent disks | Neither service has one |
| Web R2 configuration | Existing endpoint, bucket, access key, secret and region are present; bounded PO-prefix listing succeeded, with zero PO objects |
| Cron R2 configuration | **Missing all five R2 settings**; configure the same private bucket/credentials during the future authorized cutover |
| Production R2 uploads/restores | Not attempted; no isolated hosted credentials were provided |

A Render disk cannot meet the shared web/cron requirement: disks belong to one service instance and cannot attach to cron. Reusing the existing R2 infrastructure avoids adding a new storage service. See [Render disk limitations](https://render.com/docs/disks#disk-limitations-and-considerations). Cloudflare documents atomic conditional PUT support; see [R2 S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).

Configure both services with identical `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_REGION=auto`, and `PURCHASE_ORDER_PDF_STORAGE=r2` (also the code default). Do not change the existing media bucket accidentally. Keep the bucket private and ensure lifecycle rules do not expire `generated/purchase_orders/`. Bucket administration and lifecycle verification remain deployment prerequisites; an S3 listing alone does not verify those controls. Application code never makes PDFs public or generates public links.

`PURCHASE_ORDER_PDF_CACHE_DIRECTORY` is optional and disposable. The default remains the existing generated directory. `PURCHASE_ORDER_PDF_LOCAL_DIRECTORY` is for isolated filesystem fixtures only. See `.env.example`. No production environment variable was changed.

## Independent backup and restoration

`scripts/purchase_order_pdf_backup.py` backs up files independently of SQL:

```sh
python -m scripts.purchase_order_pdf_backup backup exact-references.json documents.zip
python -m scripts.purchase_order_pdf_backup verify documents.zip
python -m scripts.purchase_order_pdf_backup restore documents.zip /new/isolated/restore-directory
```

The input is a read-only export of exact PDF path strings. The tool does not connect to a database, upload to R2, regenerate documents, or repair references. Archives are created exclusively with mode 0600 and contain a per-file size/hash manifest. Missing/corrupt files are explicitly marked unavailable and `original_file_backed_up=false`. Storage access failures fail the backup rather than being misreported as absent originals. Verification checks archive members, PDF integrity, sizes and hashes. Restore validates everything first and requires a new destination; it cannot overlay originals.

Retained synthetic evidence: `output/purchasing-pdf-correction/independent-backup/documents.zip`, its independent `restored-volume/`, and `evidence.json`. Original and restored SHA-256: `77cf30cf6c020bc80757ccc7d2e465d7929963ffab51627fcf3df34b677a7f45`. Retrieval from a replacement checkout using the restored path matched every byte. A missing synthetic original was reported as not backed up. Separate tests reject damaged archives and existing restore targets.

A real custom PostgreSQL dump/restore of a disposable synthetic database also preserves the PDF reference exactly; the dump contains no PDF bytes. The file archive and database dump are separate recovery artifacts.

Before production use, retain encrypted file archives outside the application/bucket failure domain alongside independently verified SQL backups. Establish a backup owner, retention and cadence; verify an immediate backup of the first separately authorized new PO PDF. Neither local evidence nor a database export claims to back up the 50 missing originals. No backup automation was activated.

## Verification

**Final relevant regression run: 852 passed, 1 skipped, 7 subtests passed; no failures.** The skip is the existing private-signage R2 test. A separate final storage run passed all 17 local preservation tests and skipped the new isolated hosted R2 probe. These runs overlap; do not add their pass counts. The focused purchasing/PDF/receiving rerun passed 66 tests.

Final counts and source revision are recorded in `output/purchasing-pdf-correction/completion.json`; test XML/logs are in the same directory.

| Scenario | Evidence |
|---|---|
| New PDF stored and retrievable | Actual Submit route, renderer, local HTTP S3 fixture and read-back checks |
| Restart / replacement deployment / cron | Four independent Python processes with separate cache roots retrieve identical bytes from the same file-backed mocked S3 endpoint |
| Duplicate / concurrent publication | Conditional PUT retries and concurrent calls; stale-session concurrent PO submission yields exactly one original |
| Missing historical PDF | Actual authorized GET returns explicit 404; generation and commit sentinels remain untouched; PO row unchanged |
| Original recovered later | Same legacy reference retrieves restored synthetic original without database rewrite |
| Corruption | Truncated legacy PDF, changed modern bytes, corrupt remote object despite good cache, damaged backup rejected |
| Storage unavailable | Explicit 503; no stale-cache fallback or committed new reference |
| File backup restore | Independent archive, manifest/hash verification, isolated restore and subsequent retrieval |
| Database backup | Real synthetic PostgreSQL custom dump/restore retains association; no files claimed in SQL backup |
| Purchasing / receiving / payments / Square | Relevant regression suite, including three shipments, receiving history and original PDF preservation |
| Vendor format | Existing default/template PDF assertions, 500-SKU/250-line vendor PDF, and visual inspection of rendered default/template samples |

The new opt-in `test_real_isolated_private_r2_pdf_preservation` requires `RUN_REAL_PO_PDF_R2_TESTS=1` and dedicated `PO_PDF_TEST_R2_ENDPOINT_URL`, `PO_PDF_TEST_R2_BUCKET_NAME`, `PO_PDF_TEST_R2_ACCESS_KEY_ID`, `PO_PDF_TEST_R2_SECRET_ACCESS_KEY`. It refuses the application's configured bucket, uses synthetic bytes, and checks conditional retries, separate cache roots, retrieval and independent backup/restore. It is **UNVERIFIED** until isolated credentials are supplied. The existing private-signage R2 integration test is also explicitly skipped. Neither is a pass. Mocked S3 requests use the real boto3 client but do not establish Cloudflare IAM, hosted durability or production permissions.

Two old tests expected a draft PDF download to generate a document; they now invoke the existing Submit action first. The production PDF link already appears only after a path exists. Three-shipment fixture setup now explicitly creates its synthetic original. The local PostgreSQL backup rehearsal uses matching PostgreSQL 16 tools; the installed default 17 restore client was incompatible with the local 16 server. No production backup format or scheduling code was changed.

## Return to the eight-step deployment plan

The original approved `84747d0b…` is superseded by the bounded local correction; the new candidate needs owner review. Resume the existing eight-step plan only after fresh deployment authorization, with these additions:

1. **Backup gate:** refresh/restore-test the production PostgreSQL 18 dump. Keep the exact 50-reference missing-file manifest. Independently back up any present PDF objects and verify restoration; never label absent originals as backed up. Obtain isolated hosted R2 verification and confirm private access, permissions and retention before declaring readiness.
2. **Migration:** preserve the reviewed forward `0037 → … → 0038` path and all existing migrations. This PDF correction adds no migration or historical rewrite.
3. **Web:** deploy only the newly reviewed SHA, with production R2 configuration. Keep purchasing features off until initial read-only compatibility smoke checks pass. Set confirmed owner principal 6 as reviewed.
4. **Cron:** deploy the same SHA and supply matching R2 settings. Preserve the existing scheduling command/cadence and maintenance gates. Do not run cron mutations as a storage smoke test.
5. **Activation:** after read-only compatibility/storage checks, activate only previously approved features. Chase Visa remains account 2/Card 1, Card 2 stays absent and Monday recording blocked. Preserve Needs Attention and incomplete-valuation restrictions.
6. **Smoke:** authenticate and perform read-only checks. Missing originals must return the explicit response without PO/reference changes. No real PO creation/submission, payment, receiving, PDF regeneration or Square inventory writes.
7. **Controlled operation:** obtain separate authorization for any real PO/payment/inventory operation; then verify its new PDF retrieval and independent file backup immediately.
8. **Recovery:** retain R2 objects and SQL associations across rollback. Never delete/rebuild originals, overwrite a newer archive, or restore an old whole backup over legitimate later writes. Verify recovered bytes in isolation first. Retain the schema-compatible financial/receiving authority and existing stop conditions; do not roll back to the automatic-regeneration behavior as a preservation workaround.

**Local correction is implemented. Production deployment is not yet ready:** cron storage configuration, isolated hosted R2 verification and independent production file-backup arrangements remain prerequisites. No additional purchasing features or accounting changes are needed. Stop for owner review; do not deploy.
