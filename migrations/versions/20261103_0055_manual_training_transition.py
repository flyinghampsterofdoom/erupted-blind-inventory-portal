"""Manual training and profile-driven First Schedule Date; preserve prior evidence."""
from alembic import op
import sqlalchemy as sa
revision='20261103_0055'
down_revision='20261102_0054'
branch_labels=depends_on=None


def upgrade():
    op.add_column('employees',sa.Column('first_schedule_date',sa.Date()))
    op.add_column('employees',sa.Column('replacing_employee_id',sa.BigInteger()))
    op.create_foreign_key('employee_replacing_employee_fk','employees','employees',['replacing_employee_id'],['id'],ondelete='RESTRICT')
    # Explicit former completion dates remain effective. Duration alone never
    # creates completion. Old trainer/proposal columns are retained as history.
    op.execute('''UPDATE employees e SET first_schedule_date=t.completed_effective_date
        FROM employee_training t WHERE t.employee_id=e.id AND t.completed_effective_date IS NOT NULL
        AND t.id=(SELECT max(latest.id) FROM employee_training latest WHERE latest.employee_id=e.id)''')
    op.drop_constraint('schedule_shift_training_ck','schedule_shifts',type_='check')
    op.create_check_constraint('schedule_shift_training_ck','schedule_shifts',
        'NOT is_training OR (training_id IS NOT NULL AND employee_id IS NOT NULL AND NOT is_lead_of_day AND NOT is_shift_lead AND NOT is_double_coverage AND owner_principal_id IS NULL)')


def downgrade():
    if op.get_bind().execute(sa.text('''SELECT 1 FROM employees WHERE first_schedule_date IS NOT NULL OR replacing_employee_id IS NOT NULL
        UNION ALL SELECT 1 FROM schedule_shifts WHERE is_training AND (trainer_shift_id IS NULL OR trainer_employee_id IS NULL) LIMIT 1''')).first():
        raise RuntimeError('Manual training and transition history require a compatible rollback.')
    op.drop_constraint('schedule_shift_training_ck','schedule_shifts',type_='check')
    op.create_check_constraint('schedule_shift_training_ck','schedule_shifts',
        'NOT is_training OR (training_id IS NOT NULL AND trainer_shift_id IS NOT NULL AND trainer_employee_id IS NOT NULL AND employee_id IS NOT NULL AND NOT is_lead_of_day AND NOT is_shift_lead AND NOT is_double_coverage AND owner_principal_id IS NULL)')
    op.drop_constraint('employee_replacing_employee_fk','employees',type_='foreignkey')
    op.drop_column('employees','replacing_employee_id')
    op.drop_column('employees','first_schedule_date')
