"""Join immutable generator evidence and employee access without rewriting history."""

revision = '20260930_0029'
down_revision = ('20260928_0028', '20260929_0028')
branch_labels = None
depends_on = None


def upgrade():
    # Both parent schemas must be installed before this revision becomes current.
    pass


def downgrade():
    # Unmerge only; parent migrations retain their evidence-preserving guards.
    pass
