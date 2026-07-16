import pytest

from app.config import Settings
from app.seed_example import DemoSeedRefused, demo_seed_decision


def test_demo_seed_disabled_by_default_behavior():
    assert demo_seed_decision(environment='development', enabled=False) == 'disabled'


def test_settings_fail_closed_without_environment_file():
    clean = Settings(_env_file=None)
    assert clean.environment_normalized == 'production'
    assert clean.demo_seed_enabled is False


def test_demo_seed_is_permitted_only_when_deliberately_enabled_locally():
    assert demo_seed_decision(environment='development', enabled=True) == 'enabled'


@pytest.mark.parametrize('environment', ['production', 'prod', 'staging', 'stage', 'qa', 'preview'])
def test_demo_seed_is_refused_in_production_like_environments(environment):
    with pytest.raises(DemoSeedRefused):
        demo_seed_decision(environment=environment, enabled=True)
