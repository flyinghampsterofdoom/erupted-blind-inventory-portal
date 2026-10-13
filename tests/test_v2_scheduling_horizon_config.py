"""Reject invalid X/Y before persistence or generation can begin."""
from datetime import time
from decimal import Decimal
import pytest
from app.services.v2_scheduling_policy_service import update_organization_policy
from app.services.v2_scheduling_service import SchedulingValidationError


@pytest.mark.parametrize('x,y', [(0,0),(-1,0),(53,8),(9,-1),(9,9),(9,10),(1,1)])
def test_invalid_horizon_combinations_fail_before_database_access(x,y):
    with pytest.raises(SchedulingValidationError,match='X between 1 and 52'):
        update_organization_policy(None,principal=None,weekly_approval_hours=Decimal('40'),
            schedule_length_weeks=x,replenish_at_weeks=y,generate_days_before_end=7,
            publish_days_before_end=3,publication_local_time=time(9),timezone_name='America/Los_Angeles')
