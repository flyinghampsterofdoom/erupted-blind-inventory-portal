"""Run one retry-safe scheduling automation tick.

Invoke from the deployment platform's existing cron facility. This repository does not run a second
in-process scheduler. The database advisory lock serializes overlapping invocations.
"""
from app.services.scheduling_execution_control import (
    disabled_automation_result, scheduling_execution_enabled,
)


def main() -> None:
    import json
    import time
    import uuid
    from app.services.cron_observation_service import receipt
    from app.services.runtime_identity_service import cron_runtime_proof
    identity = cron_runtime_proof()
    print(json.dumps(identity), flush=True)
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    def emit(phase, **kwargs):
        print(json.dumps(receipt(identity, run_id, phase,
            duration_ms=round((time.monotonic()-started)*1000, 2), **kwargs)), flush=True)
    emit('started')
    if not scheduling_execution_enabled():
        print(disabled_automation_result())
        emit('disabled')
        return

    try:
        _execute(emit)
    except Exception:
        emit('failed')  # No exception, username, record ID or SQL in receipt.
        raise


def _execute(emit):

    # No configuration/database imports on the disabled path.
    from app.schema_contract import assert_supported_schema
    assert_supported_schema()
    from sqlalchemy import select, text
    from app.auth import Principal, Role
    from app.db import SessionLocal
    from app.models import Principal as PrincipalModel, PrincipalRole
    from app.services.v2_scheduling_policy_service import run_schedule_automation

    with SessionLocal() as db:
        schema = db.scalar(text('SELECT version_num FROM alembic_version'))
        actor = db.execute(select(PrincipalModel).where(
            PrincipalModel.active.is_(True),
            PrincipalModel.role.in_((PrincipalRole.ADMIN, PrincipalRole.MANAGER)),
        ).order_by(PrincipalModel.role, PrincipalModel.id).limit(1)).scalar_one_or_none()
        if actor is None:
            raise RuntimeError('Schedule automation requires an active admin or manager principal for audit attribution.')
        principal = Principal(id=actor.id, username=actor.username, role=Role(actor.role.value),
                              store_id=actor.store_id, active=actor.active)
        result = run_schedule_automation(db, principal=principal)
        # Reuse metadata from the already-flushed ledger row: observation must
        # not add a query/failure point to the business transaction.
        durable_work = db.info.get('schedule_automation_outcome')
        db.commit()
        # Terminal success only after the original business transaction commits.
        emit('committed', schema=schema, result=result, durable_work=durable_work)


if __name__ == '__main__':
    main()
