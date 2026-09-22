# Ordering / Blind Count release checkpoint

Branch: `codex/ordering-blind-count-reliability`, based on `16b8f9add4a286f325f2ac0bc82f1dda8a56eb7a`.

- Manual Ordering: commit `24aea8a` — `Add catalog discovery and explicit-cost manual purchasing`.
- Blind Counts: the subsequent commit containing this report — `Preserve blind count evidence and automate correction before review`.
- No deployment, production migration, or live Square mutation was performed.

Implementation details, compatibility limits and operational behavior are in [Manual Ordering](manual-ordering-change.md) and [Blind Counts](blind-count-change.md).

## Tests

| Run | Result |
| --- | --- |
| Original source, no PostgreSQL test URL | 550 passed, 175 skipped; 7 subtests passed |
| Original source, disposable PostgreSQL 16 | 722 passed, 2 failed, 1 skipped; 7 subtests passed |
| Final focused Ordering / receiving / vendor tests | 34 passed |
| Ordering bridge / V2 shell checks | 20 passed |
| Final focused Blind Count / migration / Square boundary tests | 39 passed; includes 10 JavaScript controller scenarios |
| Final full suite with disposable PostgreSQL 16 | 743 passed, 1 baseline failure, 1 skipped; 7 subtests passed (140.78 seconds) |

Two baseline failures were reproduced from an untouched `git archive` of the starting commit:

1. `test_scheduling_0025_to_0026_adds_safe_rolling_base_metadata` asserted an outdated literal head revision. The migration assertions now follow the current schema contract; this baseline test passes.
2. `test_automation_page_renders_owner_workflow_and_draft_query_requires_management` expects scheduling-page wording including “Review Schedule” that the existing page does not render. It remains outside these two changes.

Other old migration head assertions and the SQLite generation fixture were updated for the added migration/lifecycle query. The final report distinguishes the remaining baseline scheduling failure from new regressions. Two pre-existing FastAPI lifecycle deprecation warnings remain.

Tests ran with `SQUARE_ACCESS_TOKEN=''`, `SNAPSHOT_PROVIDER=mock`, an unreachable default `DATABASE_URL`, and a separately specified disposable `TEST_POSTGRES_ADMIN_URL`; Square transport was mocked. Migration upgrade, legacy data preservation, downgrade refusal and concurrent transactions were verified on PostgreSQL, not inferred from SQLite.

## Changed files

### Manual Ordering commit

- `app/routers/management.py`
- `app/services/access_control_service.py`
- `app/services/manual_ordering_service.py`
- `app/services/purchase_order_generation_service.py`
- `app/static/v2/manual-ordering.js`
- `app/templates/base.html`
- `app/templates/management_ordering_order_detail.html`
- `tests/test_manual_ordering.py`
- `tests/test_square_vendor_reassignment.py`
- `tests/test_v2_shell.py`
- `docs/manual-ordering-change.md`

### Blind Count commit

- `app/models.py`
- `app/routers/auth.py`
- `app/routers/management.py`
- `app/routers/store.py`
- `app/schema_contract.py`
- `app/security/sessions.py`
- `app/services/blind_count_service.py`
- `app/services/count_square_sync_service.py`
- `app/services/session_service.py`
- `app/services/square_snapshot_provider.py`
- `app/static/v2/blind-count.js`
- `app/templates/base.html`
- `app/templates/count_entry.html`
- `app/templates/management_count_review_detail.html`
- `app/templates/management_count_reviews.html`
- `app/templates/management_session_detail.html`
- `migrations/versions/20260922_0025_blind_count_evidence.py`
- `tests/blind_count_controller_test.cjs`
- `tests/test_blind_count_controller.py`
- `tests/test_blind_count_reliability.py`
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_v2_digital_signage.py`
- `docs/blind-count-change.md`
- `docs/ordering-blind-count-release-checkpoint.md`

The pre-existing untracked `V1-ORDERING-BLIND-COUNTS-AUDIT.md` is retained outside these implementation commits.

## Remaining behavior and owner decisions

No new product-policy decision is required to finish these approved changes. Release still requires the normal deployment decision and controlled migration/restart; it has not been performed here. The full suite is not completely green because of the reproduced scheduling baseline failure.

Financial semantics are unchanged. Unmapped PO purchases remain receivable, but mapping-scoped inventory reports can omit them; existing financial classifications, funding assignments and consignment attribution must already exist independently. Editing a placed order does not introduce new automatic recalculation of existing payment snapshots. See the Manual Ordering compatibility notes.

Count corrections retry their saved operation through submitted-request replay or the review queue after a failure; there is no new unattended retry worker. Recovery storage is scoped to the current browser tab and is not crash-proof. Mock-derived count evidence cannot perform a live inventory correction. Existing shared-account observations cannot be attributed to a specific individual beyond the authenticated login.
