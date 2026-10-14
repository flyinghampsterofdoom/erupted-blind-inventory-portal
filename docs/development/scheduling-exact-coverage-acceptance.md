# Exact store/day coverage enforcement

Baseline: `c774ac46fb7bc494f073cd8f382371cdb5ce1833`; schema remains `20261014_0034`.

## Behavior

Configured active coverage demand is the automatic full-shift position budget.
Overlapping windows use their maximum required headcount, matching the existing
materializer; they are not summed. Explicit headcount two still creates two
positions. An unconfigured store/weekday or explicit closure does not grant
additional capacity. Operating-hour ambiguity remains a blocking configuration
error for generation/publication.

Weekly target completion retains bounded reassignment of existing positions and
never inserts supplemental shifts. Both repair passes remain, around the existing
count-preserving Lead staffing repair. Eligibility, hours, availability, PTO,
NEVER exclusions, employment cutoffs, consecutive-day limits, regular/reserve
selection and company-wide Lead requirements remain authoritative. Unmet targets
are informational; unresolved required coverage or Lead duty remains actionable.

Shared whole-period validation counts every position in the current revision,
including vacancies and ineligible assignees; those cannot disguise excess
headcount. It runs before regeneration changes manual/copied proposals, after
coverage materialization, after repair, and in the shared approval/publication
assignment validator. Approval and publication recheck persisted assignments
under the existing scheduling advisory lock and do not waive excess through
warning overrides or a stale warning cache.

Existing locked/manual/copied excess is preserved for correction. A failed
regeneration rolls its output, version changes, snapshots and audit back through
the existing savepoint. Disposable unlocked automatic supplemental output from
older generation can still be replaced by the configured positions during an
explicitly requested regeneration. Manual editing can stage conflicting drafts;
this correction blocks approving/publishing them rather than deleting them.

The board reports `EXCESS_COVERAGE` separately from unfilled required coverage,
`NO_LEAD_OF_DAY`, and informational `BELOW_TARGET_SHIFTS`. Zero-assignment eligible
employees now receive target diagnostics. Target attainment counts distinct
workdays; PTO remains separate context. Existing `shift_targets` result shape is
preserved, with an additional `unmet_shift_targets` result carrying shortfalls.
Existing generation snapshots/audit JSON retain those details without a migration.

Historical backfill is exempt from prospective headcount reinterpretation;
completed evidence, effective attendance/replacement resolution, Longview
employee-week fairness, weekend fairness and Lead fairness are unchanged.

## Verification

New PostgreSQL integration tests cover:

- Four stores, 28 weekly positions, excessive employee targets and regeneration.
- Explicit two-person coverage, maximum aggregation of overlapping requirements,
  and visible zero-assignment target diagnostics.
- Manual excess, atomic regeneration preservation, copied proposals and safe
  replacement of obsolete disposable automatic output.
- Approval/publication rejection despite warning-cache deletion, forged current
  approval evidence or warning-override requests; editing after approval is
  revalidated before publication.
- Concurrent publication versus excess manual editing under the existing lock.
- Hard store exclusions leave required coverage and Lead duty unresolved rather
  than creating or illegally assigning extra coverage.
- Historical rows are not reinterpreted using today's staffing requirements.

Existing target-overlap expectations are updated to shortages without extra
positions. Synthetic legacy fixtures explicitly declare coverage where needed;
remaining eligibility, fairness, PTO, historical and inventory regressions stay
in the full suite. Test counts and exact commit are in the development handoff.
All integration data is synthetic and held in disposable loopback PostgreSQL.
No production credentials, Square calls, notifications or automation are needed.

## Existing data and release implications

There is no startup rewrite, schedule cleanup, profile change or migration.
Existing published records remain untouched. Excess drafts remain editable but
cannot be approved or published; regeneration of protected excess fails without
losing the proposal. Historical reconstruction and inventory authorization do not
use this new prospective staffing cap.

The web/cron candidate is schema-0034 compatible. Existing recovery
`8876502a1745894f2cf2773aaa86a4718a1a82a9` remains schema-compatible but predates the
coverage correction. A release must validate a replacement recovery retaining
this rule, or document recovery with generation disabled and known old behavior.
Schema compatibility alone is not equivalent behavioral recovery.

Production deployment, cron artifact promotion and operational activation remain
separately authorized. Production X=9/Y=8, feature exposure, employee links,
permissions, Longview configuration and retained draft conflicts are untouched
by this development work.
