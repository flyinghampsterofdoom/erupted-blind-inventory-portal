"""Shared employee entry points; Scheduling compatibility routes use these same handlers.

The existing scheduling feature gate and employee-management capability are retained
for this release. Domain mutations continue through their existing services.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal
from app.db import get_db
from app.routers.v2_scheduling import (
    feature_access, preferences_access, scheduling_employees_page, employee_policy_page,
    save_employee_policy_page, _form_back,
)
from app.security.csrf import verify_csrf
from app.services.employee_profile_service import (
    can_manage_employee_accounts, save_employee_account, save_personal_information,
)

router = APIRouter(prefix='/v2/hr/employees', tags=['v2-employees'])
# These are aliases of the original endpoints, not a second directory/editor.
router.add_api_route('', scheduling_employees_page, methods=['GET'])
router.add_api_route('/{employee_id}', employee_policy_page, methods=['GET'])
router.add_api_route('/{employee_id}', save_employee_policy_page, methods=['POST'])


@router.post('/{employee_id}/personal')
async def save_employee_personal(employee_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    path = f'/v2/hr/employees/{employee_id}'
    try:
        save_personal_information(db, actor=principal, employee_id=employee_id, values=await request.form())
        db.commit()
        return _form_back(path, message='Personal information saved.')
    except (ValueError, PermissionError, SQLAlchemyError):
        db.rollback()
        return _form_back(path, error='Personal information could not be saved. Check the name, email and field lengths.')


@router.post('/{employee_id}/account')
async def save_employee_login(employee_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(preferences_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    if not can_manage_employee_accounts(db, principal):
        raise HTTPException(status_code=403)
    path = f'/v2/hr/employees/{employee_id}'
    form = await request.form()
    try:
        save_employee_account(db, actor=principal, employee_id=employee_id,
            action=str(form.get('action', '')), role=str(form.get('role', 'STORE')),
            store_id=int(form['store_id']) if form.get('store_id') else None,
            password=str(form.get('password', '')), login=str(form.get('login', '')),
            confirmed=form.get('confirm_identity') == 'true')
        db.commit()
        return _form_back(path, message='Employee account saved. Scheduling settings were preserved.')
    except (ValueError, PermissionError) as exc:
        db.rollback()
        return _form_back(path, error=str(exc))
    except SQLAlchemyError:
        db.rollback()
        return _form_back(path, error='Account could not be saved. The login or employee link may already be in use.')
