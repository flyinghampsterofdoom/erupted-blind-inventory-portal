"""Sequential offload for request-owned PostgreSQL work.

SQLite's in-memory databases and default connections are thread-affine. Preserve
that local/test mode on its owning thread; hosted PostgreSQL work is awaited in
a worker and its request Session is never used concurrently.
"""
from starlette.concurrency import run_in_threadpool

async def run_db_work(function, db):
    if hasattr(db,'get_bind') and db.get_bind().dialect.name == 'sqlite':
        return function()
    return await run_in_threadpool(function)
