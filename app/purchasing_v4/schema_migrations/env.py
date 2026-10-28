from alembic import context
from app.purchasing_v4.isolation import require_isolated

connection = context.config.attributes.get('connection')
if connection is None or context.is_offline_mode():
    raise RuntimeError('Use the explicit isolated foundation migration entry point')
require_isolated(connection)
context.configure(connection=connection, version_table='alembic_version',
                  version_table_schema='purchasing_v4', transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
