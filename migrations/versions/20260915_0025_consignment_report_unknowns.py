"""Preserve unknown Consignment valuation and observed inventory as NULL.

No historical rows or source accounting/configuration data are rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_0025"
down_revision = "20260911_0024"
branch_labels = None
depends_on = None

COLUMNS = {
    "funding_reports": {
        "calculated_cogs": (14, 2),
        "inventory_units_snapshot": (14, 3),
        "inventory_value_snapshot": (14, 2),
    },
    "funding_report_lines": {
        "unit_cost_snapshot": (14, 4),
        "extended_cogs": (14, 2),
        "inventory_units_snapshot": (14, 3),
        "inventory_value_snapshot": (14, 2),
    },
    "funding_report_fact_links": {"cogs_amount_snapshot": (14, 2)},
}


def upgrade():
    for table, columns in COLUMNS.items():
        for column, numeric in columns.items():
            op.alter_column(
                table, column, existing_type=sa.Numeric(*numeric), nullable=True
            )


def downgrade():
    # SET NOT NULL refuses downgrade if unknown values exist. Never coerce them.
    for table, columns in COLUMNS.items():
        for column, numeric in columns.items():
            op.alter_column(
                table, column, existing_type=sa.Numeric(*numeric), nullable=False
            )
