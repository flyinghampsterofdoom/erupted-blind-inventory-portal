# Render production V1 baseline compatibility profile

Governed by the [V1 Preservation Guarantee](./v1-preservation-guarantee.md) and the [schema baseline contract](./v2-schema-baseline-and-environment.md).

## Purpose and scope

`render-production-v1-20260717` is an explicit, production-specific recognition profile for the first Alembic stamp of the existing Render V1 database. It is not enabled by default. It does not change the canonical `20260715_0001` baseline, define a second canonical schema, execute DDL, alter data, or authorize a V1 cutover.

The profile exists because the operational Render database was built incrementally before Alembic ownership. A read-only comparison on 2026-07-17 confirmed the same 72 V1 tables, extensions, triggers, columns, column metadata, constraints other than the four listed below, indexes, foreign keys, and enum values as the immutable baseline. The differences are limited to physical evolution history.

The profile can be selected only by its exact name. It requires a reference database versioned at `20260715_0001` or `20260716_0002`. Unknown profile names, unversioned references, and every difference outside this record fail closed.

## Accepted physical column orders

Column metadata and membership must match the canonical reference exactly. Only these exact production orders are recognized:

| Table | Render production physical order |
|---|---|
| `par_levels` | `id`, `sku`, `vendor_id`, `manual_par_level`, `suggested_par_level`, `par_source`, `confidence_score`, `confidence_state`, `locked_manual`, `confidence_streak_up`, `confidence_streak_down`, `updated_by_principal_id`, `created_at`, `updated_at`, `store_id`, `manual_stock_up_level` |
| `principals` | `id`, `username`, `password_hash`, `role`, `store_id`, `active`, `created_at`, `updated_at`, `custom_role_label` |
| `purchase_order_lines` | `id`, `purchase_order_id`, `variation_id`, `sku`, `item_name`, `variation_name`, `unit_cost`, `unit_price`, `suggested_qty`, `ordered_qty`, `received_qty_total`, `in_transit_qty`, `confidence_score`, `confidence_state`, `par_source`, `manual_par_level`, `suggested_par_level`, `removed`, `created_at`, `updated_at`, `gtin` |
| `purchase_order_store_allocations` | `id`, `purchase_order_line_id`, `store_id`, `expected_qty`, `allocated_qty`, `store_received_qty`, `variance_qty`, `created_at`, `updated_at`, `manual_par_level` |
| `purchase_orders` | `id`, `vendor_id`, `status`, `reorder_weeks`, `stock_up_weeks`, `history_lookback_days`, `notes`, `pdf_path`, `created_by_principal_id`, `submitted_by_principal_id`, `ordered_at`, `submitted_at`, `email_sent_at`, `email_sent_by_principal_id`, `created_at`, `updated_at`, `invoice_payment_status`, `invoice_paid_date`, `invoice_paid_amount`, `invoice_difference_note` |
| `snapshot_lines` | `session_id`, `variation_id`, `sku`, `item_name`, `variation_name`, `expected_on_hand`, `source_catalog_version`, `created_at`, `section_type`, `previous_recount_variance`, `recount_closed_out` |
| `store_recount_items` | `store_id`, `variation_id`, `sku`, `item_name`, `variation_name`, `last_variance`, `updated_at`, `consecutive_match_count`, `total_count_attempts`, `last_counted_qty` |
| `vendor_sku_configs` | `id`, `vendor_id`, `sku`, `pack_size`, `min_order_qty`, `is_default_vendor`, `active`, `updated_by_principal_id`, `created_at`, `updated_at`, `square_variation_id`, `unit_cost`, `gtin` |

This is safe to recognize because the application and migrations address columns by name. The profile requires every column definition to remain identical and performs no reorder.

## Accepted enum order

The canonical `principal_role` order is `ADMIN`, `MANAGER`, `LEAD`, `STORE`. Render production retains its historical order `MANAGER`, `STORE`, `ADMIN`, `LEAD` with exactly the same four values.

The profile recognizes only that exact permutation. Authorization uses equality and membership checks rather than relational enum comparison. Existing production display ordering remains unchanged because the enum is not altered.

## Accepted absent checks

Render production lacks exactly these canonical checks:

- `change_box_par_levels.change_box_par_levels_level_non_negative_ck`
- `change_box_par_levels.change_box_par_levels_non_negative_ck`
- `non_sellable_par_levels.non_sellable_par_levels_level_non_negative_ck`
- `non_sellable_par_levels.non_sellable_par_levels_non_negative_ck`

The pre-deployment audit found zero negative `par_quantity` or `level_quantity` rows in either table. This deployment does not add the checks, change validation, or alter V1 write behavior. The profile recognizes their absence only when all other checks match the canonical reference exactly.

## Fail-closed behavior

The profile rejects, among other drift:

- a missing or extra table;
- a missing or extra column;
- any changed type, nullability, or default;
- any unrecognized column order;
- a missing, extra, or changed enum value or unrecognized enum order;
- any missing, extra, or changed constraint outside the four named checks;
- index, foreign-key, extension, or trigger drift;
- an unrelated or unversioned reference database.

`stamp-existing` repeats the comparison and verifies the reference revision before creating only the Alembic revision row. The profile never causes a baseline upgrade to run over the operational schema.

## Approved first-deployment commands

Create a disposable canonical baseline reference, then run:

```text
python -m app.schema_contract validate --database-url <production> --reference-url <baseline-reference> --compatibility-profile render-production-v1-20260717
python -m app.schema_contract stamp-existing --database-url <production> --reference-url <baseline-reference> --revision 20260715_0001 --compatibility-profile render-production-v1-20260717
python -m app.schema_contract upgrade --database-url <production>
```

The normal Render pre-deploy command after the validated first stamp is:

```text
python -m app.schema_contract upgrade
```

## Retirement conditions

Remove the profile from active tooling only after all of the following are true:

1. Production is versioned and no unversioned production clone, restore artifact, or rollback procedure needs baseline recognition.
2. A separately approved V1 schema-reconciliation plan has resolved or formally superseded every accepted difference.
3. Strict comparison without a compatibility profile passes against a canonical reference at the deployed head.
4. Migration, startup, rollback, and V1 smoke tests pass after removal.
5. The owner explicitly approves profile retirement.

Completing the first stamp alone does not retire the profile and does not approve adding the four missing constraints.
