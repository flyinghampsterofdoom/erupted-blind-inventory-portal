"""Permanent inventory, account and store identities for encrypted draft recovery.

Additive only: business columns, quantities and audit history are unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261024_0050'
down_revision = '20261024_0049'
branch_labels = depends_on = None

TABLES = ('stores', 'principals', 'count_sessions', 'admin_store_counts',
          'daily_chore_sheets', 'change_box_counts', 'non_sellable_stock_takes')


def upgrade():
    op.execute("""CREATE OR REPLACE FUNCTION protect_inventory_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.inventory_identity IS DISTINCT FROM OLD.inventory_identity THEN
            RAISE EXCEPTION 'Permanent inventory identity cannot be changed';
        END IF;
        RETURN NEW;
        END $$""")
    for table in TABLES:
        inspector = sa.inspect(op.get_bind())
        columns = {c['name']: c for c in inspector.get_columns(table)}
        if 'inventory_identity' not in columns:
            op.add_column(table, sa.Column('inventory_identity', sa.Uuid(), nullable=False,
                                          server_default=sa.text('gen_random_uuid()')))
        else:
            retained = columns['inventory_identity']
            if not isinstance(retained['type'], sa.Uuid) or retained['nullable'] or 'gen_random_uuid' not in (retained['default'] or ''):
                raise RuntimeError('Retained identity column does not match the permanent identity contract')
        if not any(c['column_names'] == ['inventory_identity'] for c in inspector.get_unique_constraints(table)):
            op.create_unique_constraint(f'{table}_inventory_identity_key', table, ['inventory_identity'])
        op.execute(f'DROP TRIGGER IF EXISTS protect_inventory_identity ON {table}')
        op.execute(f'CREATE TRIGGER protect_inventory_identity BEFORE UPDATE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION protect_inventory_identity()')


def downgrade():
    # Compatibility rollback retains additive columns, defaults, constraints and
    # immutability triggers. Old code ignores them; re-upgrade must not remint IDs.
    # Never drop these identities, even when the prior application is restored.
    pass
