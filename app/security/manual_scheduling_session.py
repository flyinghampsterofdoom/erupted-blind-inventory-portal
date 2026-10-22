"""Informational acknowledgement bound to one authenticated login, never authority."""
from datetime import timezone
import hashlib
import hmac
from app.config import settings

COOKIE = 'scheduling_manual_ack'


def signature(request):
    principal = getattr(request.state, 'principal', None)
    session_id = getattr(request.state, 'web_session_id', None)
    login_at = getattr(request.state, 'login_at', None)
    if not principal or not principal.active or not session_id or not login_at:
        return None
    content = f'manual-scheduling-v1:{principal.id}:{session_id}:{login_at.astimezone(timezone.utc).isoformat()}'
    return hmac.new(settings.app_secret_key.encode(), content.encode(), hashlib.sha256).hexdigest()


def acknowledged(request):
    expected = signature(request)
    actual = request.cookies.get(COOKIE, '')
    return bool(expected and hmac.compare_digest(expected.encode(), actual.encode()))


def set_acknowledgement(response, request):
    value = signature(request)
    if value is None:
        raise ValueError('An active authenticated login is required.')
    response.set_cookie(COOKIE, value, httponly=True, secure=settings.session_cookie_secure,
                        samesite=settings.session_cookie_samesite,
                        max_age=settings.session_ttl_minutes * 60)


def generated_schedule(db, period):
    """Copies/replacement drafts retain their generated origin, without optimizing."""
    from app.models import SchedulePeriod
    pending = [period] if period else []
    seen = set()
    while pending and len(seen) < 64:
        row = pending.pop()
        if row.id in seen:
            continue
        seen.add(row.id)
        if row.generated_at:
            return True
        for identity in (row.source_schedule_period_id, row.supersedes_schedule_period_id):
            parent = db.get(SchedulePeriod, identity) if identity else None
            if parent is not None:
                pending.append(parent)
    return bool(pending)  # Unknown overlong lineage warrants the informational notice.
