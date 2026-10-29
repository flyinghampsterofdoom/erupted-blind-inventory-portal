"""Resolve the one shared movement projection; old schemas remain readable."""
from sqlalchemy import text


def movement_table(connection):
    for schema in ('purchasing_evidence','purchasing_v4'):
        if connection.execute(text(f"SELECT to_regclass('{schema}.shared_inventory_events')")).scalar_one_or_none():return schema+'.shared_inventory_events'
    return 'public.purchasing_inventory_events'


def source_failed(connection,legacy,*,as_of):
    """Latest completed shared source attempt supersedes older legacy refresh status.

    This does not mark financial import/attribution complete. A failed collector
    attempt fails closed until a subsequent successful source publication.
    """
    failed=bool(legacy and legacy.get('last_result')=='FAILED')
    schema=next((s for s in ('purchasing_evidence','purchasing_v4') if connection.execute(text(f"SELECT to_regclass('{s}.collection_attempt')")).scalar_one_or_none()),None)
    if schema is None:return failed
    latest=connection.execute(text(f'''SELECT status,completed_at FROM {schema}.collection_attempt
      WHERE completed_at<=:asof ORDER BY completed_at DESC,started_at DESC,id DESC LIMIT 1'''),{'asof':as_of}).mappings().first()
    if latest and (not legacy or legacy.get('last_attempted_at') is None or latest['completed_at']>=legacy['last_attempted_at']):
        return latest['status']=='FAILED'
    return failed
