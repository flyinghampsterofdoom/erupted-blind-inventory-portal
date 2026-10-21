"""Persist vendor scope for funding reports and payments.

Revision ID: 20260805_0017
Revises: 20260803_0016
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op


revision = '20260805_0017'
down_revision = '20260803_0016'
branch_labels = None
depends_on = None


def _add_vendor_column(table: str, index: str) -> None:
    op.add_column(table, sa.Column('vendor_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(f'{table}_vendor_id_fkey', table, 'vendors', ['vendor_id'], ['id'])
    op.create_index(index, table, ['vendor_id'])


def upgrade() -> None:
    _add_vendor_column('funding_reports', 'idx_funding_reports_vendor')
    _add_vendor_column('funding_payments', 'idx_funding_payments_vendor')

    op.execute(sa.text("""
        UPDATE funding_reports AS report
        SET vendor_id = account.vendor_id
        FROM funding_accounts AS account
        WHERE report.account_id = account.id
          AND account.account_type = 'CONSIGNMENT'
    """))
    op.execute(sa.text("""
        UPDATE funding_payments AS payment
        SET vendor_id = account.vendor_id
        FROM funding_accounts AS account
        WHERE payment.account_id = account.id
          AND account.account_type = 'CONSIGNMENT'
    """))

    op.execute(sa.text("""
        WITH eligible AS (
            SELECT account.id AS account_id, MIN(vendor.id) AS vendor_id
            FROM funding_accounts AS account
            JOIN payment_methods AS method ON method.id = account.payment_method_id
            JOIN vendor_payment_settings AS setting
              ON setting.default_payment_method_id = method.id
            JOIN vendors AS vendor ON vendor.id = setting.vendor_id
            WHERE account.account_type = 'CREDIT_CARD'
              AND account.is_active = TRUE
              AND method.is_active = TRUE
              AND method.category = 'CREDIT_CARD'
              AND vendor.active = TRUE
            GROUP BY account.id
            HAVING COUNT(*) = 1
        )
        UPDATE funding_reports AS report
        SET vendor_id = eligible.vendor_id
        FROM eligible
        WHERE report.account_id = eligible.account_id
          AND report.vendor_id IS NULL
    """))
    op.execute(sa.text("""
        WITH eligible AS (
            SELECT account.id AS account_id, MIN(vendor.id) AS vendor_id
            FROM funding_accounts AS account
            JOIN payment_methods AS method ON method.id = account.payment_method_id
            JOIN vendor_payment_settings AS setting
              ON setting.default_payment_method_id = method.id
            JOIN vendors AS vendor ON vendor.id = setting.vendor_id
            WHERE account.account_type = 'CREDIT_CARD'
              AND account.is_active = TRUE
              AND method.is_active = TRUE
              AND method.category = 'CREDIT_CARD'
              AND vendor.active = TRUE
            GROUP BY account.id
            HAVING COUNT(*) = 1
        )
        UPDATE funding_payments AS payment
        SET vendor_id = eligible.vendor_id
        FROM eligible
        WHERE payment.account_id = eligible.account_id
          AND payment.vendor_id IS NULL
    """))


def downgrade() -> None:
    for table, index in (
        ('funding_payments', 'idx_funding_payments_vendor'),
        ('funding_reports', 'idx_funding_reports_vendor'),
    ):
        op.drop_index(index, table_name=table)
        op.drop_constraint(f'{table}_vendor_id_fkey', table, type_='foreignkey')
        op.drop_column(table, 'vendor_id')
