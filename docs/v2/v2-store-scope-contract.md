# V2 store-scope contract

## Boundary

Implemented in `app/v2/store_scope.py` for future V2 modules. The Milestone 1 shell selector remains presentation-only; no V1 query or business action uses this resolver yet.

Authentication is individual per employee. The authenticated principal supplies identity and role; store assignment is an attribute. Current effective behavior supports one assigned store for `STORE`. Future multi-store employee assignments require a separate model/migration.

## Inputs

- Authenticated principal.
- Server-calculated authorized active stores.
- Repeatable `store_id=<integer>` query parameters.
- Optional `scope=all`.
- Read versus ordinary-write intent.

The database-backed adapter currently defines authorized stores as the principal’s assigned active store for `STORE`, and all active stores for ADMIN/MANAGER/LEAD. The latter is compatibility behavior, not a final assignment policy.

## Output

`ResolvedStoreScope` contains ordered stores with IDs/names, mode (`assigned`, `single`, `multiple`, `all`), `locked`, and `write_compatible`. It also exposes immutable `store_ids` and `store_names` tuples.

## Authorization rules

- Any requested unauthorized ID returns 403; partial authorized intersection is never silently accepted.
- No authorized stores returns 403, not an empty successful result.
- Invalid IDs or conflicting `scope=all` plus explicit IDs return 422.
- A STORE principal resolves to its assigned store, locked. Another ID returns 403. `scope=all` cannot expand it.
- ADMIN/MANAGER/LEAD may read one, multiple, or all stores in their server-authorized set.
- Client parameters never establish authorization.

## Read and write rules

Reads may use assigned, single, multiple, or all authorized scope. Ordinary writes require exactly one resolved store; otherwise the resolver returns 409. A future multi-store command needs a purpose-built contract and may not bypass this default.

The resolver does not persist preferences. Remembered scope is later presentation work and never grants access.

## URLs and examples

| Persona/role | URL | Result |
|---|---|---|
| Individual STORE assigned North (10) | no scope, `scope=all`, or `store_id=10` | North; assigned, locked, write-compatible |
| Same STORE | `store_id=20` | 403 |
| LEAD with current all-active compatibility | `store_id=10&store_id=20` | Multiple read scope; not ordinary-write compatible |
| MANAGER | `store_id=10` | Single read/write-compatible scope |
| ADMIN | `scope=all` or no scope on a read | All authorized stores |
| Any role | authorized and unauthorized IDs mixed | 403; no reduced success |

## Timezone and exports

Scope resolution does not calculate business dates. Callers must apply each store’s declared timezone when grouping store-day facts and must label mixed-timezone totals. Exports repeat resolved IDs/names, filters, timezone basis, and generation time.

## Unresolved decisions

- Authoritative LEAD/MANAGER store assignment and whether it differs by capability.
- Data model for an employee assigned to multiple stores.
- Per-user/per-domain scope preference persistence.
- Behavior for inactive or non-Square stores.
- Whether a management write may default to a previously selected single store or must always be explicit.
