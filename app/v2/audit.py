from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services.audit_service import log_audit


SENSITIVE_KEY_PARTS = ('password', 'secret', 'token', 'authorization', 'cookie', 'access_key')


def redact_metadata(value: Any, *, key: str = '') -> Any:
    if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(item_key): redact_metadata(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_metadata(item) for item in value]
    return value


@dataclass(frozen=True)
class V2AuditEvent:
    actor_principal_id: int
    action: str
    domain: str
    entity_type: str
    entity_id: str | int
    store_ids: tuple[int, ...] = ()
    timestamp: datetime | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
    correlation_id: str | None = None
    external_outcome: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.actor_principal_id <= 0:
            raise ValueError('V2 operational audit events require an authenticated employee principal')


def write_v2_audit_event(db: Session, *, event: V2AuditEvent, ip: str | None) -> None:
    payload = {
        'v2_contract_version': 1,
        'domain': event.domain,
        'entity_type': event.entity_type,
        'entity_id': str(event.entity_id),
        'store_ids': list(event.store_ids),
        'occurred_at': (event.timestamp or datetime.now(tz=timezone.utc)).isoformat(),
        'before': event.before,
        'after': event.after,
        'reason': event.reason,
        'correlation_id': event.correlation_id,
        'external_outcome': event.external_outcome,
        'metadata': event.metadata or {},
    }
    log_audit(
        db,
        actor_principal_id=event.actor_principal_id,
        action=f'V2:{event.domain}:{event.action}',
        session_id=None,
        ip=ip,
        metadata=redact_metadata(payload),
    )
