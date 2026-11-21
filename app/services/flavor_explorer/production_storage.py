"""Production Product Explorer adapter over the existing portal PostgreSQL engine.

No URL, development path, DDL, fixtures or identity provisioning are accepted.
All inherited domain writes share one transaction-scoped cross-process lock.
"""
from contextlib import contextmanager
from sqlalchemy import select,text
from app.services.flavor_explorer.storage import FlavorStorage
from app.services.flavor_explorer.production_schema import metadata,verify_schema,EXPLORER_LOCK

class PostgresStorage(FlavorStorage):
    def __init__(self,engine):
        if engine.dialect.name!="postgresql":raise ValueError("The existing PostgreSQL application engine is required.")
        self.engine=engine;self.metadata=metadata();self.path=None

    def close(self):
        # app.db owns this shared pool; closing a projection must not dispose it.
        pass

    def table(self,name):
        if name=="touchscreen_flavors":name="fi_flavor_identities"
        return self.metadata.tables[name]

    @contextmanager
    def _write(self):
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":EXPLORER_LOCK})
            yield connection

    def mirror_principal(self,**_):
        raise PermissionError("Production principals are managed by the existing portal.")
    def mirror_store(self,**_):
        raise PermissionError("Production stores are managed by the existing portal.")

    def mirror_category(self,*,category_id,name,slug,category_type="BROAD"):
        values={"id":category_id,"name":name,"slug":slug,"category_type":category_type}
        with self.engine.connect() as connection:
            row=connection.execute(select(self.table("touchscreen_flavor_categories")).where(
                self.table("touchscreen_flavor_categories").c.id==category_id)).mappings().first()
            if not row or any(row[key]!=value for key,value in values.items()):
                raise ValueError("Production taxonomy identity differs; existing categories were not modified.")
        return category_id
