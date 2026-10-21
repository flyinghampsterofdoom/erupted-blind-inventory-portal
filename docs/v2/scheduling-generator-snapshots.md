# Stage A1: immutable generator proposals

Scope: evidence capture only. No occurrence identity, published-assignment guards,
permissions preset, attendance changes, fairness changes, horizon changes, or UI.

## Storage and serialization contract

Migration `20260928_0028` follows production schema `20260927_0027` and adds only
`schedule_generator_snapshots`. `ScheduleGeneratorSnapshot` records period ID,
batch UUID, monotonic per-period sequence, INITIAL/REGENERATION kind, actor, origin,
server recording timestamp, before/after period versions, schema version, optional
build identity, JSON payload, and SHA-256 checksum. Restrictive parent references
preserve evidence. Assignment IDs inside JSON are historical references, not FKs.

Contract version 1 is explicitly enumerated in `V1_FIELDS` in
`v2_scheduling_snapshot_service.py`; new ORM columns do not silently extend it.
Payload sections:

- `schema_version`, `generator_contract`, `build_identity`;
- `generation`: batch, sequence, kind, origin, actor ID/username, period versions;
- `before`: complete relevant input draft, including on first generation;
- `result`: period metadata, ordered shift rows, and persisted warning rows;
- `diagnostics`: full generator return diagnostics;
- `policy` and `store_defaults`: captured organization/standard-shift context.

Each shift includes employee/store IDs and captured names, shift type name and
source StoreShift label, date/times/break, unassigned status through nullable
employee ID, Lead/double-coverage flags, manual locks, notes, provenance and base
pattern annotations. It captures persisted generator state, not Board-derived
presentation flags. No current Board serializer or warning rebuild is used by
snapshot reads/serialization. Dates/times are ISO strings, aware timestamps are
UTC, Decimal values are strings, enums are their values. Arrays are ID-ordered.
Checksum: SHA-256 of UTF-8 JSON, sorted object keys, compact separators,
`ensure_ascii=False`, `allow_nan=False`, covering the entire payload.

The payload is self-contained; later name changes or working-shift deletion do
not change it. Names/assignment notes make this administrative evidence, not a
public employee endpoint. Context is useful diagnostic evidence, not a promise
that all mutable inputs needed for deterministic replay have been captured.

## Integration and transactions

All existing callers enter `regenerate_period`. Its wrapper locks/reloads the
period, captures the input, executes the unchanged generator core, captures the
final flushed result after all repairs/reconciliation/warnings/target diagnostics,
and writes `SCHEDULE_REGENERATED` audit with snapshot ID, batch and versions.
No generator return keys or assignment logic changed. Audit diagnostics retain
the old fields and additionally include full returned shift-target diagnostics.

A savepoint contains generation, snapshot insertion, and generation audit. Errors
propagate and roll back this unit, even if a caller catches the error and commits
its outer transaction. New generated-period creation also has a savepoint, so
failure does not leave a placeholder claiming successful generation. Existing
route/job transaction boundaries remain authoritative; there is no new commit.

The period lock serializes max(sequence)+1; a unique constraint is the backstop.
Each explicit regeneration gets its own record, even for identical output.
Rolling generation supplies one batch UUID to all newly generated weeks; untouched
weeks receive no records. Manual horizon audit shares the batch correlation ID.
Origins distinguish manual horizon, automation, rolling service, and direct
period regeneration. Automation publication can occur later in the same outer
transaction without mutating the recorded draft proposal.

## Immutability and retrieval

No snapshot mutation API/service exists. ORM update/delete listeners reject
mutation; a PostgreSQL statement trigger rejects UPDATE, DELETE and TRUNCATE,
including bulk SQL. Administrators with database DDL authority can still remove
protections; checksums do not claim protection against a privileged DBA.

`read_generator_proposals` is an active-Admin-only internal read service. It
requires a period ID, exact week-start date, or snapshot ID; combinations narrow
the selection. It returns detached dictionaries, ordered by period and sequence,
including the input draft. No HTTP endpoint, UI, or permissions were added.
An empty result means **Generator proposal unavailable**. It does not reconstruct
or fabricate legacy evidence. Readers inspect `schema_version`; future schema
changes must deliberately version the contract.

