"""Install the combined staff Product Explorer on the existing portal database."""
from alembic import op
from app.services.flavor_explorer import production_schema_v0058 as frozen

revision="20261120_0058"
down_revision="20261119_0057"
branch_labels=depends_on=None

def upgrade():
    frozen.upgrade(op.get_bind())

def downgrade():
    frozen.downgrade(op.get_bind())
