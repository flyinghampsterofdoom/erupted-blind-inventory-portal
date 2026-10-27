"""Loopback-only synthetic fixture for scheduling_emergency.js."""
import os, sys, json, socket
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
os.chdir(ROOT);sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
OUT=Path(os.environ['SCHEDULING_EVIDENCE_DIR']);OUT.mkdir(parents=True,exist_ok=True)
from sqlalchemy.engine import make_url
if make_url(os.environ['TEST_POSTGRES_ADMIN_URL']).host not in ('127.0.0.1','localhost','::1'):
    raise RuntimeError('Disposable loopback PostgreSQL is required')
os.environ['DATABASE_URL']=os.environ['TEST_POSTGRES_ADMIN_URL']
from pytest import MonkeyPatch
from test_v2_scheduling_foundation import scheduling_db, date_hours
from test_v2_primary import primary, PASSWORD
from test_scheduling_lifecycle_integrity import complete, values, DAY
from test_scheduling_emergency_coverage import owner
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_service import update_shift, create_shift, ShiftInput
from datetime import time
import uvicorn
mp=MonkeyPatch();a=scheduling_db.__wrapped__();seed=next(a);b=primary.__wrapped__(seed,mp);p=next(b)
pid=p.account('LEAD');actor=Principal(pid,'person@example.test',Role.LEAD,None,True)
with p.Session() as db:
    period,rows=complete(db,p.manager,p.ids);account=owner(db)
    extra=[m.Store(name=n,square_location_id='SYNTHETIC-'+n,active=True) for n in ('Third','Fourth')]
    db.add_all(extra);db.flush();stores=(p.ids['north'],p.ids['south'],extra[0].id,extra[1].id)
    for sid in stores:date_hours(db,p.manager,sid,DAY)
    update_shift(db,principal=actor,schedule_period_id=period.id,shift_id=rows[0].id,expected_version=period.version,
        values=values(rows[0],employee_id=None,owner_principal_id=account.id,start_time=time(9),end_time=time(12)),
        allowed_store_ids=stores,manual_edit=True)
    for employee,owner_id,sid,start,end in [(None,account.id,p.ids['south'],time(12),time(17)),(None,None,extra[0].id,time(9),time(17)),(p.ids['blair'],None,extra[1].id,time(9),time(17))]:
        create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,
            values=ShiftInput(employee,sid,DAY,start,end,owner_principal_id=owner_id),allowed_store_ids=stores,manual_edit=True)
    db.commit()
(OUT/'local.json').write_text(json.dumps({'url':'http://127.0.0.1:8793','username':'person@example.test','password':PASSWORD,
    'week':str(period.week_start_date),'day':str(DAY),'employee':p.ids['blair']}))
original=socket.socket.connect
def connect(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        raise RuntimeError('External network is blocked in the synthetic browser fixture')
    return original(self,address)
socket.socket.connect=connect
try:uvicorn.run(p.app,host='127.0.0.1',port=8793,log_level='warning')
finally:
    socket.socket.connect=original
    for fixture in (b,a):
        try:next(fixture)
        except StopIteration:pass
    mp.undo()
