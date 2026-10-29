# Purchasing V4 Phase 1 identity and evidence foundation

Phase 1 implements the October 27, 2026 authorization. The module is inactive:
there are no V4 routes, workers, cron entries, owner/employee exposure, purchasing
recommendation changes, supplier assignment writes, official commitments,
inventory operations, transfers or vendor deliveries.

## Boundaries

`app/purchasing_v4` has no dependency on application database configuration,
legacy business services, HTTP clients or startup registration. Callers provide
an explicit connection. The existing application and all public-schema Alembic
migrations remain unchanged.

The foundation uses PostgreSQL schema `purchasing_v4`, migration `p1_0001` and its
own Alembic version table. `public.alembic_version` remains `20261027_0053`, as
required by the existing web and cron startup contract. This is a compatibility
choice for an inactive foundation, not permission to bypass future production
migration review. No normal application startup invokes these migrations.

Schema and import writes reject any endpoint except an explicitly supplied
`127.0.0.1` PostgreSQL database whose name begins `erupted_v4_phase1_`. URL host,
database and service overrides are rejected before the qualification CLI connects;
the writer also checks actual server/database identity. No ambient production
credentials are loaded by this module. The source snapshot is supplied externally
after a separately controlled restore into task-owned disposable infrastructure.

## Identity and provenance

An internal product groups actual Square item references; an internal variation
represents an actual Square variation reference. Internal suppliers derive only
from real existing legacy vendor rows. UUIDv5 IDs use an explicit stable local
source namespace, entity kind and original identifier. This local namespace is
not a Square merchant ID. Product names and SKUs never decide identity.

A missing parent item does not fabricate a product: the variation remains
unparented with an exception. Duplicate SKU/name values never merge variations.
Conflicting item claims and existing parent/supplier-reference changes require
review rather than silently rewriting identities. Supplier metadata can be
incomplete without inventing a supplier or Square reference.

Tables comprise `product`, `variation`, their external reference tables,
`supplier`, `supplier_reference`, `import_run`, `source_observation`,
`identity_exception`, `supplier_candidate` and `legacy_procurement_reference`.
They are append-only, including database rejection of UPDATE, DELETE and TRUNCATE.
Supplier candidates are historical configuration evidence, not active primary
assignments, and no assignment table is created in this phase.

Each import pins source digest, transform version, exact derived-plan digest and
original source observations. Money in provenance retains its exact decimal
lexical value; original accounting rows are never rewritten. Changing transform
semantics requires an explicit transform-version change. Existing entity IDs do
not depend on the import run, so a rename or a new observation does not create a
new entity. Each source row is rechecked against the target's actual legacy fact
before import, and the plan is rederived from provenance to reject tampering.

A transaction-level advisory lock serializes imports for the same source scope.
Unique identities prevent duplicate facts; an inner savepoint protects against a
caller catching a conflict and accidentally committing half an import. Repeating
the same transform and source returns the stored receipt. Interruptions before
commit leave no partially imported run. Immutable records are recovered forward,
not erased by destructive downgrade.

Legacy vendor/PO links are provenance references rather than cross-schema foreign
keys in Phase 1. The importer verifies their real source rows transactionally.
This avoids changing legacy deletion/update behavior while preserving historical
references. Future official procurement ownership, write routing and financial
bridges belong to later authorized phases; a provenance reference is not another
PO or incoming commitment.

## Evidence contracts

Use `readonly_snapshot(engine)` and `ReadOnlyEvidence(connection)`. The factory
opens one database-enforced REPEATABLE READ / READ ONLY transaction and explicitly
sets transaction-local UTC serialization. LA business dates remain the selling
date convention. A repository refuses read/write transactions, incoherent
isolation, non-UTC serialization, and reuse across transaction boundaries.

The factory's SET LOCAL is transaction control, not business persistence. Evidence
methods issue SELECT/SHOW only and never invoke legacy helpers that may create
settings, refresh source data, reconcile money or autoflush an ORM session.

`QuantityEvidence` distinguishes KNOWN (including exact zero), UNKNOWN and
UNAVAILABLE. Unknown/unavailable values cannot contain numeric zero. Inventory
may preserve a real negative source observation. Timestamps and source references
are explicit; the requested cutoff is never substituted for an observation time.
Freshness limits and date scopes must be supplied by the caller. The qualification
example's 24-hour limit is an explicit test input, not an installed business default.

