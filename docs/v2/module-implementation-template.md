# V2 module implementation template

Use this record for every V2 business module. The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) applies.

## Identity and canonical ownership

- Module:
- Product owner:
- Current canonical-owner state: **V1 canonical**
- Written owner approval to change state: none / reference
- V1 retirement approval: none / separate reference

## V1 preservation

- V1 routes preserved:
- V1 templates/services preserved:
- V1 navigation/access path preserved:
- V1 authentication/shared-data access independent of V2:
- V1 failure behavior unchanged:
- V2 failure/disabled behavior leaves V1 directly usable:

## V2 scope

- V2 routes under `/v2/...`:
- Feature key and default-disabled behavior:
- Authorization and store/object scope:
- Implemented behavior:
- Explicit exclusions:

## Data and integrations

- V1-owned tables/files/current state:
- V2 reads:
- V2 writes:
- Single-writer rule:
- Historical data migration/backfill: none unless separately approved
- Square behavior: unchanged unless separately approved

## Verification

- V1 discovery:
- Parity requirements:
- Automated tests:
- Browser verification:
- Production-readiness review:
- Migration plan:
- Rollback plan:

## Release statement

Implementation, merge, or deployment does not change canonical ownership. The module remains **V1 canonical** until its cutover record contains explicit written owner approval.
