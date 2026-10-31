"""Explicit isolated composition of the accepted V4 workspace and legacy application.

Production app.main imports this only for the explicit owner inspection binding.
Deployment capabilities default off;
they do not replace owner business configuration or grant production authority.
"""
from dataclasses import dataclass,asdict


@dataclass(frozen=True)
class Capabilities:
    workspace:bool=False
    configuration:bool=False
    generation:bool=False
    authorization:bool=False
    receiving:bool=False
    inventory:bool=False
    email:bool=False

    def __post_init__(self):
        if any(type(v) is not bool for v in asdict(self).values()):raise ValueError('Explicit boolean deployment capabilities required')
        if not self.workspace and any(v for k,v in asdict(self).items() if k!='workspace'):raise ValueError('An enabled capability requires its workspace')
        if self.inventory and not self.receiving:raise ValueError('Inventory posting requires authorized receiving')


def create_app(isolated_url,*,source_namespace,owner_principal_id,capabilities=Capabilities(),inventory_adapter=None,provider=None,webhook_secret=None,source_collector=None,target=None,supplier_resolution_enabled=False):
    from .isolation import require_isolated_url
    from sqlalchemy.engine import make_url
    if target is None:
        require_isolated_url(isolated_url)
        from .network_isolation import install
        install()
    else:
        target.verify_url(isolated_url)
        if capabilities not in (Capabilities(workspace=True),Capabilities(workspace=True,generation=True,configuration=True)):
            raise PermissionError('Owner mode permits inspection or explicitly enabled worksheet calculation only')
        if any(x is not None for x in (inventory_adapter,provider,webhook_secret,source_collector)):
            raise PermissionError('Owner inspection cannot configure external providers')
    # Dedicated-process construction prevents legacy dependencies using another DB.
    from app import db,main
    if make_url(db.engine.url)!=make_url(isolated_url):raise PermissionError('Legacy and V4 must share the exact explicit isolated database')
    app=main.app
    app.state.external_source_refresh_enabled=False
    if source_collector is not None:
        if not getattr(source_collector,'isolated_simulator',False):raise PermissionError('Isolated composition accepts an explicitly isolated source collector only')
        app.state.shared_source_collector=source_collector
    if getattr(app.state,'purchasing_v4',None) is not None:raise RuntimeError('V4 is already composed in this process')
    if not capabilities.workspace:return app
    from app.config import settings
    if settings.purchasing_owner_principal_id!=owner_principal_id:raise PermissionError('V3 and V4 must use the same explicitly configured purchasing owner')
    if (inventory_adapter is not None and not getattr(inventory_adapter,'isolated_simulator',False)) or (provider is not None and not getattr(provider,'isolated_simulator',False)):
        raise PermissionError('Phase9 integration accepts isolated simulated external effects only')
    if inventory_adapter is not None and not capabilities.inventory:raise ValueError('Posting adapter supplied while inventory capability is disabled')
    if provider is not None and not capabilities.email:raise ValueError('Mail adapter supplied while email capability is disabled')
    from fastapi import Depends,Request,HTTPException
    from fastapi.responses import JSONResponse,HTMLResponse,RedirectResponse
    from fastapi.routing import APIRoute
    from app.auth import get_current_principal
    from app.db import get_db
    from starlette.concurrency import run_in_threadpool
    from .email_app import create_app as email_app
    from .worksheet_app import create_app as worksheet_app
    from . import submission,receiving
    if target is not None:
        v4=worksheet_app(isolated_url,source_namespace=source_namespace,target=target)
        v4.state.receiving_login_allowed=lambda _principal_id:False
    else:
        v4=email_app(isolated_url,source_namespace=source_namespace,owner_principal_id=owner_principal_id,authorization_enabled=capabilities.authorization,receiving_enabled=capabilities.receiving,inventory_adapter=inventory_adapter,email_enabled=capabilities.email,provider=provider,webhook_secret=webhook_secret)
    if settings.daily_evidence_collector_enabled:
        def inventory_refresh(**values):
            collector=getattr(app.state,'shared_source_collector',None)
            method=getattr(collector,'inventory_only',None)
            if method is None:raise HTTPException(503,'Current Square inventory refresh is unavailable')
            return method(**values)
        v4.state.inventory_source_refresh=inventory_refresh
    app.state.purchasing_v4_legacy_registry_required=target is None
    app.state.purchasing_v4=v4;app.state.purchasing_v4_capabilities=capabilities
    app.state.purchasing_v4_entry_owner=owner_principal_id
    app.state.purchasing_v4_receiving_login_allowed=v4.state.receiving_login_allowed
    app.state.external_source_refresh_enabled=False
    # The generation closure belongs to the accepted worksheet layer's app object.
    # Factories extend the same FastAPI instance, so no alternate calculation route exists.
    v4.state.generation_enabled=capabilities.generation;v4.state.configuration_enabled=capabilities.configuration
    if type(supplier_resolution_enabled) is not bool or supplier_resolution_enabled and not capabilities.configuration:raise ValueError('Supplier resolution requires owner configuration access')
    v4.state.supplier_resolution_enabled=supplier_resolution_enabled
    app.state.additional_auth_exempt_paths=frozenset() if target is not None else frozenset({'/api/purchasing-email/resend/events'})

    def owner(principal=Depends(get_current_principal)):
        if target is not None and principal.id!=target.owner_principal_id:raise HTTPException(403,'Purchasing owner access required')
        with v4.state.engine.connect() as c:
            if target is None:submission.owner(c,principal.id)
            else:
                from .controls import owner as verify_owner
                verify_owner(c,principal.id)
        return principal.id

    def receiver(principal=Depends(get_current_principal)):
        with v4.state.engine.connect() as c:receiving.capability(c,principal.id,'VIEW',receiving.fixed_stores(c)[1]['id'])
        return principal.id

    app.dependency_overrides[v4.state.actor]=owner
    if hasattr(v4.state,'receiving_actor'):app.dependency_overrides[v4.state.receiving_actor]=receiver
    v4.dependency_overrides[v4.state.actor]=owner
    if hasattr(v4.state,'receiving_actor'):v4.dependency_overrides[v4.state.receiving_actor]=receiver
    legacy_home=next(r.endpoint for r in app.routes if getattr(r,'path',None)=='/' and 'GET' in getattr(r,'methods',()))
    app.router.routes[:]=[r for r in app.routes if not (getattr(r,'path',None)=='/' and 'GET' in getattr(r,'methods',()))]
    @app.get('/')
    def home(request:Request,legacy_db=Depends(get_db)):
        principal=get_current_principal(request)
        if principal.id==owner_principal_id:return RedirectResponse('/purchasing-v4'+('?' + request.url.query if request.url.query else ''),status_code=303)
        if v4.state.receiving_login_allowed(principal.id):return RedirectResponse('/receiving',status_code=303)
        return legacy_home(request,db=legacy_db)
    context=next(r.endpoint for r in v4.routes if getattr(r,'path',None)=='/api/context')
    def integrated_context(owner_id=Depends(owner)):
        base=context(owner_id=owner_id)
        if target is not None:base.update(email_enabled=False,receiving_enabled=False,quotation_enabled=False,submission_enabled=False,authorization_enabled=False)
        return dict(base,workspace_path='/purchasing-v4',management_path='/purchasing-v4/policies',legacy_orders_path='/management/ordering-tool',readiness_path='/purchasing-v4/readiness',generation_enabled=capabilities.generation,configuration_enabled=capabilities.configuration,supplier_resolution_enabled=v4.state.supplier_resolution_enabled,inventory_refresh_enabled=callable(getattr(v4.state,'inventory_source_refresh',None)))
    paths={'/':'/purchasing-v4','/management':'/purchasing-v4/policies'}
    for route in v4.routes:
        path=getattr(route,'path',None)
        if path in ('/login','/logout','/openapi.json'):continue
        if path=='/api/context':continue
        if path in paths:
            route=APIRoute(paths[path],route.endpoint,methods=['GET'],response_class=HTMLResponse,include_in_schema=False,dependency_overrides_provider=app)
        app.router.routes.insert(0,route)
    app.add_api_route('/api/context',integrated_context,methods=['GET'],include_in_schema=False)
    app.router.on_startup.extend(v4.router.on_startup);app.router.on_shutdown.extend(v4.router.on_shutdown)
    if hasattr(v4.state,'mail_schedule'):app.middleware('http')(v4.state.mail_schedule)
    legacy_handlers=dict(app.exception_handlers)
    for kind,handler in v4.exception_handlers.items():
        previous=next((legacy_handlers[base] for base in kind.__mro__ if base in legacy_handlers),None)
        async def dispatch_error(request,exc,_handler=handler,_previous=previous):
            if request.url.path.startswith(('/api/','/purchasing-v4','/receiving')):return await _handler(request,exc)
            if _previous is not None:return await _previous(request,exc)
            raise exc
        app.add_exception_handler(kind,dispatch_error)

    if target is not None:
        import re
        v4_paths=frozenset(getattr(route,'path','') for route in v4.routes)
        @app.middleware('http')
        async def owner_inspection_gate(request:Request,call_next):
            path=request.url.path
            if request.method not in ('GET','HEAD','OPTIONS') and (path.startswith('/api/') or path.startswith('/purchasing-v4')):
                manual=(path in ('/api/worksheets','/api/worksheets/discard-selected','/api/worksheet-preferences')
                        or re.fullmatch(r'/api/worksheets/[0-9a-fA-F-]+/(commands|add|submission-preview|inventory-refresh)',path))
                owned=any(re.fullmatch(re.sub(r'\{[^}]+\}',r'[^/]+',p),path) for p in v4_paths if p.startswith('/api/'))
                configurable=capabilities.configuration and (path in configuration_paths or re.fullmatch(r'/api/worksheets/[0-9a-fA-F-]+/(no-reorder|supplier-resolution)',path))
                comparison=capabilities.configuration and path in ('/api/manual-comparisons','/api/manual-comparisons/preview')
                if owned and not manual and not configurable and not comparison:return JSONResponse({'detail':'This capability is paused for owner inspection'},status_code=503)
            return await call_next(request)

    configuration_paths={'/api/commands','/api/suppliers','/api/restore','/api/adopt-legacy-manual','/api/use-automatic','/api/manual-comparisons/confirm'}

    @app.get('/api/purchasing-v4/reconciliation')
    async def reconciliation(owner_id=Depends(owner)):
        from .historical_reconciliation import report
        from .isolation import readonly_snapshot
        from .calculation_serialization import primitive
        def read():
            with readonly_snapshot(v4.state.engine) as c:
                if target is None:return primitive(report(c))
                from sqlalchemy import text
                return dict(mode='owner inspection',public_purchase_orders=c.scalar(text('SELECT count(*) FROM public.purchase_orders')),imported_variations=c.scalar(text('SELECT count(*) FROM purchasing_v4.variation_reference')),retained_manual_records=c.scalar(text('SELECT count(*) FROM purchasing_v4.legacy_manual_evidence')),commitments_enabled=False)
        return await run_in_threadpool(read)

    @app.get('/api/purchasing-v4/evidence-status')
    async def evidence_status(owner_id=Depends(owner)):
        from .evidence_qualification import operational_status
        from .isolation import readonly_snapshot
        from .calculation_serialization import primitive
        def read():
            with readonly_snapshot(v4.state.engine) as c:return primitive(operational_status(c))
        return await run_in_threadpool(read)
    @app.middleware('http')
    async def capability_gate(request:Request,call_next):
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.url.path in configuration_paths and not capabilities.configuration:return JSONResponse({'detail':'Purchasing configuration changes are paused'},status_code=503)
            if not capabilities.inventory and request.url.path.startswith('/api/receiving/') and (request.url.path.endswith('/inventory') or request.url.path.startswith('/api/receiving/inventory/')):return JSONResponse({'detail':'Inventory posting is paused; original commands remain retained'},status_code=503)
        return await call_next(request)

    @app.get('/purchasing-v4/readiness',response_class=HTMLResponse)
    def readiness(owner_id=Depends(owner)):
        with v4.state.engine.connect() as c:
            schema=c.exec_driver_sql('SELECT version_num FROM purchasing_v4.alembic_version').scalar_one()
        from html import escape
        rows=''.join('<tr><th>'+escape(k.title())+'</th><td>'+(('Available for owner inspection' if target is not None else 'Enabled in isolation') if value else 'Paused')+'</td></tr>' for k,value in asdict(capabilities).items())
        mode=('Owner-only calculations. An owner action and approved settings are required. Purchasing automation, commitments, receiving, inventory posting and email remain paused.' if capabilities.generation else 'Owner inspection. Automatic generation, commitments, receiving, inventory posting and email are paused.') if target is not None else 'Isolated development. Production cutover is not authorized.'
        return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Purchasing V4 readiness</title><main><h1>Purchasing V4 · release qualification</h1><p>'+mode+'</p><p><a href="/purchasing-v4">Purchasing worksheet</a> · <a href="/purchasing-v4/policies">Policies and suppliers</a> · <a href="/management/ordering-tool">Legacy purchase orders</a> · <a href="/receiving">HWY 99 receiving</a></p><p><a href="/api/purchasing-v4/reconciliation">Historical reconciliation · read only</a> · <a href="/api/purchasing-v4/evidence-status">Source evidence status · read only</a></p><p>Private schema: '+escape(schema)+'</p><table>'+rows+'</table><p>Automatic forecasting requires qualified source evidence and owner-approved numerical settings. Live email requires dedicated provider and DNS qualification. Existing PO writers and commitments remain authoritative.</p></main></html>'
    return app


