"""A contended inventory authorization lock must not freeze every HTTP route."""
import asyncio
import threading
from types import SimpleNamespace

from starlette.requests import Request

from app.routers.store import operational_store_access


def test_waiting_inventory_scope_allows_other_requests_to_finish(monkeypatch):
    from app.services import inventory_working_store_service as service

    owner_thread = threading.get_ident()
    release_lock = threading.Event()
    entered = threading.Event()
    scoped = object()
    context = {'version': 'scheduled'}
    worker_threads = []

    def resolve(db, *, principal, selected_store_id):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release_lock.wait(2)
        return scoped, context

    monkeypatch.setattr(service, 'inventory_principal', resolve)
    # Bound the original bug so the regression fails rather than hanging.
    timer = threading.Timer(1, release_lock.set)
    timer.start()

    async def exercise():
        request = Request({'type': 'http', 'method': 'GET', 'headers': []})
        task = asyncio.create_task(operational_store_access(
            request, principal=object(), db=SimpleNamespace(info={})))
        await asyncio.sleep(.05)
        assert entered.is_set()
        assert not task.done(), 'row-lock wait blocked the event loop'
        # This models the owning request finishing its response/transaction.
        release_lock.set()
        assert await task is scoped
        assert request.state.inventory_context is context

    try:
        asyncio.run(exercise())
    finally:
        release_lock.set()
        timer.cancel()
    assert worker_threads and worker_threads[0] != owner_thread
