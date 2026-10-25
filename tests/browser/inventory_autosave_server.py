"""Local-only synthetic PostgreSQL fixture for inventory_autosave.js; never production."""
import os,sys,json,time,contextvars,socket
from pathlib import Path
W=Path(__file__).resolve().parents[2]
P=Path(os.environ['AUTOSAVE_EVIDENCE_DIR']).resolve();P.mkdir(parents=True,exist_ok=True)
os.chdir(W);sys.path[:0]=[str(W),str(W/'tests')]
from sqlalchemy.engine import make_url
admin_url=os.environ['TEST_POSTGRES_ADMIN_URL']
if make_url(admin_url).host not in ('127.0.0.1','localhost','::1'):raise RuntimeError('Browser qualification requires disposable loopback PostgreSQL')
os.environ['DATABASE_URL']=os.environ['TEST_POSTGRES_ADMIN_URL']
from pytest import MonkeyPatch
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary,PASSWORD
from test_employee_operational_context import seeded_current
from app.config import settings
from sqlalchemy import event,select,func
from app import models as m
from starlette.responses import JSONResponse
import uvicorn
mp=MonkeyPatch();mp.setattr(settings,'inventory_draft_encryption_key','synthetic-form-investigation-only-key-20261025')
a=scheduling_db.__wrapped__();seed=next(a);b=primary.__wrapped__(seed,mp);p=next(b)
pid,*_=seeded_current(p,two=True)
trace=contextvars.ContextVar('trace',default=None)
@event.listens_for(seed[3],'before_cursor_execute')
def before(conn,cursor,statement,parameters,context,executemany):context._investigation_start=time.perf_counter()
@event.listens_for(seed[3],'after_cursor_execute')
def after(conn,cursor,statement,parameters,context,executemany):
 t=trace.get()
 if t is not None:t['sql'].append({'verb':statement.split()[0],'ms':(time.perf_counter()-context._investigation_start)*1000,'batch':executemany})
class Measure:
 def __init__(self,app):self.app=app
 async def __call__(self,scope,receive,send):
  if scope['type']!='http':return await self.app(scope,receive,send)
  if scope['path']=='/__autosave_test__/state':
   from urllib.parse import parse_qs
   query=parse_qs(scope['query_string'].decode());kind=query['kind'][0];rid=int(query['id'][0]);models={'change-box-count':m.ChangeBoxCount,'daily-chore-sheet':m.DailyChoreSheet,'non-sellable-stock-take':m.NonSellableStockTake}
   with p.Session() as db:
    record=db.get(models[kind],rid);fields={'employee_name':[record.employee_name]};metadata={'change-box-count':'change_box_count_id','daily-chore-sheet':'daily_chore_sheet_id','non-sellable-stock-take':'non_sellable_stock_take_id'}[kind]
    if kind=='change-box-count':
     from app.services.change_box_count_service import ROLL_SIZES_BY_CODE
     for line in db.scalars(select(m.ChangeBoxCountLine).where(m.ChangeBoxCountLine.count_id==rid)):
      size=ROLL_SIZES_BY_CODE.get(line.denomination_code);fields['qty_loose__'+line.denomination_code]=[str(line.quantity%size if size else line.quantity)]
      if size:fields['qty_rolls__'+line.denomination_code]=[str(line.quantity//size)]
    elif kind=='non-sellable-stock-take':
     for line in db.scalars(select(m.NonSellableStockTakeLine).where(m.NonSellableStockTakeLine.stock_take_id==rid)):fields['qty__'+str(line.item_id)]=[format(line.quantity.normalize(),'f')]
    else:fields['completed_task_ids']=[str(row.task_id) for row in db.scalars(select(m.DailyChoreEntry).where(m.DailyChoreEntry.sheet_id==rid,m.DailyChoreEntry.completed.is_(True)))]
    actions=list(db.execute(select(m.AuditLog.action,func.count()).where(m.AuditLog.meta[metadata].as_integer()==rid).group_by(m.AuditLog.action)))
    result={'status':record.status.value,'fields':fields,'audit_counts':dict(actions)}
   return await JSONResponse(result)(scope,receive,send)
  row={'path':scope['path'],'method':scope['method'],'sql':[]};token=trace.set(row);start=time.perf_counter()
  async def sent(message):
   if message['type']=='http.response.start':row['status']=message['status']
   await send(message)
  try:await self.app(scope,receive,sent)
  finally:
   row['ms']=(time.perf_counter()-start)*1000;trace.reset(token)
   with (P/'server-requests.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
# Only loopback connections permitted after the synthetic fixture is built.
original=socket.socket.connect
def connect(self,address):
 if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):raise RuntimeError('Investigation blocked external socket')
 return original(self,address)
socket.socket.connect=connect
(P/'local.json').write_text(json.dumps({'url':'http://127.0.0.1:8789','username':'person@example.test','password':PASSWORD,'principal_id':pid}))
try:uvicorn.run(Measure(p.app),host='127.0.0.1',port=8789,log_level='warning')
finally:
 socket.socket.connect=original
 for fixture in (b,a):
  try:next(fixture)
  except StopIteration:pass
 mp.undo()
