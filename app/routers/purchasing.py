"""Owner-only Purchasing V3 preparation and existing authorized store receiving."""
from __future__ import annotations
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import Principal
from app.db import get_db
from app.models import PurchasingSession, PurchasingSessionLine
from app.routers.management import ordering_access
from app.security.csrf import verify_csrf
from app.services import purchasing_session_service as purchasing

router=APIRouter(prefix='/management/purchasing',tags=['purchasing'])


def owner(principal: Principal=Depends(ordering_access),db: Session=Depends(get_db)):
    try:purchasing.authorize(db,principal.id)
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
    return principal


def response(request, template, context):
    from app.routers.v2 import V2Page, _visible_navigation
    common={'request':request,'new_key':lambda:str(uuid4()),
        'page':V2Page('ordering','Purchasing','Recommendations, owner decisions and purchase orders.',
            permission='ordering.manage',route_path='/management/purchasing',badge='Purchasing V3'),
        'navigation':_visible_navigation(request),'scope_locked':True,'scope_caption':'Purchasing scope',
        'store_scope_label':'Active stores','hide_preview_banner':True}
    return request.app.state.templates.TemplateResponse(template,{**common,**context})


def redirect(session_id):return RedirectResponse(f'/management/purchasing/sessions/{session_id}',status_code=303)


def failure(db, exc):
    db.rollback()
    raise HTTPException(status_code=404 if isinstance(exc,LookupError) else 400,detail=str(exc)) from exc


@router.get('')
def home(request: Request,principal: Principal=Depends(owner),db: Session=Depends(get_db)):
    history=db.scalars(select(PurchasingSession).where(PurchasingSession.owner_id==principal.id)
        .order_by(PurchasingSession.updated_at.desc()).limit(20)).all()
    previous=next((session for session in history if session.status=='ACTIVE'),None)
    return response(request,'purchasing/home.html',{'previous':previous,'history':history,'principal':principal})


