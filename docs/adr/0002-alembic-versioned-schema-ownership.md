# Alembic and versioned schema ownership

- Status: Accepted
- Date: 2026-07-15

## Context

V1 reapplied a large PostgreSQL schema and performed GTIN ALTER statements at runtime. This made deployed revision and drift unclear.

## Decision

Alembic owns schema revisions. Baseline `20260715_0001` pins and executes the deployed V1 SQL behavior. Existing databases must match a migrated reference before stamping. Application startup validates a supported revision and performs no DDL.

## Consequences

Fresh databases use migrations; existing operational databases use validate-then-stamp. Future changes require new revisions. Destructive baseline downgrade is refused. Explicit PostgreSQL SQL remains valid when ORM autogeneration cannot represent extensions, enums, triggers, or compatibility behavior faithfully.

Schema deployment does not change canonical module ownership. Under the [V1 Preservation Guarantee](0005-v1-preservation-guarantee.md), revisions are additive by default and ordinary V2 deployment performs no V1 data migration, semantic rewrite, route cutover, or retirement.
