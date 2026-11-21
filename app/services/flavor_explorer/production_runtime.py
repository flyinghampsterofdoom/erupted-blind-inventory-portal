"""Bind the staff Product Explorer to the existing portal and durable PG authority."""
from sqlalchemy import select,func
from app.services.flavor_explorer.production_storage import PostgresStorage,verify_schema
from app.services.flavor_explorer.production_originals import ExistingOriginals
from app.services.flavor_explorer.portal_api import PortalService
from app.services.flavor_explorer.presentation import ProjectionCache
from app.services.flavor_explorer.cache import _project_customer_catalog

class ProductExplorerRuntime:
    def __init__(self,storage,catalog,originals):
        self.storage=storage;self.catalog=catalog;self.originals=originals
        self.projection=ProjectionCache(self.project,self.versions)

    def project(self):
        result=_project_customer_catalog(self.catalog.snapshot(),self.storage,
            product_enricher=self.originals.attach)
        result.setdefault("meta",{}).update(assignedStoreId="hwy99",locationSelectionAllowed=True,staffOnly=True)
        return result

    def versions(self):
        names=("fi_catalog_observations","fi_publication_revisions","fi_discovery_changes",
               "fi_product_photo_events","fi_notes","touchscreen_flavor_categories","stores")
        result=[]
        with self.storage.engine.connect() as c:
            for name in names:
                table=self.storage.table(name)
                if name=="fi_catalog_observations":
                    result.append(c.execute(select(table.c.generation).where(table.c.id==1)).scalar_one())
                elif name in {"fi_notes","touchscreen_flavor_categories","stores"}:
                    result.append(tuple(tuple(str(v) for v in row) for row in c.execute(select(table).order_by(table.c.id))))
                else:result.append(c.execute(select(func.max(table.c.id))).scalar() or 0)
        return (*result,self.originals.version())

def install_runtime(app,engine,*,catalog=None,start_worker=True):
    verify_schema(engine)
    storage=PostgresStorage(engine)
    if catalog is None:
        from app.services.flavor_explorer.production_catalog import ProductionCatalog
        catalog=ProductionCatalog(storage)
    originals=ExistingOriginals(engine)
    runtime=ProductExplorerRuntime(storage,catalog,originals)
    app.state.flavor_intelligence=PortalService(storage,catalog.snapshot,product_enricher=originals.attach)
    from app.services.flavor_explorer.compatibility.store import CompatibilityService
    app.state.product_compatibility=CompatibilityService(storage,catalog_loader=catalog.identity,stock_loader=catalog.stock)
    app.state.product_explorer=runtime
    if start_worker:catalog.start()
    return runtime
