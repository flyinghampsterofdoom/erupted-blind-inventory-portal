# V2 schema baseline and environment contract

Schema work is governed by the [V1 Preservation Guarantee](./v1-preservation-guarantee.md). A migration may make V2 code deployable, but it does not activate V2, transfer canonical ownership, alter V1 routes, or approve V1 retirement.

## Status

The V1 baseline remains `20260715_0001`. The current and only supported repository head is `20260828_0026`.

The additive chain is:

| Revision | Purpose |
|---|---|
| `20260715_0001` | Immutable V1 deployed schema baseline |
| `20260716_0002` | Daily Store Logs and Current Store session context |
| `20260718_0003` | Staff Scheduling foundation |
| `20260719_0004` | Reusable Store Shifts |
| `20260720_0005` | Digital Signage |
| `20260720_0006` | Customer Touchscreen and Square read cache |
| `20260725_0007` | Ordering product lifecycle overrides |
| `20260725_0008` | Ordering-owned catalog identity snapshot and refresh state |
| `20260725_0009` | Ordering-owned current-inventory refresh runs and last-valid per-store counts |
| `20260728_0010` | Owner-preview order payments and rolling consignment-settlement ledger |
| `20260728_0011` | Immutable consignment sales, returns, effective-dated attribution/cost, and report facts |
| `20260728_0012` | Vendor-first historical payment classification, auditable backfill records, and exact preview-defect cleanup |
| `20260729_0013` | Auditable manual Consignment adjustments |
| `20260801_0014` | Owner vendor-assignment, manual-payment, and order-balance events |
| `20260803_0015` | Shared Funding Accounts and period COGS reports |
| `20260803_0016` | Order-derived Funding Report costs |
| `20260805_0017` | Vendor attribution for Funding Reports and payments |
| `20260810_0018` | Purchase-lot lineage for deployed credit-card Funding reports |
| `20260812_0019` | Private Saved Views for the Unified Reporting Workbench |
| `20260814_0020` | Replenishment PO finalization idempotency and Saved View report type |
| `20260824_0021` | Policy-driven scheduling, automation, special-store rotation, locks, and transfers |
| `20260825_0022` | Square-linked Scheduling employee roster and local participation state |
| `20260825_0023` | Funding FIFO exception events and resolution workflow |
| `20260826_0024` | Scheduling Lead capability, Lead-of-Day and Double Coverage designations, and Store Defaults |
| `20260826_0025` | Scheduling standard shift times, coverage-generated positions, and employee weekly shift targets |
| `20260828_0026` | Eight-week rolling horizon, deterministic A/B base patterns, and generated deviation metadata |

## Behavioral baseline

The deployed SQL in `sql/schema.sql` is authoritative. The baseline executes it verbatim so PostgreSQL behavior omitted or incompletely modeled by ORM metadata remains intact: 72 tables, `citext`, 16 named enum types, 79 indexes, constraints, 42 triggers and their function, compatibility `ALTER` statements, and both GTIN columns. The migration pins the file’s SHA-256 and refuses execution if the baseline artifact changes; all future schema changes must be new revisions. See `v1-data-map.md` §Database baseline and §Enums and text-enum hazards.

Autogeneration is not used for the baseline. Future revisions may use Alembic operations where faithful, with explicit PostgreSQL SQL where required.

## Fresh database

1. Create an empty PostgreSQL database.
2. Set `DATABASE_URL` or pass it explicitly.
3. Run `python -m app.schema_contract upgrade --database-url <url>`.
4. Confirm `alembic_version.version_num = 20260828_0026`.

The bootstrap script now uses this path instead of `psql -f sql/schema.sql`.
The upgrade command refuses a non-empty unversioned database, preventing the baseline SQL from being replayed over an existing operational schema.

## Existing matching database

Never run the baseline upgrade against an operational database merely to add the revision record.

1. Create a disposable empty reference database at the revision that exactly matches the existing database. For an unversioned V1 schema, use `upgrade --revision 20260715_0001`.
2. Run `python -m app.schema_contract validate --database-url <existing> --reference-url <reference>`.
3. Review any drift. The comparison covers tables/columns/defaults, primary/unique/check/foreign-key constraints, indexes, extensions, enum values, triggers, and ORM table/column coverage.
4. Only if it matches, run `python -m app.schema_contract stamp-existing --database-url <existing> --reference-url <reference> --revision 20260715_0001`.
5. Apply reviewed additive revisions with `python -m app.schema_contract upgrade --database-url <existing>`.

`stamp-existing` repeats validation, refuses drift or an already-versioned database, then creates only Alembic’s revision record. It does not run baseline business DDL.

### Render production compatibility recognition

The first Render deployment uses the explicit `render-production-v1-20260717` profile documented in [Render production V1 baseline compatibility profile](./render-production-v1-compatibility-profile.md). The profile recognizes only the audited physical column orders in eight evolved tables, the historical `principal_role` enum order, and four absent par-level checks. It leaves the immutable baseline canonical, changes no V1 schema or data, and rejects every additional difference.

The profile must be named explicitly on both `validate` and `stamp-existing`. It requires a versioned canonical reference at the requested revision. It is not a general drift-ignore switch and is never used by ordinary startup or migration commands.

## Startup behavior

Before Milestone 3, application startup executed two additive GTIN `ALTER TABLE` statements, and vendor mapping sync invoked the same mutator.

Imports do not connect to or modify the database. Startup reads `alembic_version` and currently accepts only `20260828_0026`. Missing, multiple, unknown, or unreadable revision state raises `UnsupportedSchemaError` with a migration/stamp instruction. `SCHEMA_REVISION_CHECK_ENABLED=false` is intended only for bounded tooling/tests and must not be a production workaround.

## Demo seed environments

`ENVIRONMENT` defaults to the production-safe value `production`; the local `.env.example` and newly generated bootstrap environment explicitly use `development`. `DEMO_SEED_ENABLED` defaults to `false`.

- Disabled: seed command reports that it did nothing.
- Enabled in a non-production environment: seed remains available for deliberate local development.
- Enabled in `production`, `prod`, `staging`, `stage`, `qa`, or `preview`: command refuses with a nonzero exit.

The legacy seed still contains known example identities and data (`Downtown`, demo campaigns/groups, `manager`, `lead1`, `store1`). This milestone does not delete or change any existing row or password. The bootstrap visibly evaluates the policy every run.

The local bootstrap also refuses a non-local `DATABASE_URL`; remote migration execution requires deliberate use of the reviewed schema command outside the convenience bootstrap.

## Operational assumptions

- Schema comparison needs a disposable reference database created from the same migration head.
- A production schema must be inspected read-only before stamping. Any approved compatibility profile must also match exactly and report each accepted difference.
- The baseline downgrade is deliberately refused; rollback restores application compatibility and database backup/revision procedure rather than dropping V1 objects.
- This release-hardening pass did not inspect or modify a deployed schema; target revision verification remains required before release.
- Revisions are additive by default. Destructive changes, V1 table-semantic changes, historical rewrites, backfills, and data deletion require separate written owner approval and a module-specific migration/rollback plan.
- Ordinary V2 deployment performs no automatic V1-to-V2 data migration.
- Application rollback must leave V1 operational without schema reconstruction or V1 recovery work. See [V2 deployment and rollback plan](./v2-deployment-and-rollback-plan.md).
