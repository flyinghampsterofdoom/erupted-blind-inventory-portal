from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog, AuthEvent


def log_auth_event(
    db: Session,
    *,
    attempted_username: str,
    success: bool,
    ip: str | None,
    user_agent: str | None,
    principal_id: int | None = None,
    failure_reason: str | None = None,
) -> None:
    db.add(
        AuthEvent(
            attempted_username=attempted_username,
            success=success,
            failure_reason=failure_reason,
            principal_id=principal_id,
            ip=ip,
            user_agent=user_agent,
        )
    )


def log_audit(
    db: Session,
    *,
    actor_principal_id: int | None,
    action: str,
    session_id: int | None,
    ip: str | None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_principal_id=actor_principal_id,
            action=action,
            session_id=session_id,
            ip=ip,
            meta=metadata or {},
        )
    )
