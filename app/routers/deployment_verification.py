"""Authenticated owner-only GET checks; no deployment controls or new tokens."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import get_db
from app.routers.purchasing import owner
from app.services.runtime_identity_service import capture_identity

router = APIRouter(prefix='/management/deployment', tags=['deployment verification'])


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