## Build identity and migration safety

Build identity uses the same existing runtime variables as application assets:
`RENDER_GIT_COMMIT`, then `GIT_COMMIT`, preserving the full value. If neither is
provided, it is null (unknown), not a guessed Git SHA. The serialization contract
identifier is not represented as a deployed generator commit.

There is no backfill. A legacy previously generated draft's first captured
regeneration is sequence 1 / REGENERATION; sequence 1 does not claim to be its
original historical proposal. Downgrade is allowed only while the table is empty;
once evidence exists it explicitly refuses destructive downgrade. Plan rollback
around retaining this additive schema and the repository's strict schema contract.
No production migration or deployment was performed.

## Validation

Tests use disposable loopback PostgreSQL databases, an unreachable default app
DATABASE_URL, an empty Square token, and mock snapshot provider. Focused coverage
includes generation/result equality, input capture, multiple proposals, edits,
locks/designations/double coverage/template replacement/publication/revision
cloning, live label changes, detached reads, concurrency, batch/no-op behavior,
failures before/after insertion, outer rollback, ORM and direct SQL protection,
legacy absence, empty migration roundtrip and evidence-preserving downgrade refusal.

Validation results:

- Existing complete Scheduling foundation/repair suite: **338 passed** (the
  combined run also passed the then-current 7 snapshot cases: 345 total).
- Final expanded snapshot suite: **11 passed**.
- Broader repository suite, including PostgreSQL migration tests: **682 passed,
  1 skipped; 7 subtests passed**. The optional real-R2 integration remains skipped.
- Focused migration/schema and related schema-head assertions: **37 passed,
  1 skipped** in the intermediate focused rerun.
- Generator core AST comparison, Python compilation, and `git diff --check`: passed.
- Only existing FastAPI startup-event deprecation warnings were reported.

Commands used the existing workspace virtualenv with `PYTHONPATH=.` and explicit
local test environment overrides. Test modules: `test_v2_scheduling_foundation.py`,
`test_v2_scheduling_repair.py`, `test_v2_scheduling_snapshots.py`; the broader run
was `pytest -q tests` excluding those three modules.

## Review notes and changed files

Implementation is isolated from baseline `35d6c7f3683a1503dbab65a32fabddfdb1486828`.
The branch starts at local `48f958d`, whose only difference from that baseline is
an existing release-readiness documentation correction; application code matches.

Changed files:

- `app/models.py`: snapshot evidence model.
- `app/schema_contract.py`: new required schema head.
- `app/services/v2_scheduling_snapshot_service.py`: V1 serializer, checksum,
  append-only insertion, ORM guards, Admin-only detached retrieval.
- `app/services/v2_scheduling_policy_service.py`: transactional capture wrapper,
  batch/origin propagation, audit references; generator core decisions unchanged.
- `migrations/versions/20260928_0028_scheduling_generator_snapshots.py`: additive
  table, PostgreSQL immutability trigger, evidence-preserving downgrade policy.
- `tests/test_v2_scheduling_snapshots.py`: focused PostgreSQL behavior tests.
- `tests/test_schema_migration_postgres.py`: additive table-count expectations.
- `tests/test_square_cost_workflow_boundaries.py`: expected schema head only.
- `tests/test_v2_digital_signage.py`: expected schema head only.
- `docs/v2/scheduling-generator-snapshots.md`: this contract/release record.

No unexpected dependency required A2 or changes to the scheduling business rules.
An AST comparison to the branch base confirmed the extracted generator core is
identical except for relocation of its audit emission to the capture wrapper.
No assignment-output differences were discovered in regression validation.

Release recommendation: ready for independent A1 code review after the checks
below. No push, merge, deployment, production migration, or production schedule
regeneration is part of this change. The evidence-preserving downgrade refusal
requires an explicit release rollback plan; do not drop captured evidence.
