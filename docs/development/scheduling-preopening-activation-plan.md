# Scheduling activation: universal pre-opening correction

Owner-approved requirement, October 13, 2026: all four operating stores open at 09:00, and every scheduled employee begins at **08:45**, exactly 15 minutes before opening. Existing shift/coverage ends remain **22:00**. The withdrawn 10:00/09:45 Andresen example must not enter production configuration.

This is a local development correction and a future configuration plan. No production writes, migration, deployment, exposure change, or automation activation is authorized by this document. The accepted production source remains `0e2d0ed0d4bc6161e8290ba238cb0b9256f412a5`, schema `20261013_0033`. A new candidate requires separate release approval.

## Implementation

Existing `store_operating_hours` supplies active Sunday=0 through Saturday=6 actual trading intervals. Existing `store_special_hours` overrides a specific date, including explicit closures. No new schema/table, secondary store-assignment database, inferred opening from a shift, or hardcoded store-name matching is introduced.

The shared opening service requires one unambiguous actual interval per required store/date. Missing/ambiguous intervals and an opening whose preparation would cross midnight fail closed. An explicit closed-date exception has no required generated opening shift. The 15-minute preparation duration is the universal business constant; opening clocks remain existing authorized configuration.

Generation preflights the required openings and preserved end-time compatibility before replacing disposable draft positions. It derives the start from actual opening minus 15 minutes, regardless of an active Store Shift or standard-start override. Existing Store Shift/standard **end times remain authoritative and are preserved**. Configuration rows are not silently rewritten. Manual locks remain intact; an invalid locked shift requires an explicit subsequent edit before approval/publication. Weekly target completion cannot clone an invalid locked/manual start into new generated shifts; it leaves the unmet target visible for review.

Approval and publication independently re-read opening configuration and revalidate every assignment. Incorrect early/late starts and closed/missing-opening dates cannot be waived through warning permissions or an old approval. Copied schedules, templates, manual edits, and Store Shift placement share this validation. Drafts may retain invalid proposed entries for review, but cannot be approved/published.

Coverage validation distinguishes actual trading hours from the staffing interval beginning 15 minutes earlier. It checks staffing from 08:45, accepts the required preparation period without a false outside-hours warning, and retains checks for genuinely unexpected time outside that interval. Preparation remains working time for availability, overlaps, paid-hour limits and cost calculations. No automatic unpaid break is added. Readiness reports missing opening configuration before horizon generation.

Other targets, fairness rules, Lead/Longview policies, permissions, horizon policy, and inventory working-store rules are unchanged.

## Exact production configuration, after separate authorization

The October 13 read-only snapshot found zero ordinary/special hours, standard defaults #1 already 08:45–22:00, 28 active coverage windows already 08:45–22:00/count=1, and only one active Store Shift: Andresen #3 at 09:00–22:00.

| Production object | Required action |
|---|---|
| `store_operating_hours`, stores Andresen #2, HWY 99 #4, Longview #6, SR 503 #9 | Using existing audited operating-hours service, create/upsert one active 09:00–22:00 interval for each weekday 0–6: **28 records**. Re-read immediately before applying; reuse correct records if someone has since configured them, and stop on conflicting intervals. Record actual opening as 09:00, not 08:45. |
| `store_special_hours` | Add only actual owner-approved exceptions through the existing service. Do not invent holidays or change closing times implicitly. |
| `store_shifts.id=3`, Andresen Primary | Start **09:00 → 08:45 only**. Preserve end 22:00, all seven active weekdays, label, notes, identity, and other metadata. |
| `scheduling_store_defaults.id=1` | **No change:** retain standard 08:45–22:00. |
| `coverage_requirements`, active IDs #2–29 | **No change:** retain 08:45–22:00, one employee, existing store/day mappings. |
| Inactive Andresen rule #1 (09:45); retired Store Shifts #1/#2 | Keep inactive; preserve historical clocks and records. Do not reactivate. |
| Shift/schedule template tables | Snapshot has zero `shift_templates`, zero `schedule_templates`, zero `schedule_template_shifts`; no template-row correction required. Future templates/copies must pass the shared validation. |

Closing fields are not part of the authorized correction. If a preserved end is incompatible with an approved special opening or coverage interval, stop for explicit resolution instead of silently extending or shortening the shift.

