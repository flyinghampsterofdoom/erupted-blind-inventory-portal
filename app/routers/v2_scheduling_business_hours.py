import logging

from datetime import date, datetime, time, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.auth import get_current_principal
from app.db import get_db
from app.models import StoreSpecialHour
from app.security.csrf import verify_csrf
from app.routers.v2 import V2Page
from app.routers.v2_scheduling import feature_access,scheduling_write_lock,_simple_page_context,_form_back
from app.v2.store_scope import list_authorized_stores
from app.services.v2_scheduling_business_hours_service import require_admin,save_business_hours,restore_normal_hours,assignment_impacts
from app.services.v2_scheduling_service import SchedulingValidationError,SchedulingConflict
from app.services.v2_scheduling_lead_duty_service import business_today
from app.dependencies import get_client_ip

logger = logging.getLogger(__name__)

router=APIRouter(prefix='/v2/scheduling/business-hours',tags=['v2-scheduling'],
    dependencies=[Depends(feature_access),Depends(scheduling_write_lock)])


def admin_access(principal=Depends(get_current_principal),db:Session=Depends(get_db)):
    try:require_admin(db,principal)
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
    return principal


@router.get('')
def page(request:Request,edit_id:int|None=None,principal=Depends(admin_access),db:Session=Depends(get_db)):
    stores=list_authorized_stores(db,principal);ids=tuple(s.id for s in stores)
    today=business_today(db)
    rows=list(db.scalars(select(StoreSpecialHour).where(StoreSpecialHour.store_id.in_(ids),
        StoreSpecialHour.active.is_(True),StoreSpecialHour.calendar_date>=today-timedelta(days=14))
        .order_by(StoreSpecialHour.calendar_date,StoreSpecialHour.store_id)))
    edit=db.get(StoreSpecialHour,edit_id) if edit_id else None
    if edit_id and (edit is None or not edit.active or edit.store_id not in ids):raise HTTPException(status_code=404)
    names={s.id:s.name for s in stores}
    return request.app.state.templates.TemplateResponse('v2/scheduling/business_hours.html',
        _simple_page_context(request,principal,page=V2Page('scheduling/business-hours','Business hours',
        'Set a dated closure or different hours.',route_path='/v2/scheduling/business-hours',
        badge='V2 Scheduling',active_prefix='/v2/scheduling/business-hours'),
        available_stores=stores,exceptions=[{'row':r,'store_name':names[r.store_id],
        'impacts':assignment_impacts(db,r)} for r in rows],edit=edit,edit_store=names.get(edit.store_id) if edit else None))


@router.post('')
async def save(request:Request,principal=Depends(admin_access),db:Session=Depends(get_db),_csrf=Depends(verify_csrf)):
    form=await request.form();path='/v2/scheduling/business-hours'
    try:
        stores=list_authorized_stores(db,principal);allowed=tuple(s.id for s in stores)
        scope=str(form.get('scope','selected'))
        if scope not in ('selected','all'):raise SchedulingValidationError('Choose selected stores or all stores.')
        ids=allowed if scope=='all' else tuple(int(v) for v in form.getlist('store_ids'))
        mode=str(form.get('mode',''))
        if mode not in ('closed','hours'):raise SchedulingValidationError('Choose Closed all day or Different hours.')
        rows=save_business_hours(db,principal=principal,store_ids=ids,allowed_store_ids=allowed,
            calendar_date=date.fromisoformat(str(form.get('calendar_date',''))),event_name=str(form.get('event_name','')),
            closed_all_day=mode=='closed',
            opening_time=time.fromisoformat(str(form['opening_time'])) if mode=='hours' and form.get('opening_time') else None,
            closing_time=time.fromisoformat(str(form['closing_time'])) if mode=='hours' and form.get('closing_time') else None,
            staffing_note=str(form.get('staffing_note','')),
            edit_id=int(form['edit_id']) if form.get('edit_id') else None,
            expected_updated_at=datetime.fromisoformat(str(form['expected_updated_at'])) if form.get('expected_updated_at') else None,
            ip=get_client_ip(request))
        db.commit()
        return _form_back(path,message=f'Business hours saved for {len(rows)} store(s). Review affected schedules below; existing assignments were preserved.')
    except (ValueError,KeyError,SchedulingValidationError,SchedulingConflict) as exc:
        db.rollback();return _form_back(path,error=str(exc) if not isinstance(exc,(ValueError,KeyError)) else 'Check the date, times and store selection.')
    except PermissionError as exc:
        db.rollback();raise HTTPException(status_code=403,detail=str(exc)) from exc
    except SQLAlchemyError:
        logger.exception('Business hours update failed')
        db.rollback();return _form_back(path,error='Business hours could not be saved. Reload and check for another update.')


@router.post('/{exception_id}/restore')
async def restore(exception_id:int,request:Request,principal=Depends(admin_access),db:Session=Depends(get_db),_csrf=Depends(verify_csrf)):
    form=await request.form();path='/v2/scheduling/business-hours'
    try:
        restore_normal_hours(db,principal=principal,exception_id=exception_id,
            expected_updated_at=datetime.fromisoformat(str(form.get('expected_updated_at',''))),
            allowed_store_ids=tuple(s.id for s in list_authorized_stores(db,principal)),ip=get_client_ip(request))
        db.commit();return _form_back(path,message='Normal business hours restored. Existing assignments were preserved; review affected schedules.')
    except (ValueError,SchedulingValidationError,SchedulingConflict) as exc:
        db.rollback();return _form_back(path,error=str(exc))
    except PermissionError as exc:
        db.rollback();raise HTTPException(status_code=403,detail=str(exc)) from exc
    except SQLAlchemyError:
        logger.exception('Business hours update failed')
        db.rollback();return _form_back(path,error='This exception could not be updated. Reload before retrying.')
