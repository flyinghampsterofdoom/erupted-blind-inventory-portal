# Direct company-wide Lead of the Day — November 14, 2026

This change includes the pending employee replacement correction in 9791b2c.
They must be qualified and released together. Production schedules are never
modified for testing.

There is exactly one company-wide Lead of the Day per calendar date within a
schedule revision. The existing database uniqueness constraint and scheduling
model are unchanged. Store filters never establish separate daily Leads.

An administrator opens a scheduled employee's compact calendar card and chooses
Make Lead. The selected employee must already be Lead-capable and eligible for
that date. The operation never changes qualifications. The previous daily Lead,
including one at another authorized store, loses only their daily designation.
Remove Lead removes only that designation, leaves the shift intact and flags the
date's missing Lead coverage. Temporary shift Lead authority remains separate.

The existing inspector supplies the two-click action and the existing small Lead
label. No reason, confirmation, first-session manual-edit acknowledgement or
separate administration page is involved. Existing nonadministrator controls and
authorization remain unchanged; the new operation enforces administrator and
explicit permission/store boundaries server-side.

The service takes the shared scheduling lock, checks the expected revision,
clears the previous Lead before setting the new one under the existing unique
constraint, records the existing audit event and commits atomically. Busy or
stale schedules return an actionable conflict. Non-Lead-capable, training,
historical and unauthorized assignment requests are rejected.

No generation, optimization, assignment evaluation, fairness, replacement
processing or schedule-wide warning evaluation runs. Draft updates return only
the affected Lead flags and version. The browser patches those cards and the
date's Lead-gap warning. Normal poll/refresh uses the existing canonical board.

Published rows remain immutable. The established editable-revision path creates
an unpublished copy, applies the designation there and navigates to that revision.
An existing draft must be opened rather than creating a competing revision.
Publication review remains separate.

Regression coverage includes cross-store transfer, filtered calendars, removal,
qualification/role/CSRF/version/scope rejection, concurrent selection, published
source preservation, saved state in a fresh database session, compact client
patching, no manual confirmation, and resolving a replacement's Lead gap without
running replacement again. Local HTTP mutation met the one-second target.
The replacement benchmark remains 39 SQL statements and approximately 49 ms
for 36 shifts over twelve weeks on isolated local PostgreSQL.
