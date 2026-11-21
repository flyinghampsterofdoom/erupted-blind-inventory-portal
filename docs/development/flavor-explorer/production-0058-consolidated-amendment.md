# Product Explorer: consolidated public0058 compatibility amendment

The Product Explorer release needs three existing schema consumers to accept the new additive revision. The prepared change preserves their current authorization and operational controls.

The two web compatibility edits are exactly those in [production-0058-compatibility.patch](production-0058-compatibility.patch) and its existing hash manifest. They add literal0058 acceptance in the existing Purchasing owner-inspection and collector installer checks; exact database/user/host/active-owner and restricted-role checks remain.

The retained scheduling artifact at `af00e6dee9738b22afeaaed9951d029be1b21cbc` calls `assert_supported_schema()` during every enabled natural run. Its current allowed set contains0057 only. The deployed cron is active, its schema check and execution flag are enabled, and its cadence is `0 10 * * 0`. Advancing the shared public schema to0058 without compatibility would make that future run fail.

[production-0058-cron-compatibility.patch](production-0058-cron-compatibility.patch) changes one line in an artifact derived from that exact cron revision: `SUPPORTED_REVISIONS` also accepts0058. Its head remains0057, and every scheduling business source file stays byte-identical. The exact patch SHA is `4721299359443918cc977482c1cd676e61ac5247a3a44c5e59998f27d9ddcc4d`.

The fixed plan is to publish and qualify the unique `release/product-explorer-cron-compat-20261120` branch, temporarily bind the existing staff cron to it, build/deploy once, verify the exact artifact, then restore its original source branch and autodeployoff. The existing original branch tip is newer than the live cron artifact; it is preserved without a ref update or force push. All27 cron environment values, start/build commands and weekly cadence are preserved.

No run trigger, manual automation, active-run cancellation, execution-flag change or collector-cron mutation is included. [Render's cron documentation](https://render.com/docs/cronjobs) describes new Git builds as affecting future runs and documents manual triggers separately. Its [deploy API](https://api-docs.render.com/reference/create-deploy) does not support cron `commitId`; its [service update API](https://api-docs.render.com/reference/update-service) documents the separate source-branch update and deployment steps.

The compatible web recovery artifact is activated and verified on0057 before the migration. The fixed cron artifact is then built and verified before public0058 is applied. The final Explorer activation follows the transactional migration and curated import. A successful artifact build is separate evidence from a future natural scheduled run.

The complete sanitized identities, hashes, fixed actions and excluded actions are in [production-0058-consolidated-amendment.json](production-0058-consolidated-amendment.json). Owner approval is recorded. Exact artifacts, hosted CI, restore rehearsal and live verification remain required; no production mutation has been performed.
