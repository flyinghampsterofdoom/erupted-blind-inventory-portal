"""Explicit owner-inspection target, scoped to a constructed V4 engine."""
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class OwnerInspectionTarget:
    database_name: str
    hostname: str
    owner_principal_id: int
    database_user: str

    def verify_url(self, value):
        url = make_url(value) if isinstance(value, str) else value
        if (url.get_backend_name() != 'postgresql' or not self.database_name or not self.hostname
                or url.database != self.database_name or url.host != self.hostname
                or url.username != self.database_user or type(self.owner_principal_id) is not int
                or self.owner_principal_id <= 0
                or {'host','hostaddr','port','dbname','service','servicefile'}.intersection(url.query)):
            raise PermissionError('Owner inspection database target mismatch')
        return url

    def verify(self, connection):
        self.verify_url(connection.engine.url)
        database, user, recovery = connection.execute(text(
            'SELECT current_database(),current_user,pg_is_in_recovery()')).one()
        if (database,user,recovery) != (self.database_name,self.database_user,False):
            raise PermissionError('Owner inspection server identity mismatch')
        if connection.scalar(text('SELECT version_num FROM public.alembic_version')) != '20261027_0053':
            raise RuntimeError('Owner inspection requires qualified public schema 0053')
        if not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM public.principals WHERE id=:id AND role::text='ADMIN' AND active)"), {'id':self.owner_principal_id}):
            raise PermissionError('Configured purchasing owner is not active')


def attach(engine, target):
    if not isinstance(target, OwnerInspectionTarget):
        raise TypeError('Explicit owner inspection target required')
    target.verify_url(engine.url)
    engine._purchasing_v4_owner_target = target
    return engine
