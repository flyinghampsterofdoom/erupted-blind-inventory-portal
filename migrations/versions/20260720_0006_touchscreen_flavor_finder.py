"""Add the V2 touchscreen flavor finder and local Square read cache.

Revision ID: 20260720_0006
Revises: 20260720_0005
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260720_0006'
down_revision = '20260720_0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'touchscreen_sync_runs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='RUNNING'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('freshness_at', sa.DateTime(timezone=True)),
        sa.Column('error_summary', sa.Text()),
        sa.Column('variation_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('inventory_record_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_complete', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='SET NULL')),
        sa.CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name='touchscreen_sync_runs_status_ck'),
    )
    op.create_index('idx_touchscreen_sync_runs_status_started', 'touchscreen_sync_runs', ['status', 'started_at'])

    op.create_table(
        'touchscreen_square_variation_cache',
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('sku', sa.Text()),
        sa.Column('item_name', sa.Text(), nullable=False),
        sa.Column('variation_name', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('is_sellable', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('source_updated_at', sa.DateTime(timezone=True)),
        sa.Column('successful_run_id', sa.BigInteger(), sa.ForeignKey('touchscreen_sync_runs.id'), nullable=False),
        sa.Column('cached_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_touchscreen_variation_cache_sku', 'touchscreen_square_variation_cache', ['sku'])

    op.create_table(
        'touchscreen_store_inventory_cache',
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('available_quantity', sa.Numeric(14, 3), nullable=False),
        sa.Column('is_location_present', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('successful_run_id', sa.BigInteger(), sa.ForeignKey('touchscreen_sync_runs.id'), nullable=False),
        sa.Column('freshness_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('idx_touchscreen_inventory_availability', 'touchscreen_store_inventory_cache', ['store_id', 'available_quantity'])

    op.create_table(
        'touchscreen_devices',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='ACTIVE'),
        sa.Column('orientation', sa.String(16), nullable=False, server_default='AUTO'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True)),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('name', name='touchscreen_devices_name_key'),
        sa.UniqueConstraint('token_hash', name='touchscreen_devices_token_hash_key'),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name='touchscreen_devices_status_ck'),
        sa.CheckConstraint("orientation IN ('AUTO', 'LANDSCAPE', 'PORTRAIT')", name='touchscreen_devices_orientation_ck'),
    )
    op.create_index('idx_touchscreen_devices_store_status', 'touchscreen_devices', ['store_id', 'status'])

    op.create_table(
        'touchscreen_flavors',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('brand_name', sa.Text(), nullable=False),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('slug', postgresql.CITEXT(), nullable=False),
        sa.Column('short_description', sa.Text(), nullable=False),
        sa.Column('long_description', sa.Text()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_touchscreen_visible', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sweetness', sa.Integer()),
        sa.Column('tartness', sa.Integer()),
        sa.Column('cooling_intensity', sa.Integer()),
        sa.Column('is_staff_favorite', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_new_arrival', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('slug', name='touchscreen_flavors_slug_key'),
    )
    op.create_index('idx_touchscreen_flavors_visibility', 'touchscreen_flavors', ['is_published', 'is_touchscreen_visible', 'is_active', 'deleted_at'])
    op.create_index('idx_touchscreen_flavors_display_order', 'touchscreen_flavors', ['display_order', 'display_name'])

    op.create_table(
        'touchscreen_flavor_categories',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('slug', postgresql.CITEXT(), nullable=False),
        sa.Column('category_type', sa.String(16), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('category_type', 'slug', name='touchscreen_flavor_categories_type_slug_uniq'),
        sa.CheckConstraint("category_type IN ('BROAD', 'FRUIT', 'OTHER_NOTE')", name='touchscreen_flavor_categories_type_ck'),
    )
    category_table = sa.table(
        'touchscreen_flavor_categories',
        sa.column('name', postgresql.CITEXT()), sa.column('slug', postgresql.CITEXT()),
        sa.column('category_type', sa.String()), sa.column('display_order', sa.Integer()), sa.column('is_active', sa.Boolean()),
    )
    broad_names = ['Fruity', 'Savory', 'Dessert', 'Candy', 'Beverage', 'Tobacco', 'Mint/Menthol']
    fruit_names = [
        'Apple', 'Banana', 'Blueberry', 'Cherry', 'Citrus', 'Coconut', 'Dragon Fruit', 'Grape', 'Guava',
        'Kiwi', 'Lemon', 'Lime', 'Mango', 'Melon', 'Orange', 'Peach', 'Pineapple', 'Raspberry',
        'Strawberry', 'Watermelon',
    ]
    op.bulk_insert(category_table, [
        {'name': name, 'slug': name.lower().replace('/', '-').replace(' ', '-'), 'category_type': 'BROAD', 'display_order': index, 'is_active': True}
        for index, name in enumerate(broad_names)
    ] + [
        {'name': name, 'slug': name.lower().replace(' ', '-'), 'category_type': 'FRUIT', 'display_order': index, 'is_active': True}
        for index, name in enumerate(fruit_names)
    ])

    op.create_table(
        'touchscreen_flavor_category_links',
        sa.Column('touchscreen_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('category_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavor_categories.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_touchscreen_category_links_category', 'touchscreen_flavor_category_links', ['category_id', 'touchscreen_flavor_id'])

    op.create_table(
        'touchscreen_flavor_sku_links',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('touchscreen_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('square_variation_id', sa.Text(), nullable=False),
        sa.Column('format', sa.String(16), nullable=False),
        sa.Column('cooling_type', sa.String(16), nullable=False, server_default='UNKNOWN'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('touchscreen_flavor_id', 'square_variation_id', name='touchscreen_flavor_sku_links_flavor_variation_uniq'),
        sa.CheckConstraint("format IN ('SALT', 'FREEBASE')", name='touchscreen_flavor_sku_links_format_ck'),
        sa.CheckConstraint("cooling_type IN ('ICED', 'NON_ICED', 'UNKNOWN')", name='touchscreen_flavor_sku_links_cooling_ck'),
    )
    op.create_index('idx_touchscreen_sku_links_variation', 'touchscreen_flavor_sku_links', ['square_variation_id', 'is_active'])

    op.create_table(
        'touchscreen_flavor_store_overrides',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('touchscreen_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('inventory_display_threshold', sa.Integer()),
        sa.Column('reason', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('store_id', 'touchscreen_flavor_id', name='touchscreen_flavor_store_overrides_store_flavor_uniq'),
        sa.CheckConstraint('inventory_display_threshold IS NULL OR inventory_display_threshold >= 0', name='touchscreen_flavor_store_overrides_threshold_ck'),
    )

    op.create_table(
        'touchscreen_flavor_recommendations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('source_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('recommended_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('relationship_type', sa.String(32), nullable=False, server_default='SIMILAR'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('source_flavor_id', 'recommended_flavor_id', name='touchscreen_flavor_recommendations_direction_uniq'),
        sa.CheckConstraint('source_flavor_id <> recommended_flavor_id', name='touchscreen_flavor_recommendations_not_self_ck'),
    )
    op.create_index('idx_touchscreen_recommendations_source', 'touchscreen_flavor_recommendations', ['source_flavor_id', 'sort_order', 'is_active'])

    op.create_table(
        'touchscreen_flavor_media',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('touchscreen_flavor_id', sa.BigInteger(), sa.ForeignKey('touchscreen_flavors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('media_asset_id', sa.BigInteger(), sa.ForeignKey('digital_signage_media_assets.id'), nullable=False),
        sa.Column('role', sa.String(24), nullable=False, server_default='PRIMARY'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('alt_text', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('touchscreen_flavor_id', 'role', 'sort_order', name='touchscreen_flavor_media_role_order_uniq'),
        sa.CheckConstraint("role IN ('PRIMARY', 'THUMBNAIL', 'DETAIL', 'BRAND_LOGO', 'BACKGROUND')", name='touchscreen_flavor_media_role_ck'),
    )
    op.create_index('idx_touchscreen_flavor_media_asset', 'touchscreen_flavor_media', ['media_asset_id'])


def downgrade() -> None:
    op.drop_index('idx_touchscreen_flavor_media_asset', table_name='touchscreen_flavor_media')
    op.drop_table('touchscreen_flavor_media')
    op.drop_index('idx_touchscreen_recommendations_source', table_name='touchscreen_flavor_recommendations')
    op.drop_table('touchscreen_flavor_recommendations')
    op.drop_table('touchscreen_flavor_store_overrides')
    op.drop_index('idx_touchscreen_sku_links_variation', table_name='touchscreen_flavor_sku_links')
    op.drop_table('touchscreen_flavor_sku_links')
    op.drop_index('idx_touchscreen_category_links_category', table_name='touchscreen_flavor_category_links')
    op.drop_table('touchscreen_flavor_category_links')
    op.drop_table('touchscreen_flavor_categories')
    op.drop_index('idx_touchscreen_flavors_display_order', table_name='touchscreen_flavors')
    op.drop_index('idx_touchscreen_flavors_visibility', table_name='touchscreen_flavors')
    op.drop_table('touchscreen_flavors')
    op.drop_index('idx_touchscreen_devices_store_status', table_name='touchscreen_devices')
    op.drop_table('touchscreen_devices')
    op.drop_index('idx_touchscreen_inventory_availability', table_name='touchscreen_store_inventory_cache')
    op.drop_table('touchscreen_store_inventory_cache')
    op.drop_index('idx_touchscreen_variation_cache_sku', table_name='touchscreen_square_variation_cache')
    op.drop_table('touchscreen_square_variation_cache')
    op.drop_index('idx_touchscreen_sync_runs_status_started', table_name='touchscreen_sync_runs')
    op.drop_table('touchscreen_sync_runs')
