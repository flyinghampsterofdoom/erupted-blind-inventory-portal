from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.auth import get_current_principal
from app.db import get_db
from app.security.csrf import verify_csrf
from app.services.employee_profile_service import can_manage_employee_accounts
from app.services.application_settings_service import resend_configuration, save_resend_configuration, ConfigurationError, email_address
from app.services.transactional_email_service import send_email
from app.services.password_reset_service import throttle
from app.services.audit_service import log_audit
from app.routers.v2_scheduling import _form_back

router = APIRouter(prefix='/admin/settings/integrations', tags=['integrations'])


def admin_access(principal=Depends(get_current_principal), db: Session = Depends(get_db)):
    if not can_manage_employee_accounts(db, principal):
        raise HTTPException(403)
    return principal


@router.get('')
def page(request: Request, principal=Depends(admin_access), db: Session = Depends(get_db)):
    return request.app.state.templates.TemplateResponse('integrations.html', dict(request=request,
        config=resend_configuration(db), message=request.query_params.get('message', ''), error=request.query_params.get('error', '')),
        headers={'Cache-Control': 'no-store'})


@router.post('')
async def save(request: Request, principal=Depends(admin_access), db: Session = Depends(get_db), _=Depends(verify_csrf)):
    form = await request.form()
    try:
        replacement = str(form.get('api_key', '')) if form.get('replace_key') == 'true' else ''
        save_resend_configuration(db, actor=principal, values=form, replacement_key=replacement)
        db.commit()
        return _form_back('/admin/settings/integrations', message='Integration settings saved.')
    except ConfigurationError as exc:
        db.rollback()
        return _form_back('/admin/settings/integrations', error=str(exc))


@router.post('/test')
async def test_email(request: Request, principal=Depends(admin_access), db: Session = Depends(get_db), _=Depends(verify_csrf)):
    form = await request.form()
    try:
        recipient = email_address(form.get('recipient', ''))
    except ConfigurationError:
        return _form_back('/admin/settings/integrations', error='Enter a valid test recipient email.')
    allowed = throttle(db, f'integration-test:{principal.id}', limit=5)
    db.commit()
    if not allowed:
        return _form_back('/admin/settings/integrations', error='Test email rate limit reached.')
    result = send_email(db, recipient=recipient, subject='Erupted Vapor integration test',
        text='This is an administrator-requested transactional email test.', html='<p>This is an administrator-requested transactional email test.</p>')
    log_audit(db, actor_principal_id=principal.id, action='INTEGRATION_TEST_EMAIL', session_id=None, ip=None,
        metadata={'integration': 'resend', 'accepted': result.accepted})
    db.commit()
    return _form_back('/admin/settings/integrations', **{('message' if result.accepted else 'error'): result.message})