* `identity_plan`: deterministic read-only catalog/vendor/config/PO source plan.
* `sales`: completed local-day gross observed source quantities with a correction
  overlay; returns remain separate. Full source coverage can establish a zero
  selling day; missing, stale, partial or post-cutoff evidence cannot. Financial
  facts remain unchanged. Gross-versus-net estimator acceptance remains a Phase 2
  decision; this adapter labels exactly which observed quantity it supplies.
* `inventory`: archived Square observations with location, refresh outcome,
  freshness, observation timestamp and numeric validation. Missing rows are
  unavailable, never zero. No Square read or refresh occurs here.
* `availability`: proves full-interval exposure only from a qualified count
  anchor and exhaustive dated movements. Positive endpoints alone are insufficient.
  Count corrections, uncertain boundary/read-window movements, mixed exposure or
  contradictory sales during an alleged full stockout remain unknown.
* `incoming`: an exhaustive legacy open-PO allocation projection. Null receipt
  amounts and conflicting store totals remain unknown, with no proportional cap
  allocator. A proved fully received/overreceived line has no remaining incoming,
  even if its old identity is unresolved. Positive unidentifiable obligations
  cannot be assigned by guessing. Closed quantities come from the complete event
  query; an empty event set establishes zero closures, not a missing receipt.

Incoming rows are cached only within the pinned read-only snapshot. There is no
second incoming ledger, V4 commitment source or new engine connected to the app.
Future V4-origin supply and central/transit state accounting require the later
canonical procurement implementation and its qualification.

## Mathematical fixtures and validation

`WindowContract` requires explicit lookback/recent windows and the approved
configurable initial minimum of 14 available days. `CandidateModelInputs`
requires every influence/sensitivity/limit explicitly; it has no tuning defaults
and cannot generate recommendations. Global/category persistence is later work.

`tests/fixtures/purchasing_v4_phase1_math.json` contains approved conversion and
policy-unit cases plus clearly labeled illustrative candidate arithmetic. Pure
reference contracts check confirmed-only velocity and final per-store half-up
pack conversion. These are not a deployed adaptive forecasting/replenishment
engine. No manual rule expires, converts or becomes preselected; existing manual
rows are preserved, and comparison/conversion UI remains a later phase.

## Reproduce qualification

Install the unchanged hash-locked runtime and test dependencies in a task-owned
virtual environment. Use Python 3.10.13 and PostgreSQL 18.6 tools for this repository.
Do not use an older `pg_dump` against the newer server.

Run the three `test_purchasing_v4_*.py` files with `TEST_POSTGRES_ADMIN_URL` pointing
only at a task-owned loopback cluster. The integration fixture creates uniquely
named disposable databases and bootstraps static synthetic historical records
before its preservation baseline. It never invokes a real order authorization or
external inventory workflow.

For a retained real snapshot restored into a separately created isolated database:

```sh
PYTHONPATH=. .venv/bin/python scripts/qualify_purchasing_v4_phase1.py \
  --database-url "$V4_ISOLATED_DATABASE_URL" \
  --source-namespace erupted-retained-square-source \
  --as-of "$V4_SNAPSHOT_CUTOFF" \
  --max-age-hours 24 \
  --output "$V4_QUALIFICATION_DIRECTORY"
```

The URL must name a loopback `erupted_v4_phase1_` database. The CLI does not capture
or connect to production. It checks every public table's count and digest, public
columns/constraints/indexes/triggers/enums and sequence positions before and after
schema/import/replay. It records mapping exceptions and all product-store incoming
states, plus an explicitly labeled bounded deterministic history sample. It also
tests interrupted transactions and rejects destructive downgrade.

Rollback means use the unchanged legacy application with the expanded database;
the inactive namespace remains. Qualify the exact original web startup check and
cron imports against that isolated database under database-enforced read-only
credentials/options. Do not execute cron business functions. Forward recovery
replays the same immutable import after interruption; it never recreates POs,
replays receipt totals or restores an old database over external effects.

## Later phase gates

The disjoint adaptive model is approved as the initial candidate. Numerical tuning
defaults, the exact Stockout Adjustment control, sales/exposure acceptance,
fallback sufficiency, fractional unit boundaries and forecast-quality evaluation
still need the decisions in the Phase 1 completion report. This foundation does
not authorize production migration, activation, deployment or Phase 2 work.
