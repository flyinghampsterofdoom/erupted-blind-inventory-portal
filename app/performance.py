"""Opt-in numeric telemetry. Never capture parameters, URLs, users or payloads."""
import asyncio
from contextvars import ContextVar
from functools import wraps
import json
import logging
import random
from time import perf_counter
from uuid import uuid4
from sqlalchemy import event
from sqlalchemy.orm import Session
from app.config import settings

current = ContextVar('request_performance', default=None)
logger = logging.getLogger('erupted.performance')
logger.setLevel(logging.INFO)


def timed(stage, operation=None):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            sample = current.get()
            if sample is None:
                return function(*args, **kwargs)
            start = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = (perf_counter()-start)*1000
                sample[stage] = sample.get(stage, 0) + elapsed
                if operation:
                    logger.info(json.dumps({'event':'operation_performance','correlation_id':sample['correlation_id'],
                                            'operation':operation,'elapsed_ms':round(elapsed,3)}))
        return call
    return decorate


class PerformanceSession(Session):
    def _connection_for_bind(self, *args, **kwargs):
        sample=current.get()
        if sample is None:
            return super()._connection_for_bind(*args, **kwargs)
        start=perf_counter()
        try:return super()._connection_for_bind(*args, **kwargs)
        finally:sample['connection_ms'] += (perf_counter()-start)*1000


def instrument_engine(engine):
    @event.listens_for(engine,'before_cursor_execute')
    def before(connection,cursor,statement,parameters,context,many):
        if current.get() is not None:context._performance_start=perf_counter()
    @event.listens_for(engine,'after_cursor_execute')
    def after(connection,cursor,statement,parameters,context,many):
        sample=current.get();start=getattr(context,'_performance_start',None)
        if sample is not None and start is not None:
            sample['sql_count'] += 1;sample['sql_ms'] += (perf_counter()-start)*1000

    @event.listens_for(engine,'handle_error')
    def failed(exception_context):
        sample=current.get()
        context=exception_context.execution_context
        start=getattr(context,'_performance_start',None)
        if sample is not None and start is not None:
            sample['sql_count'] += 1;sample['sql_ms'] += (perf_counter()-start)*1000


class PerformanceMiddleware:
    def __init__(self, app):self.app=app
    async def __call__(self, scope, receive, send):
        if (scope['type']!='http' or not settings.performance_metrics_enabled
                or random.random() >= settings.performance_sample_rate):
            return await self.app(scope,receive,send)
        if not logger.handlers and not logging.getLogger().handlers:
            logger.addHandler(logging.StreamHandler())
        sample={'correlation_id':uuid4().hex,'sql_count':0,'sql_ms':0.,'connection_ms':0.,'event_loop_lag_max_ms':0.}
        token=current.set(sample);start=perf_counter()
        async def watch():
            while True:
                expected=asyncio.get_running_loop().time()+0.1
                await asyncio.sleep(0.1)
                sample['event_loop_lag_max_ms']=max(sample['event_loop_lag_max_ms'],
                    max(0.,(asyncio.get_running_loop().time()-expected)*1000))
        watcher=asyncio.create_task(watch())
        async def send_timed(message):
            if message['type']=='http.response.start':
                sample['status']=message['status']
                message={**message,'headers':list(message.get('headers',[]))+[(b'x-correlation-id',sample['correlation_id'].encode())]}
            await send(message)
        try:
            await self.app(scope,receive,send_timed)
        finally:
            watcher.cancel()
            try:await watcher
            except asyncio.CancelledError:pass
            sample['request_ms']=(perf_counter()-start)*1000
            # Route template only: no actual path, IDs, query string or headers.
            route=scope.get('route');sample['route']=getattr(route,'path','unmatched')
            logger.info(json.dumps({'event':'request_performance',**{k:round(v,3) if isinstance(v,float) else v for k,v in sample.items()}}))
            current.reset(token)
