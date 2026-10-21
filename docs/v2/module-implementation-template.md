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

- Mandatory provenance map reference: [V2 definitive data-source map](./data-source-map.md)
- Feature name and required business facts:
- Classification and authoritative source for each fact:
- Exact active writer/service/API and source identifiers:
- Canonical read path:
- Live/synchronized/snapshotted/immutable/derived semantics:
- V1-owned tables/files/current state:
- V2 reads:
- V2 writes:
- External/V1 write-back permissions:
- Single-writer rule:
- Historical snapshot and reversal requirements:
- Derived formulas, time boundaries, signs, and rounding:
- Missing/stale/ambiguous/conflicting-data behavior:
- Allowed owner overrides and audit requirements:
- Known legacy structures and unresolved source ambiguities:
- Historical data migration/backfill: none unless separately approved
- Square behavior: unchanged unless separately approved

Before implementation, inspect active readers and writers for every declared source. If repository evidence
contradicts the proposed authority, stop and report the conflict. Never substitute a similarly named source.

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