Use existing audited services, scheduling mutation locks, optimistic versions, and approval/publication workflows. Do not execute bulk SQL clock updates or backfill publication/attendance to make old drafts look completed.

## Andresen drafts identified for review

Snapshot: 36 Andresen draft assignments at 09:00, across five periods. None is published. Updating configuration or filling the rolling horizon does not update these retained assignments.

| Period ID / week start | Shift IDs with 09:00 starts | Treatment |
|---|---|---|
| #4 / 2026-08-30 | 8, 12, 16, 20, 24, 28, 32 | Historical draft evidence: preserve; do not bulk rewrite. |
| #5 / 2026-09-06 | 68, 72, 76, 80, 84, 88, 92 | Historical draft evidence: preserve. |
| #6 / 2026-09-20 | 124, 152, 128, 132, 136, 140, 144, 148 | Historical draft evidence, including extra staffing: preserve. |
| #7 / 2026-10-04 | 153, 157, 161, 165, 169, 173, 177 | Historical draft evidence: preserve. |
| #8 / 2026-10-11 | 238, 242, 246, 250, 254, 258, 262 | Review against actual work and current date; targeted future edits only after authorization. |

As of October 13, the future current-week rows are **#250 October 14, #254 October 15, #258 October 16, #262 October 17**. Their proposed correction is start-only to 08:45, retaining 22:00 ends, employee/store identity, and original audit evidence. Shift #238 (October 11) is manually locked and elapsed; preserve the lock and history. Re-evaluate the applicable dates at execution time; this list is not evergreen authorization to modify elapsed work. Do not retroactively edit October 11–13 or publish old weeks to manufacture fairness credit. Current-week publication remains a separately authorized operational decision.

After authorized future edits, re-evaluate employee availability and scheduled hours, inspect full coverage, reapprove the exact edited version, and publish explicitly only when appropriate. Moving a 09:00 start to 08:45 adds 0.25 hours to that assignment. Three standard full shifts total 39.75 paid hours with the existing zero unpaid break.

## Activation/release order and stop conditions

1. Accept isolated implementation/test evidence and prepare an exact new release candidate/recovery plan. No schema migration is required; schema stays 0033. Previously approved production SHA is not an approval of new code.
2. Coordinate the compatible application deployment and the 28 actual-hours records under a separately approved checklist. The old validator incorrectly flags mandatory preparation once true 09:00 opening hours exist, so do not treat entering those rows on the old web build as a completed fix. Do not publish during an incompatible intermediate state.
3. Apply only the authorized start/configuration changes above. Confirm original closes, standard settings, coverage windows, and retained business evidence are unchanged.
4. Review applicable current-week future drafts. Preserve elapsed history, locks, counts, swaps, and attendance. Reapprove edited revisions.
5. Under separately authorized generation, verify all 28 store/day positions start 08:45, cover preparation through 22:00, and satisfy one-person staffing, Lead duty, Longview travel, work targets, and hour/availability rules. Initial horizon and publication remain separate authorized actions.
6. Keep cron suspended and both execution gates false. Do not broaden feature exposure, employee permissions, or inventory access during this correction. All other configuration readiness blockers remain in force.

Stop for missing/ambiguous hours, bad required starts, closed-date assignments, incompatible preserved closing times, stale approvals, a wrong artifact/schema, unexpected data changes, or inventory authorization/data-integrity failures. Hold the affected workflow and preserve evidence. No destructive rollback or downgrade is permitted. A schema-compatible older recovery build may restore general application availability but does not provide this corrected scheduling validation; retain the scheduling activation hold until the corrected candidate is restored.

## Required acceptance evidence

Regression coverage must show all four stores and all seven weekdays, exact 08:45 arrival for 09:00 opening, full preparation coverage, rejection of early/late starts and stale approval/configuration, Store Shift override resistance, copied/template/manual-entry validation, applicable special opening/closure behavior, fail-closed missing/ambiguous hours, preserved ends/defaults/coverage/locks, preparation in weekly-hour limits, and unchanged inventory/scheduling evidence protections. Tests run only against disposable local databases. Final test counts and candidate identity belong in the implementation report, not in this configuration plan until verified.
