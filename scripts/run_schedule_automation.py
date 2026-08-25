"""Run one retry-safe scheduling automation tick.

Invoke from the deployment platform's existing cron facility. This repository does not run a second
in-process scheduler. The database advisory lock serializes overlapping invocations.
"""
from sqlalchemy import select

from app.auth import Principal, Role
from app.db import SessionLocal
from app.models import Principal as PrincipalModel, PrincipalRole
from app.services.v2_scheduling_policy_service import run_schedule_automation


def main() -> None:
    with SessionLocal() as db:
        actor = db.execute(select(PrincipalModel).where(
            PrincipalModel.active.is_(True),
            PrincipalModel.role.in_((PrincipalRole.ADMIN, PrincipalRole.MANAGER)),
        ).order_by(PrincipalModel.role, PrincipalModel.id).limit(1)).scalar_one_or_none()
        if actor is None:
            raise RuntimeError('Schedule automation requires an active admin or manager principal for audit attribution.')
        principal = Principal(id=actor.id, username=actor.username, role=Role(actor.role.value),
                              store_id=actor.store_id, active=actor.active)
        result = run_schedule_automation(db, principal=principal)
        db.commit()
        print(result)


if __name__ == '__main__':
    main()
