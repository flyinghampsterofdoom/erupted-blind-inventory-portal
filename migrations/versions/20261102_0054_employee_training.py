"""Dated supplemental training and reviewed employee replacement proposals."""
from alembic import op
import sqlalchemy as sa
revision = '20261102_0054'
down_revision = '20261027_0053'
branch_labels = depends_on = None


def upgrade():
    op.create_table('employee_training',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('employee_id',sa.BigInteger(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('start_date',sa.Date(),nullable=False),
        sa.Column('duration_days',sa.Integer(),nullable=False,server_default='14'),
        sa.Column('completed_effective_date',sa.Date()),
        sa.Column('created_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint('duration_days > 0 AND duration_days <= 365',name='employee_training_duration_ck'),
        sa.CheckConstraint('completed_effective_date IS NULL OR completed_effective_date >= start_date',name='employee_training_completion_ck'))
    op.create_index('ix_employee_training_employee_id','employee_training',['employee_id'])
    op.create_table('scheduling_replacement_proposals',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('created_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('fingerprint',sa.String(64),nullable=False),sa.Column('payload',sa.JSON(),nullable=False),
        sa.Column('applied_at',sa.DateTime(timezone=True)),sa.Column('result',sa.JSON()))
    for col in [sa.Column('is_training',sa.Boolean(),nullable=False,server_default=sa.false()),
                sa.Column('training_id',sa.BigInteger()),sa.Column('trainer_shift_id',sa.BigInteger()),
                sa.Column('trainer_employee_id',sa.BigInteger()),sa.Column('trainer_override_reason',sa.Text())]:
        op.add_column('schedule_shifts',col)
    for name,column,target in [('schedule_shift_training_fk','training_id','employee_training'),
                               ('schedule_shift_trainer_shift_fk','trainer_shift_id','schedule_shifts'),
                               ('schedule_shift_trainer_employee_fk','trainer_employee_id','employees')]:
        op.create_foreign_key(name,'schedule_shifts',target,[column],['id'],ondelete='RESTRICT')
    op.create_check_constraint('schedule_shift_training_ck','schedule_shifts',
        'NOT is_training OR (training_id IS NOT NULL AND trainer_shift_id IS NOT NULL AND trainer_employee_id IS NOT NULL AND employee_id IS NOT NULL AND NOT is_lead_of_day AND NOT is_shift_lead AND NOT is_double_coverage AND owner_principal_id IS NULL)')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM employee_training UNION ALL SELECT 1 FROM scheduling_replacement_proposals LIMIT 1')).first():
        raise RuntimeError('Training and replacement history must be retained; use a compatible rollback.')
    op.drop_constraint('schedule_shift_training_ck','schedule_shifts',type_='check')
    for name in ['schedule_shift_training_fk','schedule_shift_trainer_shift_fk','schedule_shift_trainer_employee_fk']:
        op.drop_constraint(name,'schedule_shifts',type_='foreignkey')
    for name in ['is_training','training_id','trainer_shift_id','trainer_employee_id','trainer_override_reason']:
        op.drop_column('schedule_shifts',name)
    op.drop_table('scheduling_replacement_proposals')
    op.drop_table('employee_training')
