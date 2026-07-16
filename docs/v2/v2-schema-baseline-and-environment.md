# V2 schema baseline and environment contract

## Status

Implemented for Milestone 3. This contract changes schema management tooling, not the V1 business schema or production data. Baseline revision: `20260715_0001`.

## Behavioral baseline

The deployed SQL in `sql/schema.sql` is authoritative. The baseline executes it verbatim so PostgreSQL behavior omitted or incompletely modeled by ORM metadata remains intact: 72 tables, `citext`, 16 named enum types, 79 indexes, constraints, 42 triggers and their function, compatibility `ALTER` statements, and both GTIN columns. The migration pins the file’s SHA-256 and refuses execution if the baseline artifact changes; all future schema changes must be new revisions. See `v1-data-map.md` §Database baseline and §Enums and text-enum hazards.

Autogeneration is not used for the baseline. Future revisions may use Alembic operations where faithful, with explicit PostgreSQL SQL where required.

## Fresh database

1. Create an empty PostgreSQL database.
2. Set `DATABASE_URL` or pass it explicitly.
3. Run `python -m app.schema_contract upgrade --database-url <url>`.
4. Confirm `alembic_version.version_num = 20260715_0001`.

The bootstrap script now uses this path instead of `psql -f sql/schema.sql`.
The upgrade command refuses a non-empty unversioned database, preventing the baseline SQL from being replayed over an existing operational schema.

## Existing matching database

Never run the baseline upgrade against an operational database merely to add the revision record.

1. Create a disposable empty reference database and upgrade it through Alembic.
2. Run `python -m app.schema_contract validate --database-url <existing> --reference-url <reference>`.
3. Review any drift. The comparison covers tables/columns/defaults, primary/unique/check/foreign-key constraints, indexes, extensions, enum values, triggers, and ORM table/column coverage.
4. Only if it matches, run `python -m app.schema_contract stamp-existing --database-url <existing> --reference-url <reference>`.

`stamp-existing` repeats validation, refuses drift or an already-versioned database, then creates only Alembic’s revision record. It does not run baseline business DDL.

## Startup behavior

Before Milestone 3, application startup executed two additive GTIN `ALTER TABLE` statements, and vendor mapping sync invoked the same mutator.

After Milestone 3, imports do not connect to or modify the database. Startup reads `alembic_version` and accepts only a supported revision. Missing, multiple, unknown, or unreadable revision state raises `UnsupportedSchemaError` with a migration/stamp instruction. `SCHEMA_REVISION_CHECK_ENABLED=false` is intended only for bounded tooling/tests and must not be a production workaround.

## Demo seed environments

`ENVIRONMENT` defaults to the production-safe value `production`; the local `.env.example` and newly generated bootstrap environment explicitly use `development`. `DEMO_SEED_ENABLED` defaults to `false`.

- Disabled: seed command reports that it did nothing.
- Enabled in a non-production environment: seed remains available for deliberate local development.
- Enabled in `production`, `prod`, `staging`, `stage`, `qa`, or `preview`: command refuses with a nonzero exit.

The legacy seed still contains known example identities and data (`Downtown`, demo campaigns/groups, `manager`, `lead1`, `store1`). This milestone does not delete or change any existing row or password. The bootstrap visibly evaluates the policy every run.

The local bootstrap also refuses a non-local `DATABASE_URL`; remote migration execution requires deliberate use of the reviewed schema command outside the convenience bootstrap.

## Operational assumptions

- Schema comparison needs a disposable reference database created from the same migration head.
- A production schema must be inspected read-only before stamping.
- The baseline downgrade is deliberately refused; rollback restores application compatibility and database backup/revision procedure rather than dropping V1 objects.
- No deployed schema was inspected or modified in Milestone 3.
