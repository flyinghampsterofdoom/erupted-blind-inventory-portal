"""Add Square-linked scheduling employee roster fields.

Revision ID: 20260825_0022
Revises: 20260824_0021
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op


revision = '20260825_0022'
down_revision = '20260824_0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('employees', sa.Column(
        'scheduling_active', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    # Preserve the prior effective candidate state for existing employees. New Square imports
    # explicitly set this false until manager review.
    op.execute('UPDATE employees SET scheduling_active = active')
    op.add_column('employees', sa.Column('square_team_member_id', sa.Text()))
    op.add_column('employees', sa.Column('square_status', sa.String(length=32)))
    op.add_column('employees', sa.Column('square_location_assignment', sa.String(length=64)))
    op.add_column('employees', sa.Column(
        'square_location_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")))
    op.add_column('employees', sa.Column('square_synced_at', sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        'employees_square_team_member_id_uniq', 'employees', ['square_team_member_id'])


def downgrade() -> None:
    op.drop_constraint('employees_square_team_member_id_uniq', 'employees', type_='unique')
    for column in (
        'square_synced_at', 'square_location_ids', 'square_location_assignment',
        'square_status', 'square_team_member_id', 'scheduling_active',
    ):
        op.drop_column('employees', column)
