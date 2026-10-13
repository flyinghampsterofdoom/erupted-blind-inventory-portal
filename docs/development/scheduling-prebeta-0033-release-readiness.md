# Scheduling V2: final schema 0033 release readiness

October 13, 2026. **Technical release rehearsal passed. Production release remains gated by the operational prerequisites below and separate authorization.** Production schema/data, deployed applications, feature exposure, employee permissions, scheduling configuration, and automation were not changed. No Git push, deployment, or external-provider mutation occurred.

The final procedure is the [deployment checklist, stop conditions, and recovery plan](scheduling-prebeta-0033-deployment-checklist.md). It supersedes the earlier 0032 procedure. [Sanitized evidence](evidence/scheduling-prebeta-0033-release.json) records exact identities and results.

## Exact accepted artifacts

These source commits are the release identities. A later documentation-only HEAD is not the tested candidate. Every frozen source file was compared with its Git blob, and every source file inside each Linux image was checked against the sealed manifest: **625 candidate files and 627 recovery files, zero mismatches**.

| Artifact | Source commit | Local branch |
| --- | --- | --- |
| Web and cron | `0e2d0ed0d4bc6161e8290ba238cb0b9256f412a5` | `codex/scheduling-prebeta` |
| Bounded recovery | `d4ff6856ac592250eb2e09a20c4960d4b3edfce0` | `codex/scheduling-prebeta-recovery-0033-final` |

Protected source archives:

- [Web/cron archive](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-0033-20261013/release-final/web-cron.tar.gz): 3,337,357 bytes; SHA-256 `cbc60536ca2d80ea11872dc0c9253f16632cfe26df7248d605e7bd266cf504e4`.
- [Recovery archive](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-0033-20261013/release-final/recovery.tar.gz): 3,340,735 bytes; SHA-256 `6f5c1779d7ca5024a96145efd866b265f2a8465aec24fc5c558280bf5d8556c1`.
- [Complete per-file manifests](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-0033-20261013/release-final/artifact-manifest.json).

Accepted Linux `amd64` image IDs:

- Web/cron: `sha256:f128d53198f9f214b108b6ceb4eb56f80bc91d3bcc24e8eee60cca12aa9bbe81`.
- Recovery: `sha256:cfe68ad7c386cbfa09d3ea54b6e1c6f363c8814e08d559926b71fc59bd7c318d`.

The [saved image bundle](/Users/justinrawlinson/.codex/reports/scheduling-prebeta-release-0033-20261013/linux-images-final.tar.gz) contains both images, 262,850,190 bytes, SHA-256 `7ef4f2e3acadd9ca4a586f784c914276b31cd58f956d33af626736c88cb75d0a`. It contains source/runtime layers, not database volumes or production credentials. The pinned base is `python:3.10.13-slim-bullseye@sha256:e89739e9b11753ace337116c9086c4ba780c0c3daff118a7c680783cf7afada1`. Nothing has been published to a registry or Git remote.

## Final regression and Linux gates

The **complete suite against the final frozen candidate passed: 1,368 tests, seven subtests, zero failures**, in 570.75 seconds. One opt-in live R2 integration was skipped; it requires external-provider activity outside this authorization. The full suite ran on the locked macOS Python 3.10.13 runtime with disposable PostgreSQL databases. See [the regression result](evidence/scheduling-prebeta-0033-regression.txt).

Both exact Linux images passed the shared production build command, hash-required binary dependency installation, and `pip check`. Linux acceptance used an isolated local VM with **x86-64 Rosetta emulation, Python 3.10.13, and PostgreSQL 18.3**, matching the production database minor version. This was Linux execution, including real uvicorn startup and HTTP `/login` returning 200; it was not a Render deployment or a native Render hardware test. Platform promotion/startup acceptance remains part of the future authorized deployment checklist.

Linux candidate, bounded recovery, and forward restart passed authentication, existing schedule reads, store-scoped inventory reads/draft saves, direct cron command and wrapper gates, and zero external-provider attempts. Recovery rejected all registered Scheduling/HR/account/reset mutation routes, blocked direct ORM/bulk protected writes, and kept automation inert even with its execution flag deliberately true. Missing columns, missing clear markers, disabled immutable-snapshot protection, and disabled schema validation were rejected.

Only two genuine release blockers required code changes after feature acceptance:

1. Recovery packaging removed the body of a new conditional board-cache block. Packaging now leaves an explicit `pass`, with a compile regression that also runs inside frozen archives.
2. PostgreSQL 18.3 and 18.6 deparse one equivalent preference-status constraint with different varchar/text array casts. The recovery contract now canonicalizes **only the two exact approved representations**. Changed status values, omitted values, different expressions, and unrelated schema drift remain rejected. Linux recovery and its negative checks passed after the fix.

No additional feature development was performed. Superseded intermediate archives/images are not release targets.

## Production-data migration and retained records

Fresh production snapshots were acquired using GET-only deployment inspection and database connections forced read-only. Custom-format dumps streamed directly into a restricted loopback PostgreSQL 18.6 clone; no raw dump file was written. Production credentials were never propagated to application tests or images. Linux representative data streamed from the local clone into a network-isolated, memory-backed PostgreSQL 18.3 container, with no published ports.

