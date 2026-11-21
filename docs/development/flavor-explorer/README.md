# Staff Product Explorer

E-liquid Explorer and Compatibility Explorer use existing staff sessions and permissions.
Open E-liquid administration at /v2/flavor-intelligence, staff suggestions at /v2/flavor-suggestions,
the staff discovery screen at /explorer/, and Compatibility at /v2/compatibility-explorer.

Manufacturer and own-photo candidates have separate immutable histories. A reviewed manufacturer
photo remains primary after a later own upload. Removing one candidate selects the retained fallback;
catalog reset and explicit hide remain available. Existing original assets are read by exact active
variation/media links. Staff media endpoints require normal authentication.

The production PostgreSQL migration adds separate fi_ and cp_ tables. Existing principals, stores,
taxonomy, financial records and historical workflows are read targets and remain independently owned.
Transactions serialize Explorer writes with a transaction-scoped advisory lock. Leased read-only Square
refresh keeps the last good observations; missing, stale or conflicting stock is unknown.

The public manufacturer seed contains sourced compatibility facts only. The fixed operational release
bundle stays in encrypted private custody and supplies reviewed photos, exact retailer associations
and exclusion policy directly to the existing database. Historical development observations and images
are retained in their original private authority and are omitted from this public source checkout.

The production rollout plans in this directory describe the exact release and pending compatibility
amendment. Full hosted CI, current restore rehearsal, normal owner authentication and live source/media
verification are separate release gates.
