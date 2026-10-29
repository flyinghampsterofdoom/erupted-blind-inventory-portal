"""Narrow Resend transport. Never surface provider bodies, credentials or URLs."""
from dataclasses import dataclass
from html import escape
import httpx
from app.services.application_settings_service import resend_configuration


@dataclass(frozen=True)
class EmailResult:
    accepted: bool
    message: str


def send_email(db, *, recipient, subject, text, html, idempotency_key=None):
    config = resend_configuration(db, decrypt=True)
    if not config.enabled or config.status != 'Configured' or not config.api_key:
        return EmailResult(False, 'Email not sent: Resend is disabled or incompletely configured. Check Admin Integrations.')
    sender = f'{config.from_name} <{config.from_email}>' if config.from_name else config.from_email
    payload = dict(to=[recipient], subject=subject, text=text, html=html, **{'from': sender})
    if config.reply_to:
        payload['reply_to'] = config.reply_to
    headers = {'Authorization': 'Bearer ' + config.api_key}
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
            response = client.post('https://api.resend.com/emails', headers=headers, json=payload)
        if 200 <= response.status_code < 300:
            return EmailResult(True, 'Email accepted by provider. Inbox delivery is not confirmed.')
        return EmailResult(False, 'Email not sent: provider rejected the request. Check Admin Integrations.')
    except httpx.HTTPError:
        return EmailResult(False, 'Email submission could not be confirmed. Check configuration and try again.')


def send_password_email(db, *, recipient, token, setup, token_id):
    config = resend_configuration(db)
    # Fragments never enter HTTP access logs or Referer headers. The page copies
    # this into a POST field and removes the fragment from browser history.
    link = config.portal_url + '/password#' + token
    subject = 'Set up your Erupted Vapor employee account' if setup else 'Reset your Erupted Vapor password'
    introduction = 'Your Erupted Vapor employee account is ready.' if setup else 'A password reset was requested for your Erupted Vapor employee account.'
    label = 'Create Password' if setup else 'Reset Password'
    footer = 'This link expires in one hour and can be used once. If you did not expect this email, ignore it or contact your administrator. Your password will not change unless you complete this form.'
    return send_email(db, recipient=recipient, subject=subject,
        text=f'{introduction}\n\n{label}: {link}\n\n{footer}',
        html=f'<p>{escape(introduction)}</p><p><a href="{escape(link, quote=True)}">{label}</a></p><p>{escape(footer)}</p>',
        idempotency_key=f'employee-password-{token_id}')


def purchasing_resend_request(client, method, path, payload=None, *, idempotency_key=None):
    """Explicit injected Resend API client for isolated purchasing qualification.

    Existing transactional settings, credentials and routing are not changed.
    Neither raw provider error bodies nor authorization headers reach user errors.
    """
    import re
    if method not in ('GET', 'POST') or not re.fullmatch(r'/emails(?:/receiving)?(?:/[a-f0-9-]+(?:/attachments/[a-f0-9-]+)?)?', path):
        raise PermissionError('Only the qualified purchasing email endpoints are allowed')
    if method == 'POST' and path != '/emails':
        raise PermissionError('No provider administration or domain mutation is permitted')
    if str(client.base_url).rstrip('/') != 'https://api.resend.com':
        raise PermissionError('Explicit Resend API origin required')
    response = client.request(method, path, json=payload,
        headers={'Idempotency-Key': idempotency_key} if idempotency_key else {}, follow_redirects=False)
    if len(response.content) > 2_000_000:
        raise ValueError('Provider metadata exceeds its qualified bound')
    if response.status_code >= 300:
        return {'status': response.status_code, 'data': {}}
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError('Invalid Resend response')
    return {'status': response.status_code, 'data': value}
