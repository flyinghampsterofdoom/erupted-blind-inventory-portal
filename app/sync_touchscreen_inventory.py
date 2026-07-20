from app.db import SessionLocal
from app.services.touchscreen_inventory_sync_service import synchronize_touchscreen_cache


def main() -> None:
    with SessionLocal() as db:
        run = synchronize_touchscreen_cache(db)
        print(f'Touchscreen sync {run.status}: {run.variation_count} variations, {run.inventory_record_count} inventory records')
        if run.status != 'SUCCEEDED':
            raise SystemExit(run.error_summary or 'Touchscreen synchronization failed')


if __name__ == '__main__':
    main()
