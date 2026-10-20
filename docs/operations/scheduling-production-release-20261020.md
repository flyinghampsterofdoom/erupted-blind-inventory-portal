# Consolidated scheduling release — 2026-10-20

## Included work

The production candidate integrates the actual commit histories for combined scheduling `0a6ca5c`, PTO `1d1d030` and its draft-preservation follow-up `ccff9ce`, and Business Hours `58297bf` (including the preceding regeneration rollback fix). The merge retains approval-content verification and business-hour labels together.

Included: employee scheduling configuration redesign; revision/comparison and assignment display; weekly single-tail maintenance and Monday pending/overdue indicators; employee-owned PTO submission/edit/cancellation and reviewed amendments; explicit week-specific vacation allocations; exact allocation and reserve limits; generation/publication validation; and Admin dated store closures/special hours.

The password-recovery changes introduced by `d87abcc` are excluded by restoring their application code, templates and tests to the preceding implementation. The additive migration 44 and nullable token model remain for migration-chain compatibility; neither activates recovery or changes accounts. Existing employee account access is retained. No email configuration, sender, permission grants or unrelated background services are activated.

An existing Lead account supports all active stores through the ordinary Lead scope. Scheduling-disabled active employees retain login access without scheduling participation. A narrow freshness-hook fix prevents creation of such a nonparticipant from invalidating drafts containing unfilled rows. There are no employee-name exceptions or special privileges.

## Verification and test classification

A real production-schema-41 backup was restored locally and upgraded to schema 46. All original columns/rows in 196 pre-existing tables retained identical fingerprints. New columns, tables, indexes and immutable-evidence protections are additive. The production sequence repeats data checks under maintenance before/after migration. Existing employee records, allocations, approved PTO and assignments must remain unchanged.

The legacy double-coverage test expected unresolved regeneration to return partially completed output. The approved preservation guard instead raises and rolls back. The test now asserts unchanged draft/evidence plus the remaining coverage warning; the runtime guard is preserved.

Broader regression reconciliation retains the deployed consignment behavior (verified unchanged from `557bd47`): current vendor assignments and locally known costs determine current inventory valuation; prospective financing/cost decisions do not replace them. Four older expectations were reproduced on the production revision and corrected in tests only. The COGS gating test now targets the retained history/COGS template, and the allowed local cost-writer list includes the already-deployed audited resolution workflow. No Square or financial application changes are bundled.

Other fixture corrections: schema-head assertions reflect 46 while testing the full historical graph; the employee profile test follows the approved compact interface; the fail-closed settings test removes inherited test environment settings; historical migration construction detaches current-schema ORM hooks; special-hour edits use legal times; generation fixtures declare exact weekly targets. PostgreSQL backup utilities match the local server major version. The isolated PostgreSQL lock-table capacity is sized for parallel migration tests; production tuning is unchanged.

Full/focused regression logs, provider configuration receipts, migration fingerprints, screenshots, rollback checks and cleanup results are kept in the enclosing workspace's `output/scheduling-production-release-2026-10-20/` directory. Credentials and backups remain private and are excluded from Git.

## Deployment and rollback

Keep automatic deployment off. Align web and the existing scheduling cron with the verified primary-branch revision. Pause cron and web writes for the cutover; take a fresh backup; apply migrations 41→46; deploy and verify source identities/schema. Configure the existing cron to Sunday 10:00 UTC (`0 10 * * 0`), retaining its command, plan, nine-future-week policy and existing execution authorization. Do not manually generate production schedules during acceptance.

The recovery tag `release/scheduling-rollback-schema46-20261020` identifies `0ce5500`, based on the verified PTO/preservation build with password-recovery changes excluded. It passed 84 focused tests and the schema gate against the migrated production copy. If acceptance fails, retain maintenance mode, pause cron, set execution disabled for recovery, deploy that compatible code, and verify identities/data before reopening. It omits the new Business Hours interface and combined weekly maintenance; review any subsequently created special-hour records before re-enabling scheduling. Do not downgrade or remove PTO/approval/audit evidence. Preserve both schema-41 backups and the schema-46 recovery reference.

After successful acceptance, remove only incorporated obsolete scheduling branches using ancestry checks and non-force deletion. Preserve unique/unfinished work, password-recovery activation work, tags and recovery branches. Detached old feature checkouts preserve local ignored files; worktree deletion is not required.

No holiday closures are entered automatically. No performance optimization is included; the accepted deployed interface is the next investigation's baseline.

## Production visual acceptance follow-up

Production acceptance exposed cached, unversioned scheduling assets: the employee editor received the prior stylesheet while the deployed runtime source was verified. Acceptance was held, maintenance restored, cron suspended, and the compatible schema-46 recovery deployment initiated. Scheduling and employee template assets now use the existing build asset version, so returning sessions request the current CSS and JavaScript. This is an asset freshness correction, with no scheduling policy or data change. Full and focused suites are rerun before the corrected deployment; no branches are removed until final acceptance.
