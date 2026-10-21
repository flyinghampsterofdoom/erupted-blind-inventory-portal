from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Campaign,
    CountGroup,
    CountGroupCampaign,
    Principal,
    PrincipalRole,
    Store,
    StoreRotationState,
)
from app.security.passwords import hash_password


PRODUCTION_LIKE_ENVIRONMENTS = frozenset({'production', 'prod', 'staging', 'stage', 'qa', 'preview'})


class DemoSeedRefused(RuntimeError):
    pass


def demo_seed_decision(*, environment: str, enabled: bool) -> str:
    clean_environment = str(environment or '').strip().lower()
    if not enabled:
        return 'disabled'
    if clean_environment in PRODUCTION_LIKE_ENVIRONMENTS:
        raise DemoSeedRefused(
            f'Demo seeding is refused in production-like environment {clean_environment!r}.'
        )
    return 'enabled'


def seed(*, environment: str | None = None, enabled: bool | None = None) -> None:
    decision = demo_seed_decision(
        environment=environment if environment is not None else settings.environment_normalized,
        enabled=enabled if enabled is not None else settings.demo_seed_enabled,
    )
    if decision != 'enabled':
        return
    with SessionLocal() as db:
        store = db.execute(select(Store).where(Store.name == 'Downtown')).scalar_one_or_none()
        if not store:
            store = Store(name='Downtown', square_location_id='LOC-MOCK-001', active=True)
            db.add(store)
            db.flush()

        active_campaigns = db.execute(select(Campaign).where(Campaign.active.is_(True)).order_by(Campaign.id.asc())).scalars().all()
        if not active_campaigns:
            demo_labels = ['Demo Category A', 'Demo Category B', 'Demo Category C', 'Demo Category D']
            for label in demo_labels:
                db.add(Campaign(label=label, category_filter=label, brand_filter=None, active=True))
            db.flush()
            active_campaigns = db.execute(
                select(Campaign).where(Campaign.active.is_(True)).order_by(Campaign.id.asc())
            ).scalars().all()

        active_groups = db.execute(
            select(CountGroup).where(CountGroup.active.is_(True)).order_by(CountGroup.position.asc(), CountGroup.id.asc())
        ).scalars().all()
        if not active_groups:
            for idx, campaign in enumerate(active_campaigns[:4]):
                group = CountGroup(name=(campaign.category_filter or campaign.label), position=idx, active=True)
                db.add(group)
                db.flush()
                db.add(CountGroupCampaign(group_id=group.id, campaign_id=campaign.id))
            db.flush()

        first_group = db.execute(
            select(CountGroup).where(CountGroup.active.is_(True)).order_by(CountGroup.position.asc(), CountGroup.id.asc())
        ).scalars().first()

        rotation = db.execute(select(StoreRotationState).where(StoreRotationState.store_id == store.id)).scalar_one_or_none()
        if not rotation:
            db.add(StoreRotationState(store_id=store.id, next_group_id=first_group.id if first_group else None))
        else:
            if first_group and not rotation.next_group_id:
                rotation.next_group_id = first_group.id

        manager = db.execute(select(Principal).where(Principal.username == 'manager')).scalar_one_or_none()
        if not manager:
            db.add(
                Principal(
                    username='manager',
                    password_hash=hash_password('managerpass'),
                    role=PrincipalRole.ADMIN,
                    store_id=None,
                    active=True,
                )
            )

        lead = db.execute(select(Principal).where(Principal.username == 'lead1')).scalar_one_or_none()
        if not lead:
            db.add(
                Principal(
                    username='lead1',
                    password_hash=hash_password('leadpass'),
                    role=PrincipalRole.LEAD,
                    store_id=None,
                    active=True,
                )
            )

        store_user = db.execute(select(Principal).where(Principal.username == 'store1')).scalar_one_or_none()
        if not store_user:
            db.add(
                Principal(
                    username='store1',
                    password_hash=hash_password('storepass'),
                    role=PrincipalRole.STORE,
                    store_id=store.id,
                    active=True,
                )
            )

        db.commit()


if __name__ == '__main__':
    try:
        decision = demo_seed_decision(
            environment=settings.environment_normalized,
            enabled=settings.demo_seed_enabled,
        )
        if decision == 'disabled':
            print(
                f'Demo seed disabled for environment {settings.environment_normalized!r}; '
                'set DEMO_SEED_ENABLED=true in a non-production environment to opt in.'
            )
        else:
            seed()
            print('Demo seed deliberately enabled; example data inserted/verified.')
    except DemoSeedRefused as exc:
        print(f'REFUSED: {exc}')
        raise SystemExit(2) from exc