The final candidate rehearsed **0031 → 0032 → 0033**. Fingerprints of original columns/records across **172 business tables** were unchanged immediately after migration. Existing foreign-key definitions and sequence values survived. Retained source records included:

| Record | Original count |
| --- | ---: |
| Employees | 115 |
| Accounts | 15 |
| Stores | 9 |
| Schedule periods | 8 |
| Assignments | 143 |
| Inventory count sessions | 903 |
| Employee/account links | 1 |

Migration retained the existing X=8 policy and initialized Y=7. X=9/Y=8 was configured **only on the clone**. Nine complete future weeks, October 18 through December 13, generated successfully, passed authoritative validation, stayed unpublished, and produced zero extra weeks on repeat generation. A Lead reviewed, approved, and published a representative next week on the clone; the continuity indicator became Yellow while other generated weeks awaited publication.

## Clearing, fairness, inventory, and regeneration

Both Admin controls were exercised through the final artifact against migrated representative periods: pending clear previewed **nine periods/252 assignments**; confirmed clear previewed **one published period/28 assignments**. Count previews, CSRF, exact typed confirmation, successful retirement, and no regeneration during clearing passed. Seven completed historical periods remained exactly unchanged.

After clearing, retained assignment/snapshot/approval/attendance/commitment/Lead/inventory records remained intact. A separate all-table check covered **176 retained business tables and 142,482 existing audit rows**. Only intended period reset metadata and employee context versions changed, plus employee modification timestamps maintained by the existing database trigger. All other original employee fields, identities, account links, configuration, inventory, history, and audit rows remained unchanged. Employee timestamp changes were checked against context-version changes rather than ignored globally.

Cleared future assignments were excluded from effective planning and future fairness. A tagged synthetic current-day published fixture proved that retirement removes normal inventory authorization: it could not authorize access by itself. Explicit Admin-approved temporary access through the existing emergency grant/selection infrastructure remained available, stale submissions failed, and expiration removed access. Linux recovery retained these clear markers, grants, original-store draft values, and authorization semantics; forward restart passed the same checks.

Nine-week regeneration after clearing used the original scheduling roster, passed authoritative validation, produced no publications, and created no duplicate weeks on repeat. Final checks found **nine active future drafts, zero serious warnings, zero double-coverage shifts, zero store-days with multiple employees, and zero orphaned account/employee/store/period/source/approval/snapshot/grant references**. Twenty retired clone revisions retained their clear markers.

## Recovery result and limits

Recovery retained approvals, generation outcomes, approved preferences, emergency grants, immutable snapshots, clear markers, and all scheduling/inventory history. It preserved the strict schema/evidence contract while allowing unaffected workflows and selection of existing, unexpired emergency grants. New grants, scheduling/account changes, resets, generation, publication, migration, and stamping remained unavailable.

Destructive downgrade to 0032 was refused once clear evidence existed. Returning to 0031 was also refused by retained new evidence. Recovery followed the supported path: **candidate → schema-compatible bounded recovery → forward candidate restart**, with retained data. Do not deploy old 0031 applications, prior 0032 recovery, or a superseded intermediate 0033 recovery build on the accepted schema.

## Remaining production release gates

There are **no unresolved code, migration, regression, or Linux-runtime blockers** in this rehearsal. Production release is still on hold for:

1. **Published coverage:** production still has eight DRAFT periods, July 12–October 11, and no current/next published schedule. Authorized Leads must review and publish current and next operating coverage before employee-specific inventory rollout. Never automatically publish or bulk-publish historical drafts.
2. **Approved operational configuration:** production remains X=8, with the Double Coverage store selector configured. Authorize/apply X=9/Y=8 and the approved single-employee store configuration during rollout. The 28 active coverage requirements already specify one employee.
3. **Identity/access/exposure rollout:** production has 11 scheduling-active employees, three Lead-capable employees, and one employee/account link. Approve the intended mappings, Lead publication permissions, and feature scope. Unlinked legacy store-account smoke tests do not establish personal employee authorization for unmapped staff.
4. **Release authorization and checkpoint:** approve the exact candidate/recovery identities and maintenance window, publish/promote them only under that authorization, freeze writers, and obtain/verify a fresh recoverable checkpoint and secured configuration backup. Perform platform startup acceptance before reopening workflows. Automation activation remains separately authorized.

The final read-only production inspection confirmed schema **0031**, unchanged web commit `83b247976ef99e9923496211ef1134c203244889`, unchanged cron commit `94594090ef44ee3c329f75069407bc599b268aae`, auto-deploy disabled, cron suspended, and both execution gates false. Feature exposure and configuration were not changed by this work.

Disposable databases were removed, PostgreSQL data/WAL and temporary credentials deleted, and the task's Linux database container and VM removed. Only protected code/runtime archives and sanitized evidence remain. The [deployment checklist](scheduling-prebeta-0033-deployment-checklist.md) specifies the approved future order, stop conditions, and forward-recovery procedure.
