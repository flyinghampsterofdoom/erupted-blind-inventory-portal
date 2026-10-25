# Employee authorization observation cases

The Phase 3.5 observation sampler previously selected the lowest-ID active STORE
account with any assigned store and required a nonempty store scope. That is not
a valid assumption: `list_authorized_stores` filters inactive stores, and
`resolve_store_scope` correctly denies unavailable assigned scope. The historical
failed production observation remains authoritative evidence of the sampler bug.

The corrected sampler is part of the deployed application observation service.
An external evaluator cannot infer the missing positive case from the old signed
aggregate. This change therefore requires its own reviewed source, CI, release
packet, source-bound advisory artifact and separate deployment authorization.
Preparing this change does not qualify the corrected behavior in production.

## Existing rules, unchanged

A principal must be active, and a linked employee must also be active, for normal
authentication. Shared legacy accounts may have no linked Employee row. The
session loader enforces these facts; `get_current_principal` enforces the resulting
identity's active state. Observation does not create/load/renew browser sessions.
Synthetic integration tests cover normal session rejection for either inactive
principal or inactive linked employee.

Fixed account-store discovery returns only the active assigned store for STORE
principals. It returns no stores for inactive or absent assignments. Explicit
cross-store selection is denied, including selection of inactive stores.
Deactivating a store does not delete historical assignments or employee identity.
Store capability discovery alone is not an operational inventory access grant.

Linked employee inventory scope is a separate, existing resolver: current
published, nonhistorical, uncleared scheduling and effective coverage determine
work at active stores, subject to employee activation and employment eligibility.
Historical shifts and inventory grants are evidence, not current grants. A
session's selected store cannot expand that authority. An employee may have no
current operational store; multiple eligible active assignments can also be
ambiguous and must fail closed. The observation's fixed-scope cases do not claim
to test this entire dynamic workflow. Existing operational integration tests and
the six-case synthetic matrix verify those rules independently of the sampler.

## Bounded deterministic samples

Each case uses its own lowest account ID, never an arbitrary later passing user.
Eligible active cases require both active account and active linked employee
when present. Four cases are required for a complete sampled result:

| Case | Expected assertion |
|---|---|
| Active identity, active assigned store | Exactly that store, locked scope; unauthorized store rejected |
| Active identity, inactive assigned store | Empty available scope; both default and explicit inactive-store resolution denied |
| Active identity, no assigned store | Empty fixed scope, resolution denied; no inference about separately authorized scheduled work |
| Inactive account or linked employee | Actual authentication dependency rejects the inactive identity constructed from the existing activation facts |

Active cases retain scheduling-board, management, purchasing denial and employee
store-capability assertions. Negative cases cannot replace positive coverage.
Missing subjects are explicitly INCONCLUSIVE, including missing negative-case
subjects; no fabricated identity is created in production. Every available case
is still evaluated. Any genuine privilege or authorized-access denial failure
wins over missing evidence. No IDs, names, selected-store inputs or session tokens
are returned. Owner/Lead policy behavior is unchanged.

The existing four-second database observation budget, read-only transactions,
statement/lock/connect timeouts, one-in-flight semaphore, fixed signed GET path,
30-second timestamp window and generic signature denials remain unchanged. The
larger finite sample has a tested SQL ceiling below 90 statements rather than the
previous single-employee ceiling of 60. This is neither a global scan nor an
all-account authorization or login proof. There is no nonce persistence or new
infrastructure.
