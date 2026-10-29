"""Resolve the one shared movement projection; old schemas remain readable."""
from sqlalchemy import text


def movement_table(connection):
    exists=connection.execute(text("SELECT to_regclass('purchasing_v4.shared_inventory_events')")).scalar_one_or_none()
    return 'purchasing_v4.shared_inventory_events' if exists else 'public.purchasing_inventory_events'


def source_failed(connection,legacy,*,as_of):
    """Latest completed shared source attempt supersedes older legacy refresh status.

    This does not mark financial import/attribution complete. A failed collector
    attempt fails closed until a subsequent successful source publication.
    """
    failed=bool(legacy and legacy.get('last_result')=='FAILED')
    if not connection.execute(text("SELECT to_regclass('purchasing_v4.collection_attempt')")).scalar_one_or_none():return failed
    latest=connection.execute(text('''SELECT status,completed_at FROM purchasing_v4.collection_attempt
      WHERE completed_at<=:asof ORDER BY completed_at DESC,started_at DESC,id DESC LIMIT 1'''),{'asof':as_of}).mappings().first()
    if latest and (not legacy or legacy.get('last_attempted_at') is None or latest['completed_at']>=legacy['last_attempted_at']):
        return latest['status']=='FAILED'
    return failed
