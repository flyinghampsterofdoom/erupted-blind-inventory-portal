"""Add the V2 Digital Signage module.

Revision ID: 20260720_0005
Revises: 20260719_0004
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260720_0005'
down_revision = '20260719_0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'digital_signage_displays',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('slug', postgresql.CITEXT(), nullable=False),
        sa.Column('username', postgresql.CITEXT(), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('password_rotated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True)),
        sa.Column('archived_at', sa.DateTime(timezone=True)),
        sa.Column('archived_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.UniqueConstraint('name', name='digital_signage_displays_name_key'),
        sa.UniqueConstraint('slug', name='digital_signage_displays_slug_key'),
        sa.UniqueConstraint('username', name='digital_signage_displays_username_key'),
        sa.CheckConstraint("slug ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,63}$'", name='digital_signage_displays_slug_format_ck'),
    )
    op.create_index('idx_digital_signage_displays_active', 'digital_signage_displays', ['is_enabled', 'archived_at'])

    op.create_table(
        'digital_signage_display_sessions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('display_id', sa.BigInteger(), sa.ForeignKey('digital_signage_displays.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('ip', postgresql.INET()),
        sa.Column('user_agent', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('token_hash', name='digital_signage_display_sessions_token_hash_key'),
    )
    op.create_index('idx_digital_signage_display_sessions_display_active', 'digital_signage_display_sessions', ['display_id', 'revoked_at', 'expires_at'])

    op.create_table(
        'digital_signage_media_assets',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('media_type', sa.String(length=32), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=False),
        sa.Column('public_token', sa.String(length=64), nullable=False),
        sa.Column('original_filename', sa.Text(), nullable=False),
        sa.Column('content_type', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('width', sa.Integer()),
        sa.Column('height', sa.Integer()),
        sa.Column('metadata', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('archived_at', sa.DateTime(timezone=True)),
        sa.Column('archived_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.UniqueConstraint('storage_key', name='digital_signage_media_assets_storage_key_key'),
        sa.UniqueConstraint('public_token', name='digital_signage_media_assets_public_token_key'),
        sa.UniqueConstraint('media_type', 'content_hash', name='digital_signage_media_type_hash_uniq'),
        sa.CheckConstraint("media_type IN ('IMAGE', 'HTML_ANIMATION')", name='digital_signage_media_type_ck'),
        sa.CheckConstraint('size_bytes > 0', name='digital_signage_media_size_positive_ck'),
        sa.CheckConstraint(
            "(media_type = 'IMAGE' AND width > 0 AND height > 0) OR "
            "(media_type = 'HTML_ANIMATION' AND width IS NULL AND height IS NULL)",
            name='digital_signage_media_dimensions_ck',
        ),
    )

    op.create_table(
        'digital_signage_advertisement_groups',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date()),
        sa.Column('daily_start_time', sa.Time()),
        sa.Column('daily_end_time', sa.Time()),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('archived_at', sa.DateTime(timezone=True)),
        sa.Column('archived_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.CheckConstraint('end_date IS NULL OR end_date >= start_date', name='digital_signage_groups_date_order_ck'),
        sa.CheckConstraint(
            '(daily_start_time IS NULL AND daily_end_time IS NULL) OR '
            '(daily_start_time IS NOT NULL AND daily_end_time IS NOT NULL AND daily_end_time > daily_start_time)',
            name='digital_signage_groups_daily_window_ck',
        ),
    )
    op.create_index('idx_digital_signage_groups_eligibility', 'digital_signage_advertisement_groups', ['is_enabled', 'start_date', 'end_date', 'archived_at'])

    op.create_table(
        'digital_signage_group_displays',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('advertisement_group_id', sa.BigInteger(), sa.ForeignKey('digital_signage_advertisement_groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('display_id', sa.BigInteger(), sa.ForeignKey('digital_signage_displays.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('advertisement_group_id', 'display_id', name='digital_signage_group_displays_uniq'),
    )
    op.create_index('idx_digital_signage_group_displays_display', 'digital_signage_group_displays', ['display_id', 'advertisement_group_id'])

    op.create_table(
        'digital_signage_group_items',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('advertisement_group_id', sa.BigInteger(), sa.ForeignKey('digital_signage_advertisement_groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('media_asset_id', sa.BigInteger(), sa.ForeignKey('digital_signage_media_assets.id'), nullable=False),
        sa.Column('display_duration_seconds', sa.Integer()),
        sa.Column('is_permanent', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('advertisement_group_id', 'sort_order', name='digital_signage_group_items_order_uniq'),
        sa.CheckConstraint('sort_order >= 0', name='digital_signage_group_items_order_non_negative_ck'),
        sa.CheckConstraint(
            '(is_permanent AND display_duration_seconds IS NULL) OR '
            '(NOT is_permanent AND display_duration_seconds BETWEEN 5 AND 300)',
            name='digital_signage_group_items_duration_ck',
        ),
    )
    op.create_index(
        'digital_signage_one_enabled_permanent_item_uniq',
        'digital_signage_group_items',
        ['advertisement_group_id'],
        unique=True,
        postgresql_where=sa.text('is_permanent AND is_enabled'),
    )


def downgrade() -> None:
    op.drop_index('digital_signage_one_enabled_permanent_item_uniq', table_name='digital_signage_group_items')
    op.drop_table('digital_signage_group_items')
    op.drop_index('idx_digital_signage_group_displays_display', table_name='digital_signage_group_displays')
    op.drop_table('digital_signage_group_displays')
    op.drop_index('idx_digital_signage_groups_eligibility', table_name='digital_signage_advertisement_groups')
    op.drop_table('digital_signage_advertisement_groups')
    op.drop_table('digital_signage_media_assets')
    op.drop_index('idx_digital_signage_display_sessions_display_active', table_name='digital_signage_display_sessions')
    op.drop_table('digital_signage_display_sessions')
    op.drop_index('idx_digital_signage_displays_active', table_name='digital_signage_displays')
    op.drop_table('digital_signage_displays')
