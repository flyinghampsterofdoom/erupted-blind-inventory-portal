# Approved navigation deployment — October 21, 2026

The owner authorized deployment of `302120f`, subject to preservation, regression,
and rollback gates. Production was already at `8073b0f`; the approved commit was
cherry-picked onto that revision as **`771a289d0df3ba58530a2e9408eaeb5e39f155ba`**.
Only its ten files differ from the previous live revision. The release contains
PDF visibility and inventory-category navigation, their tests, and the existing
local acceptance record. No workflow batch-saving changes are deployed.

Production web is verified live at `771a289` (Render deploy
`dep-dbcq0g5g1s2s73e4vut0`). GitHub main points to that revision. Cron remains on
`8073b0f`, with its schedule and configuration unchanged. Environment values,
auto-deployment settings, maintenance state, dependencies and schema 47 were
unchanged. No migration, purchasing mutation, PO creation/submission, or inventory
write was performed for deployment testing.

## Gates and evidence

Evidence: `/Users/justinrawlinson/Developer/Erupted/output/ordering-navigation-deployment-20261021/`.

- Fresh production dump captured with an exported read-only transaction snapshot.
  Local restoration matched all **198 tables / 731,971 rows**. The backup remains
  private; the dump SHA-256 is
  `73e28c4e45f1d3171760fd2ecf3e1ac140cfd11e88d673f417e0ab6dcb8c326a`.
- Regression run: 209 passed, 10 skipped. The nine skipped scheduling integration
  checks were subsequently run with the disposable PostgreSQL configuration:
  **9 passed**. The remaining skip requires hosted-test R2 credentials.
- JavaScript table and syntax checks passed; clean Git workspace and release
  manifest verified. Shared build/history-job preflight made zero database
  connections, writes, or Square requests.
- Post-deployment read-only comparison matched all **64 checked business tables**,
  including orders, quantities, allocations, supplier configuration, purchasing
  session history, accounting, inventory metadata, permissions, and PDF references.
- Hosted read-only smoke checks verified runtime identity, category totals,
  bounded pages, and category filtering for two suppliers in each of saved
  sessions 3 and 4. All four category reads succeeded.
- Actual production HTTP JavaScript/CSS bytes match the approved release hashes.
- Rollback revision `8073b0f` and its prior deployment ID are pinned in
  `rollback-ready.json` and in the remote rollback release branch. `release.py
  rollback` deploys that exact application revision. Schema/data rollback is
  unnecessary for this navigation release.

## Existing PDF-storage issue discovered during read-only smoke testing

The 20 recent purchase orders inspected with saved PDF references returned
**Original PDF Unavailable** because the corresponding storage keys are missing.
Their references are identical to the verified pre-deployment backup. The PDF
renderer, retrieval and storage implementation are unchanged by this release.
This is a pre-existing storage-availability problem, separate from restoring
visible navigation to the existing document endpoint. No missing original was
regenerated or overwritten. Local synthetic generation, byte-preserving export,
and native printing had passed before deployment; a valid recent production
original could not be verified in this sample.

Do not interpret the UI release as restoration of those missing stored files.
Any recovery should restore verified originals from backup in a separate scoped
operation, preserving the established PDF preservation boundary.
