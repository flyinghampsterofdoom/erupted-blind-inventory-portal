"""Owned PostgreSQL fixture: CI URL schema or confined temporary cluster.

Never connects to the protected workstation service, creates another database,
or changes existing production objects. All local cluster bytes live in TMPDIR.
"""
import os
import sys
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

def admitted_test_endpoint(url):
    """Admit only the real qualified Mac fixture or the isolated CI runner."""
    if url.drivername!="postgresql+psycopg" or url.query:return False
    if url.host not in {"localhost","127.0.0.1"}:return False
    hosted_ci=(sys.platform=="linux" and os.getuid()==2001 and
               Path.home()==Path("/home/ci-tests") and os.getenv("ENVIRONMENT")=="test" and
               not os.getenv("AVA_BOUNDED_SESSION"))
    if hosted_ci:
        return (url.host,url.port,url.username,url.password,url.database)==(
            "127.0.0.1",5433,"postgres","ci_only","postgres")
    return url.port==55486

@pytest.fixture(scope="module")
def explorer_pg_base():
    supplied=os.getenv("TEST_POSTGRES_ADMIN_URL")
    if supplied:
        url=make_url(supplied)
        if not admitted_test_endpoint(url):
            pytest.fail("Explorer PostgreSQL permits only the explicitly owned local or CI loopback fixture.")
        engine=create_engine(url,pool_pre_ping=True)
        try:
            with engine.connect() as c:c.execute(text("SELECT 1"))
            yield engine
        finally:engine.dispose()
        return
    if not os.getenv("CI") and not os.getenv("AVA_BOUNDED_SESSION","").startswith("AVA-ERUPTED-"):
        pytest.skip("Use the qualified release launcher or explicit CI PostgreSQL fixture.")
    binaries=Path("/opt/homebrew/opt/postgresql@16/bin")
    initdb=shutil.which("initdb") or str(binaries/"initdb")
    postgres=shutil.which("postgres") or str(binaries/"postgres")
    if not Path(initdb).is_file() or not Path(postgres).is_file():
        pytest.skip("PostgreSQL fixture binaries or TEST_POSTGRES_ADMIN_URL are required.")
    root=Path(tempfile.mkdtemp(prefix="explorer-pg-",dir=os.getenv("TMPDIR")))
    data=root/"cluster";log=root/"postgres.log"
    subprocess.run([initdb,"-D",str(data),"-U","explorer_fixture","--auth=trust","--no-locale","-E","UTF8"],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    handle=log.open("wb")
    process=subprocess.Popen([postgres,"-D",str(data),"-h","127.0.0.1","-p","55486","-k","",
        "-c","shared_buffers=8MB","-c","max_connections=12","-c","work_mem=1MB",
        "-c","fsync=off","-c","full_page_writes=off","-c","max_wal_size=64MB"],
        stdout=handle,stderr=subprocess.STDOUT)
    engine=create_engine("postgresql+psycopg://explorer_fixture@127.0.0.1:55486/postgres",pool_pre_ping=True)
    try:
        deadline=time.monotonic()+15
        while True:
            if process.poll() is not None:raise RuntimeError("Owned PostgreSQL fixture failed to start.")
            try:
                with engine.connect() as c:c.execute(text("SELECT 1"))
                break
            except Exception:
                if time.monotonic()>deadline:raise RuntimeError("Owned PostgreSQL fixture startup timed out.") from None
                time.sleep(.1)
        yield engine
    finally:
        engine.dispose()
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        handle.close()
        shutil.rmtree(root)

@pytest.fixture
def explorer_pg_engine(explorer_pg_base):
    schema="explorer_fixture_"+uuid.uuid4().hex
    assert re.fullmatch(r"explorer_fixture_[a-f0-9]{32}",schema)
    with explorer_pg_base.begin() as c:c.exec_driver_sql('CREATE SCHEMA "'+schema+'"')
    engine=create_engine(explorer_pg_base.url,connect_args={"options":"-c search_path="+schema},
        pool_size=4,max_overflow=0,pool_pre_ping=True)
    try:
        with engine.begin() as c:
            c.exec_driver_sql("""CREATE TYPE principal_role AS ENUM('ADMIN','MANAGER','LEAD','STORE');
                CREATE TABLE stores(id BIGINT PRIMARY KEY,name TEXT NOT NULL,square_location_id TEXT,active BOOLEAN NOT NULL DEFAULT true);
                CREATE TABLE principals(id BIGINT PRIMARY KEY,username TEXT NOT NULL,role principal_role NOT NULL,
                    active BOOLEAN NOT NULL,store_id BIGINT REFERENCES stores(id));
                CREATE TABLE touchscreen_flavor_categories(id BIGINT PRIMARY KEY,name TEXT NOT NULL,slug TEXT NOT NULL,
                    category_type VARCHAR(16) NOT NULL,display_order INT NOT NULL DEFAULT 0,is_active BOOLEAN NOT NULL DEFAULT true);
                CREATE TABLE touchscreen_flavors(id BIGINT PRIMARY KEY,brand_name TEXT NOT NULL,display_name TEXT NOT NULL,
                    slug TEXT NOT NULL,short_description TEXT NOT NULL,created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
                    updated_by_principal_id BIGINT NOT NULL REFERENCES principals(id));
                CREATE TABLE principal_permission_overrides(id BIGSERIAL PRIMARY KEY,principal_id BIGINT REFERENCES principals(id),
                    permission_key TEXT NOT NULL,allowed BOOLEAN NOT NULL);
                CREATE TABLE role_permission_overrides(id BIGSERIAL PRIMARY KEY,role principal_role NOT NULL,
                    permission_key TEXT NOT NULL,allowed BOOLEAN NOT NULL);
                INSERT INTO stores VALUES(4,'Synthetic Highway99','FIXTURE-HWY',true),(9,'Synthetic SR503','FIXTURE-SR',true);
                INSERT INTO principals VALUES(1,'Synthetic admin','ADMIN',true,NULL),(2,'Synthetic staff','STORE',true,4),
                    (3,'Synthetic Lead','LEAD',true,9),(4,'Synthetic inactive','ADMIN',false,NULL);
                INSERT INTO touchscreen_flavors VALUES(77,'Retained','Legacy fixture','retained','Retained description',1,1);""")
            c.execute(text("INSERT INTO touchscreen_flavor_categories(id,name,slug,category_type) VALUES(:id,:name,:slug,:kind)"),
                [{"id":n,"name":"Synthetic taxonomy"+str(n),"slug":"fruity" if n==1 else "mint-menthol" if n==7 else "note-"+str(n),
                  "kind":"BROAD" if n<=7 else "FRUIT"} for n in range(1,28)])
        yield engine
    finally:
        engine.dispose()
        with explorer_pg_base.begin() as c:c.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')

@pytest.fixture
def explorer_pg_storage(explorer_pg_engine):
    from app.services.flavor_explorer.production_schema_v0058 import upgrade
    from app.services.flavor_explorer.production_storage import PostgresStorage
    with explorer_pg_engine.begin() as c:upgrade(c)
    yield PostgresStorage(explorer_pg_engine)
