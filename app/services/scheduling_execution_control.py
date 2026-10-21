"""Process execution permission, independent of scheduling policy and Render state."""
import logging
import os

ENVIRONMENT_KEY = 'SCHEDULE_AUTOMATION_EXECUTION_ENABLED'
ENABLED_VALUES = frozenset({'true', '1', 'yes', 'on'})
DISABLED_MESSAGE = 'SCHEDULE_AUTOMATION_DISABLED: scheduling automation execution disabled; no automation work performed.'


def scheduling_execution_enabled() -> bool:
    # Read on invocation, not import. Missing/malformed values always fail closed.
    if os.environ.get(ENVIRONMENT_KEY, '').strip().lower() not in ENABLED_VALUES:
        return False
    from app.config import settings
    return not settings.scheduling_read_only


def disabled_automation_result() -> dict:
    logging.getLogger(__name__).info(DISABLED_MESSAGE)
    return {'ok': False, 'execution_enabled': False, 'status': 'disabled',
            'generated_period_ids': [], 'published_period_ids': [],
            'blocked_period_ids': [], 'message': DISABLED_MESSAGE}