def main():
    import argparse,os
    p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--owner-id',type=int,required=True);p.add_argument('--port',type=int,default=58461)
    for name in asdict(Capabilities()):p.add_argument('--enable-'+name,action='store_true')
    p.add_argument('--simulate-email',action='store_true');p.add_argument('--simulate-square',action='store_true');a=p.parse_args()
    from .isolation import require_isolated_url
    require_isolated_url(a.isolated_url);os.environ['DATABASE_URL']=a.isolated_url;os.environ['ENVIRONMENT']='test'
    os.environ['PURCHASING_OWNER_PRINCIPAL_ID']=str(a.owner_id)
    capabilities=Capabilities(**{name:getattr(a,'enable_'+name) for name in asdict(Capabilities())})
    provider=adapter=None;secret=None
    if a.simulate_email:
        from sqlalchemy import create_engine
        from .email_simulator import FixtureResend
        provider=FixtureResend(create_engine(a.isolated_url));secret='whsec_'+__import__('base64').b64encode(b'isolated-fixture-webhook-key-only').decode()
    if a.simulate_square:
        from sqlalchemy import create_engine
        from .receiving_simulator import FixtureInventoryTransport
        from .receiving_square import InventoryAdapter
        adapter=InventoryAdapter(FixtureInventoryTransport(create_engine(a.isolated_url)))
        # Contract marker is local composition evidence, never a live adapter option.
        adapter.isolated_simulator=True
    import uvicorn
    uvicorn.run(create_app(a.isolated_url,source_namespace=a.source_namespace,owner_principal_id=a.owner_id,capabilities=capabilities,inventory_adapter=adapter,provider=provider,webhook_secret=secret),host='127.0.0.1',port=a.port)

if __name__=='__main__':main()
