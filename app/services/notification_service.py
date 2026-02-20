from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.audit_service import log_audit


def send_variance_report_stub(
    db: Session,
    *,
    actor_principal_id: int,
    session_id: int,
    store_name: str,
    ip: str | None,
    variance_rows: list[dict],
) -> None:
    non_zero = [row for row in variance_rows if row['variance'] != 0]
    payload = {
        'store_name': store_name,
        'session_id': session_id,
        'variance_only_lines': len(non_zero),
        'report_type': 'VARIANCE_ONLY',
        'status': 'STUB_SENT',
    }
    log_audit(
        db,
        actor_principal_id=actor_principal_id,
        action='VARIANCE_EMAIL_STUB_SENT',
        session_id=session_id,
        ip=ip,
        metadata=payload,
    )
