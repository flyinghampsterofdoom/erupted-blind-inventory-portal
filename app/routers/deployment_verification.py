"""Authenticated owner-only GET checks; no deployment controls or new tokens."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import get_db
from app.routers.purchasing import owner
from app.services.runtime_identity_service import capture_identity

router = APIRouter(prefix='/management/deployment', tags=['deployment verification'])


# Independent from browser sessions and the pre-existing telemetry key. A holder
# can only request this fixed aggregate observation, never become an Owner.
from threading import BoundedSemaphore
_observation_slot = BoundedSemaphore(1)


@router.get('/observation')
def observation(request: Request):
    from app.config import settings
    from app.services import deployment_observation_service as probe
    headers = {'Cache-Control': 'no-store, private'}
    if not settings.deployment_observation_public_key:
        return JSONResponse({'detail': 'Not found'}, status_code=404, headers=headers)
    timestamp = request.headers.get('X-Observation-Timestamp', '')
    nonce = request.headers.get('X-Observation-Nonce', '')
    signature = request.headers.get('X-Observation-Signature', '')
    if (request.query_params or len(timestamp) > 12 or len(signature) > 128
            or not probe.authorized(settings.deployment_observation_public_key, timestamp, nonce, signature)):
        return JSONResponse({'detail': 'Forbidden'}, status_code=403, headers=headers)
    if not _observation_slot.acquire(blocking=False):
        return JSONResponse({'detail': 'Observation busy'}, status_code=429, headers=headers)
    try:
        result = probe.observe(capture_identity(), getattr(request.app.state, 'runtime_identity', {}))
        result['nonce'] = nonce
        return JSONResponse(result, headers=headers)
    finally:
        _observation_slot.release()


@router.get('/runtime')
def runtime(request: Request, principal=Depends(owner), db: Session=Depends(get_db)):
    db.execute(text('SET TRANSACTION READ ONLY'))
    schema = db.scalar(text('SELECT version_num FROM alembic_version'))
    from app.services.purchase_order_math_service import ENGINE_VERSION
    identity = getattr(request.app.state, 'runtime_identity', {'status': 'UNVERIFIED'})
    current = capture_identity()
    intact = current.get('source_tree_sha256') == identity.get('source_tree_sha256') and current['status'] == 'VERIFIED'
    return JSONResponse({**identity, 'role': 'web', 'files_still_match': intact,
                         'schema': schema, 'engine_version': ENGINE_VERSION,
                         'owner_authorized': True, 'principal_id': principal.id},
                        headers={'Cache-Control': 'no-store, private'})


@router.get('/readiness')
def readiness(request: Request, principal=Depends(owner), db: Session=Depends(get_db)):
    """Owner-only readiness, independent of acceptance and external business APIs.

    Read-only DB transaction and actual source attestation. HTTP status alone is
    insufficient: callers must check this versioned body and exact identities.
    Square/R2 are functional integrations, not web/cron process prerequisites.
    """
    from sqlalchemy.exc import SQLAlchemyError
    from app.schema_contract import SUPPORTED_REVISIONS
    dependencies = {'process': True, 'database': False, 'schema': False, 'source': False}
    schema = None
    try:
        db.execute(text('SET TRANSACTION READ ONLY'))
        dependencies['database'] = db.scalar(text('SELECT 1')) == 1
        schema = db.scalar(text('SELECT version_num FROM alembic_version'))
        dependencies['schema'] = schema in SUPPORTED_REVISIONS
    except SQLAlchemyError:
        db.rollback()  # No SQL/error text or connection details in response.
    current = capture_identity()
    started = getattr(request.app.state, 'runtime_identity', {})
    dependencies['source'] = (current.get('status') == 'VERIFIED'
        and started.get('status') == 'VERIFIED'
        and current.get('revision') == started.get('revision')
        and current.get('source_tree_sha256') == started.get('source_tree_sha256'))
    ready = all(dependencies.values())
    return JSONResponse({'contract': 'erupted-readiness-v1', 'status': 'READY' if ready else 'NOT_READY',
        'dependencies': dependencies, 'schema': schema,
        'web': {k: current.get(k) for k in ('revision','source_tree_sha256','status','service_id')} | {'role':'web'}},
        status_code=200 if ready else 503, headers={'Cache-Control':'no-store, private'})
