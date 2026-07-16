# Individual authenticated employee accounts

- Status: Accepted
- Date: 2026-07-15

## Context

V1 can use principals that function as shared store logins, while V2 operational evidence must identify the person who acted. Existing roles/capabilities and historical principal IDs must remain compatible.

## Decision

Every V2 employee uses an individual authenticated principal. Existing STORE, LEAD, MANAGER, and ADMIN roles and capability resolution remain. Store assignment is principal data, not authentication identity. Every V2 operational event records the authenticated principal. Historical shared-principal attribution is preserved and never guessed retroactively.

## Consequences

V2 modules derive actor/store from the session and server scope, not typed identity fields. New V2 exposure requires individual accounts. A later approved rollout must inventory and phase out shared credentials without rewriting history; multi-store assignment remains separate.
