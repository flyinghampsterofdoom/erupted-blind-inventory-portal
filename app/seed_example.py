from sqlalchemy import select

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


def seed() -> None:
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
                    role=PrincipalRole.MANAGER,
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
    seed()
    print('Seed data inserted/verified.')