@router.post('/generate')
async def generate(request: Request,principal: Principal=Depends(owner),db: Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        session=purchasing.generate(db,actor_id=principal.id,request_key=str(form.get('request_key','')),
            settings_={name:form.get(name,default) for name,default in [('reorder_weeks',5),('target_weeks',10),('history_days',84)]})
        db.commit()
    except (ValueError,LookupError,RuntimeError) as exc:return failure(db,exc)
    return redirect(session.id)


@router.get('/sessions/{session_id}')
def session_view(session_id:int,request:Request,principal: Principal=Depends(owner),db: Session=Depends(get_db)):
    try:
        workspace=purchasing.table_workspace(db,session_id=session_id,actor_id=principal.id)
        query=str(request.query_params.get('q',''))
        view={'principal':principal,'workspace':workspace,'query':query,'session':db.get(PurchasingSession,session_id),
            'search_results':purchasing.catalog_search(db,session_id=session_id,actor_id=principal.id,query=query) if query else []}
        return response(request,'purchasing/session.html',view)
    except (ValueError,LookupError) as exc:return failure(db,exc)


@router.post('/sessions/{session_id}/refresh')
async def refresh(session_id:int,request:Request,principal: Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        purchasing.refresh(db,session_id=session_id,actor_id=principal.id,request_key=str(form.get('request_key','')));db.commit()
    except (ValueError,LookupError,RuntimeError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/lines/{line_id}')
async def update(session_id:int,line_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        existing=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session_id,PurchasingSessionLine.id==line_id))
        if not existing:raise LookupError('Session product not found')
        supplied=int(str(form.get('quantity',existing.quantity)))
        supplier=int(str(form.get('supplier_id',existing.supplier_id or '0')))
        # An unchanged automatic quantity alongside a supplier change is not a
        # new owner quantity override; it must receive the new supplier multiple.
        quantity=None if supplied==existing.quantity and supplier!=existing.supplier_id and not existing.decisions.get('quantity_override') else supplied
        allocations={key[7:]:int(str(value)) for key,value in form.items() if str(key).startswith('alloc__')} if form.get('use_allocations') else None
        purchasing.update_line(db,session_id=session_id,line_id=line_id,actor_id=principal.id,quantity=quantity,
            supplier_id=supplier,allocations=allocations,accept_proposal=form.get('action')=='accept',
            acknowledge=bool(form.get('acknowledge')),cost=str(form.get('cost','')).strip() or None,
            expected_version=int(str(form.get('version','0'))));db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/lines/{line_id}/remove')
async def remove(session_id:int,line_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        purchasing.remove_line(db,session_id=session_id,line_id=line_id,actor_id=principal.id,undo=form.get('undo')=='1');db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/add')
async def add(session_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        purchasing.add_product(db,session_id=session_id,actor_id=principal.id,variation_id=str(form.get('variation_id','')),
            quantity=int(str(form.get('quantity','0'))),supplier_id=int(str(form.get('supplier_id','0'))),
            cost=str(form.get('cost','')).strip() or None);db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/groups/{supplier_id}/discard')
async def discard(session_id:int,supplier_id:int,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    try:purchasing.discard_group(db,session_id=session_id,supplier_id=supplier_id,actor_id=principal.id);db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/groups/{supplier_id}/account')
async def account(session_id:int,supplier_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    form=await request.form()
    try:
        purchasing.set_account(db,session_id=session_id,supplier_id=supplier_id,actor_id=principal.id,
            account_id=int(str(form.get('account_id','0')) or '0') or None);db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return redirect(session_id)


@router.post('/sessions/{session_id}/groups/{supplier_id}/po')
async def create_po(session_id:int,supplier_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    try:
        form=await request.form()
        if form.get('version') is not None:
            saved=purchasing._session(db,session_id,principal.id)
            if saved.version!=int(str(form['version'])):raise ValueError('This session changed; reload and review before creating a PO')
        po=purchasing.create_po(db,session_id=session_id,supplier_id=supplier_id,actor_id=principal.id);db.commit()
    except (ValueError,LookupError) as exc:return failure(db,exc)
    return RedirectResponse(f'/management/ordering-tool/orders/{po.id}',status_code=303)


@router.get('/sessions/{session_id}/workspace')
def workspace_data(session_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    values=request.query_params
    try:
        return purchasing.table_workspace(db,session_id=session_id,actor_id=principal.id,
            supplier=int(values['supplier']) if 'supplier' in values else None,
            query=values.get('q',''),store=values.get('store') or None,sort=values.get('sort','need_desc'),
            page=int(values.get('page',1)),page_size=int(values.get('page_size',50)),state=values.get('state','active'))
    except (ValueError,LookupError) as exc:return failure(db,exc)


@router.get('/sessions/{session_id}/catalog')
def catalog_data(session_id:int,q:str='',principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    try:
        products=purchasing.catalog_search(db,session_id=session_id,actor_id=principal.id,query=q)
        return {'products':[{'id':p['variation_id'],'name':p['item_name'],'variant':p.get('variation_name',''),
            'sku':p.get('sku','')} for p in products]}
    except (ValueError,LookupError) as exc:return failure(db,exc)


@router.patch('/sessions/{session_id}/draft')
async def save_draft(session_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    try:
        values=await request.json()
        if not isinstance(values,dict) or type(values.get('version')) is not int:
            raise ValueError('A saved session version is required')
        session=purchasing._session(db,session_id,principal.id)
        if session.version!=values['version']:
            db.rollback()
            return JSONResponse({'detail':'Another save changed this session. Review the saved draft before reapplying your edits.',
                'conflict':True,'version':session.version},status_code=409)
        kind=values.get('kind','line');changes=values.get('changes',{})
        if not isinstance(changes,dict):raise ValueError('Invalid draft changes')
        if kind=='line':
            if set(changes)-{'quantity','supplier_id','allocations','cost','acknowledge','accept_proposal'}:
                raise ValueError('Unsupported draft field')
            for field in ('quantity','supplier_id'):
                if field in changes and type(changes[field]) is not int:raise ValueError('Enter a whole number')
            if 'allocations' in changes and (not isinstance(changes['allocations'],dict) or any(type(v) is not int for v in changes['allocations'].values())):
                raise ValueError('Allocations must contain whole numbers')
            purchasing.update_line(db,session_id=session_id,line_id=int(values['line_id']),actor_id=principal.id,
                expected_version=values['version'],**changes)
        elif kind in ('remove','undo'):
            purchasing.remove_line(db,session_id=session_id,line_id=int(values['line_id']),actor_id=principal.id,undo=kind=='undo')
        elif kind=='account':
            purchasing.set_account(db,session_id=session_id,supplier_id=int(values['supplier_id']),actor_id=principal.id,
                account_id=int(changes.get('account_id') or 0) or None)
        else:raise ValueError('Unsupported draft action')
        result={'version':session.version,'status':session.status}
        db.commit()
        return result
    except (ValueError,LookupError,KeyError,TypeError) as exc:
        db.rollback()
        return JSONResponse({'detail':str(exc)},status_code=404 if isinstance(exc,LookupError) else 400)


@router.get('/sessions/{session_id}/draft')
def saved_decisions(session_id:int,line_ids:str='',principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    try:
        session=purchasing._session(db,session_id,principal.id,lock=False)
        ids=[int(value) for value in line_ids.split(',') if value][:100]
        lines=db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session_id,
            PurchasingSessionLine.id.in_(ids))).all()
        return {'version':session.version,'status':session.status,'lines':{
            str(line.id):{'quantity':line.quantity,'supplier_id':line.supplier_id,'allocations':line.allocations,
                'cost':str(line.unit_cost) if line.unit_cost is not None else None,'removed':line.removed} for line in lines}}
    except (ValueError,LookupError) as exc:return failure(db,exc)


@router.get('/orders/{purchase_order_id}/receiving')
def receiving_view(purchase_order_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    from app.services import purchase_order_admin_service as receiving
    try:
        workspace=receiving.receiving_workspace(db,purchase_order_id=purchase_order_id,actor_principal_id=principal.id)
        return response(request,'purchasing/receiving.html',{'workspace':workspace,'principal':principal})
    except ValueError as exc:return failure(db,exc)


@router.post('/orders/{purchase_order_id}/shipments')
async def submit_shipment(purchase_order_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    from app.services import purchase_order_admin_service as receiving
    try:
        data=await request.json()
        if not isinstance(data,dict) or not isinstance(data.get('cells'),list) or len(data['cells'])>20000:
            raise ValueError('Invalid shipment count')
        cells={}
        for cell in data['cells']:
            if not isinstance(cell,list) or len(cell)!=3 or any(type(value) is not int for value in cell):
                raise ValueError('Store quantities must be whole numbers')
            line,store,quantity=cell
            if (line,store) in cells:raise ValueError('Duplicate receiving cell')
            cells[line,store]=quantity
        key=data.get('request_key')
        if not isinstance(key,str) or not key or len(key)>100:raise ValueError('A shipment identity is required')
        result=receiving.submit_receiving_shipment(db,purchase_order_id=purchase_order_id,
            received_qty_by_line_store=cells,actor_principal_id=principal.id,request_key=key)
        return result
    except (ValueError,LookupError,KeyError,TypeError) as exc:
        db.rollback();return JSONResponse({'detail':str(exc)},status_code=400)
    except Exception:
        db.rollback();return JSONResponse({'detail':'Submission could not be confirmed. Keep this count and retry the same shipment; check receiving history. Saved quantities will not be counted twice.'},status_code=503)


@router.post('/orders/{purchase_order_id}/shipments/{receipt_id}/retry')
def retry_shipment(purchase_order_id:int,receipt_id:int,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    from app.services import purchase_order_admin_service as receiving
    try:return receiving.receive_purchase_order(db,purchase_order_id=purchase_order_id,actor_principal_id=principal.id,receipt_id=receipt_id)
    except (ValueError,RuntimeError) as exc:return failure(db,exc)


@router.post('/orders/{purchase_order_id}/close-shortage')
async def close_shortage(purchase_order_id:int,request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    from app.services import purchase_order_admin_service as receiving
    try:
        form=await request.form();key=str(form.get('request_key',''))
        if not key or len(key)>64:raise ValueError('A closure identity is required')
        receiving.close_receiving_shortages(db,purchase_order_id=purchase_order_id,actor_principal_id=principal.id,request_key=key);db.commit()
    except ValueError as exc:return failure(db,exc)
    return RedirectResponse(f'/management/purchasing/orders/{purchase_order_id}/receiving?closed=1',status_code=303)


@router.get('/payments')
def monday_payments_page(request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    from app.services import purchasing_payments_service as payments
    try:
        view=payments.workspace(db,actor_id=principal.id,monday=request.query_params.get('monday'))
        return response(request,'purchasing/payments.html',{'payments':view,'principal':principal})
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
    except (ValueError,RuntimeError) as exc:return failure(db,exc)

@router.get('/payments/workspace')
def monday_payments_data(request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    from app.services import purchasing_payments_service as payments
    try:return payments.workspace(db,actor_id=principal.id,monday=request.query_params.get('monday'))
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
    except (ValueError,RuntimeError) as exc:return failure(db,exc)

@router.post('/payments')
async def save_monday_payments(request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    from app.services import purchasing_payments_service as payments
    try:
        payload=await request.json()
        if set(payload)-{'monday','amounts','request_key','token','corrects','note'}:raise ValueError('Unexpected payment fields')
        batch=payments.save(db,actor_id=principal.id,**payload)
        db.commit()
        return {'saved':True,'batch_id':batch.id,'request_key':batch.request_key,'monday':str(batch.monday)}
    except payments.Conflict as exc:
        db.rollback();return JSONResponse({'detail':str(exc)},status_code=409)
    except PermissionError as exc:
        db.rollback();raise HTTPException(status_code=403,detail=str(exc)) from exc
    except (ValueError,TypeError,RuntimeError) as exc:return failure(db,exc)


@router.get('/attention')
def attention_page(request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    return response(request,'purchasing/attention.html',{'principal':principal})


@router.get('/attention/workspace')
def attention_data(principal:Principal=Depends(owner),db:Session=Depends(get_db)):
    from app.services import purchasing_attention_service as attention
    try:return attention.workspace(db,actor_id=principal.id)
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
    except (ValueError,RuntimeError,OSError) as exc:return failure(db,exc)


@router.post('/attention')
async def attention_save(request:Request,principal:Principal=Depends(owner),db:Session=Depends(get_db),_:None=Depends(verify_csrf)):
    from app.services import purchasing_attention_service as attention
    try:
        payload=await request.json()
        if set(payload)!={'request_key','changes'}:raise ValueError('Expected request identity and decisions')
        result=attention.save(db,actor_id=principal.id,**payload);db.commit();return result
    except attention.Conflict as exc:
        db.rollback();return JSONResponse({'detail':str(exc)},status_code=409)
    except PermissionError as exc:
        db.rollback();raise HTTPException(status_code=403,detail=str(exc)) from exc
    except (ValueError,RuntimeError,TypeError,KeyError) as exc:return failure(db,exc)
