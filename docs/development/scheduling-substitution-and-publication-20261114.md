# Assignment substitution and publication reliability

Implemented locally November 14, 2026. Base: aca373df2669d68f248dc1a51d8c881a52dd8d46,
the integration revision recorded as deployed November 9. No production changes
or deployments were performed. No owner's replacement was retried. The public
scheduling JavaScript still matched its recorded baseline when checked read-only.

## Findings and corrections

Administrator profile replacement previously performed whole-week warning
evaluations before and after the transfer, evaluated full assignment policy
per shift, and rebuilt persisted warnings again after the change. These reads
include every employee's assignments and cross-week evidence. Owner warnings
could force the entire attempted mutation to be rolled back and submitted again.
This is repeated database work, not a direct call to schedule generation.

A dedicated substitution service now selects the existing assignments, checks
hard conflicts in batches, transfers their employee identity, locks the intended
manual assignments, records the audit, and commits through the existing profile
transaction. It does not invoke generation, optimization, schedule-wide warning
evaluation, or preference/fairness confirmation. Its exact audited preference
decision survives publication; a later change to employee/store/date/times
invalidates that authority.

Store, date, times, break, shift type and staffing coverage stay intact.
Administrator replacement prioritizes shift coverage. If the incoming employee is
not Lead-qualified, only the transferred shifts' Lead of Day and temporary shift
Lead designations are cleared; qualifications are never granted. The saved audit
and result identify each affected draft shift, and the success notification reports
how many Lead shifts require reassignment. Lead-qualified recipients retain the
existing designations. Lead reassignment is separate and never a save prerequisite.
Employment eligibility, overlaps, one-store-per-day, recurring hard
unavailability, approved PTO, required rest, duplicate Double Coverage and active
attendance/coverage evidence remain protected. Conflict validation uses the
selected editable revisions and includes adjacent weeks for required rest.

Published source rows remain immutable. As before, an unpublished editable
revision preserves their assignments for modification. Copying now validates
store/day consistency once in memory and skips intermediate warning rebuilds for
substitution. Future incoming training rows reconciled by the explicit transition
retain their existing audited retirement behavior. Other employees' assignments
and historical schedules remain intact. Managers and training-only actions retain
their established validation and permissions.

Publishing already had immutable-period retry identity, duplicate guards and
interrupted-response recovery. However, approve-and-publish validated the complete
schedule twice and constructed a full board before committing. Even its recovery
state endpoint constructed that board again.

Approval and publication now reuse one validation only within their shared locked
transaction. Standalone publication still validates afresh; immutable approval
checksums remain verified. Read reuse avoids repeated identical eligibility facts.
The routes return a compact commit receipt. The state endpoint waits for pending
publication and returns durable state without board serialization. The frontend
confirms publication immediately, disables obsolete draft actions, and loads the
board separately. A failed board read cannot repeat publication or report a
committed publication as failed. Database errors with an uncertain COMMIT outcome
are reported as unconfirmed and reconciled before another request. Validation and
permission errors retain actionable messages and existing protections.

## Measured performance

Measurements include the real service transaction and commit against isolated
local PostgreSQL 16. The synthetic fixture matches recorded production cardinality:
117 employees, 50 schedule periods and 1,312 shifts across six stores.
No artificial delay or live business data was used.

| Existing scope | Assignments | Previous | Candidate | Previous SQL | Candidate SQL |
| --- | ---: | ---: | ---: | ---: | ---: |
| One week | 3 | 0.820 s | 0.032 s | 1,900 | 39 |
| Twelve weeks | 36 | 10.053 s | 0.044 s | 22,271 | 39 |

The one-week and twelve-week targets of 2 and 5 seconds pass locally. A separate
12-week published-source case also passes the 5-second bound and verifies every
preexisting source row remains unchanged. Hosted network latency and end-user
timings remain unmeasured; no claim is made about the exact production incident's
transaction or logs. The query multiplication provides a measured latency cause,
while the interrupted-response failure modes are reproduced in isolated tests.
Raw results are in scheduling-benchmark.json.

## Validation

127 focused regression tests and four explicit comparison cases passed, in batches
of at most two files. Coverage includes exact assignment shape, preferences without
confirmation, hard-conflict rollback, published-source immutability, old and new
revision overlap checks, profile save and board refresh, no generation during
replacement/publication, duplicate/concurrent publication, lost committed responses,
rollback after late failure, and a broken board serializer after publication.

The coverage-first follow-up removes the replacement's Lead qualification gate.
The success receipt lists affected draft shift IDs, dates, stores and prior Lead
designations in the existing audit. Each shift is counted once, even if it carried
both Lead designations. Preexisting gaps and ordinary shifts are not counted as
new Lead reassignment work. No reason, confirmation or Lead reassignment is required.
The existing board continues to diagnose missing Lead of Day coverage separately.
The historical profile-route fixture uses the repository's explicit clock helper
so its November dates do not expire with the wall clock.

Follow-up validation: 85 affected regressions passed across substitution, owner
review, training and publication completion. Mixed-qualified draft and published
replacement cases preserve shift shape, all unrelated employees and every
published source row. Tests prohibit generation, optimization and schedule-wide
warning evaluation in the replacement path. The non-Lead 12-week fixture transferred
36 shifts in 0.0491 seconds with 39 SQL statements, including commit, against the
isolated local PostgreSQL instance. This follow-up has not been deployed and no
owner schedule was changed.

## Bounded development

No authorized Erupted launcher existed. The separately scoped installed entry is:

    /Users/justinrawlinson/Library/Application Support/Ava One Development Lifecycle/bin/erupted-dev

This extends the existing architecture without modifying Ava's controller,
dispatch, quotas, credentials, recovery, or original repositories. All Erupted
source/Git, home configuration, caches and synthetic databases are on the existing
retained volume and count against the permanent 100 GB ceiling. Erupted does not
use Ava's additional temporary allowance. No new volumes or backup destinations
were created. Initial bootstrap controller files are small accounted control
outputs in the existing lifecycle storage-controls directory.

Use the registered worktree returned by start. This task is AVA-ERUPTED-SCHEDULING-20261114.
Run and write are confined to its registered scopes. Local commits check live
launcher ancestry, branch, hooks and staged scope. Remote publication is outside
this local development profile. AGENTS.md documents the entry point and limits.

Child/grandchild writes to original Erupted and Ava source, out-of-scope writes,
symlink escapes and out-of-scope staged changes were refused. Three test files
were refused. The original shared regression lock, 512 MiB Node heap, 16 MiB nursery,
two-file bound, 1536 MiB aggregate descendant RSS limit and deadline are retained.
The highest measured regression RSS was 649,510,912 bytes. Synthetic PostgreSQL
is constrained to port 55486 and its owned disposable data directory is verified
before any test database write. Host PostgreSQL remains inaccessible.

Existing native quotas and accounting admission are reused. Legacy host services
remain outside those native quotas; this is not machine-wide hard enforcement.
Only registered idle disposable caches/fixtures may be cleaned. Source, Git,
persistent configuration, unknown files and recovery custody are retained.
