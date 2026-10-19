"""Shared feature helper must render with the actual configured setting."""
from jinja2 import Environment


def test_actual_template_feature_global_has_a_settings_binding(monkeypatch):
    from app.main import app
    from app.config import settings
    helper=app.state.templates.env.globals['purchasing_v3_enabled']
    # Invoke through Jinja exactly as both failing production templates do.
    template=Environment().from_string('{{ purchasing_v3_enabled() }}')
    for enabled in (True,False):
        monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',enabled)
        assert template.render(purchasing_v3_enabled=helper)==str(enabled)
