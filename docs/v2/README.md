# V2 documentation index

Status date: 2026-08-28. Current checkpoint schema head: `20260828_0027`. Production remains on deployed Scheduling `20260826_0024`; additive checkpoints 0025–0027 are not deployed.

Scheduling rolling-horizon semantics: Sunday `2026-01-04` is the permanent Week A anchor; each following Sunday alternates A/B by calendar parity. Employee Week A and Week B weekday masks are soft assignment preferences. Generated PTO, rotation, fairness, coverage, Lead, target-balancing, or consecutive-day exceptions are recorded on shifts and never update those masks. A normal horizon fill includes the current/live week, creates missing weeks up to the configured maximum of eight, and never regenerates an existing planned week.

Vancouver weekend fairness ranks Saturday and Sunday independently from authoritative assignments. It compares the trailing 12 weeks of published historical work, then the oldest last equivalent-day assignment, then already-planned future burden before the target date. Longview assignments are excluded. Permanent weekday lockouts and approved PTO make an employee ineligible for the affected date without creating fairness debt or changing the saved A/B pattern; a fairness-required A/B exception is recorded as `WEEKEND_FAIRNESS`.

Longview keeps permanent primary staff distinct from Vancouver rotation participants. Eligible primary staff retain coverage priority. When rotation coverage is needed, assignment-derived published history and effective planned-future Longview shifts are ranked before A/B adherence, weekly target need, store preference, and employee ID. Manual/locked Longview shifts participate in planned burden, and Longview weekend work remains excluded from Vancouver weekend fairness. PTO, permanent weekday lockouts, consecutive-day rules, and Never-store restrictions remain hard exclusions. Generated `LONGVIEW_ROTATION` deviations never modify A/B masks; scheduled assignments remain separate from any future worked, call-out, or coverage facts.

Lead coverage and Lead-of-the-Day designation are separate. Coverage repair runs only when no eligible Lead-capable employee is already scheduled and prefers an ordinary Vancouver repair over Longview disruption. Designation then selects exactly one actually scheduled Lead-capable employee using trailing 12-week published history, oldest last designation, planned-future burden, current generated-week burden, and employee ID. Valid manager overrides remain authoritative and count as planned burden. Lead metadata adds no shift, hours, weekend/Longview burden, or A/B exception; only an actual staffing repair may record `LEAD_COVERAGE`.

Published schedules remain the authoritative record of expected work. Additive `schedule_attendance_events` link to the exact published shift revision and separately record worked-as-scheduled, call-out, coverage, lateness, opened-store-late, and no-call/no-show facts. Coverage keeps the original scheduled employee and stores the actual replacement separately; it never creates a transfer or rewrites schedule history. Corrections void an event with actor, timestamp, and reason instead of deleting it. Current Longview, weekend, and Lead fairness still use published assignments, while attendance query semantics expose scheduled-versus-actual facts for a later policy layer.

## Canonical current-state documents

- [Definitive data-source map](./data-source-map.md): mandatory provenance registry, source classifications,
  legacy/fallback audit, and the source-declaration gate for future V2 work.
- [Feature parity ledger](./v1-v2-feature-parity-ledger.md): legacy replacement and V2-native implementation status.
- [Release readiness report](./v2-release-readiness-report.md): objective architecture, infrastructure, testing, risk, and confidence assessment.
- [Recommended sequence](./v2-recommended-sequence.md): current roadmap and highest-value next milestone.
- [Technical debt register](./v2-technical-debt-register.md): authoritative intentional-debt backlog.
- [Test verification](./v2-test-verification.md): most recent available-suite result and skip classification.
- [Feature exposure contract](./v2-feature-exposure-and-cutover.md): implemented keys, dependencies, and disable limitations.
- [Schema and environment contract](./v2-schema-baseline-and-environment.md): migration chain and operational rules.
- [Deployment and rollback plan](./v2-deployment-and-rollback-plan.md), [release checklist](./v2-production-release-checklist.md), and [canary guide](./v2-canary-deployment-guide.md).

## Supporting implementation records

- [Exchanges and Returns](./exchanges-returns-v2-implementation.md)
- [Daily Store Logs](./daily-store-log-v2-implementation.md)
- [Store Operations completion dashboard](./store-operations-completion-dashboard.md)
- [Staff Scheduling foundation](./staff-scheduling-v2-foundation.md), [weekly board](./staff-scheduling-v2-weekly-board.md), and [policy-driven scheduling](./policy-driven-scheduling.md)
- [Digital Signage](./digital-signage.md)
- [Unified Reporting Workbench](./reporting-workbench.md)
- [Customer Touchscreen](./touchscreen-flavor-finder.md)
- [Ordering V1 navigation bridge](./ordering-v2-milestone-plan.md)
- [Ordering, Purchasing, Receiving, Payment, and Replenishment](./ordering/README.md): current owner-canary implementation, completed evidence, and proposed phased design.
- [Ordering Phase 0 owner decisions](./ordering/phase-0-owner-decision-packet.md): all 12 calculation blockers resolved; Phase 1 planning is READY and runtime implementation still awaits approval.
- [Ordering Phase 1 implementation plan](./ordering/phase-1-implementation-plan.md): approved scope and acceptance gates, now implemented but not deployed.
- [Ordering Phase 1 implementation record](./ordering/phase-1-implementation-record.md): verified read-only runtime slice, tests, V1 parity, deviations, debt, and rollback impact; not deployed.
- [Navigation architecture](./v2-navigation-architecture.md), [store scope](./v2-store-scope-contract.md), [audit](./v2-audit-event-contract.md), [results](./v2-error-and-result-contract.md), and [statuses](./v2-status-contract.md)

## Historical and planning evidence

Documents named `v1-*`, `*-discovery`, `*-proposal`, `*-test-plan`, risk registers, and the product/UX blueprint preserve discovery or design evidence. They do not override current implementation status, current schema head, release readiness, or the technical-debt register. Module cutover records remain authoritative for explicit canonical-owner decisions; none currently records V1 retirement.
