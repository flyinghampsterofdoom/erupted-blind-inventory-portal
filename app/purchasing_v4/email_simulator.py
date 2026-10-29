"""Durable Resend contract fixture. No HTTP, credentials, DNS or external email."""
from uuid import uuid5,UUID
from sqlalchemy import text
from .isolation import require_isolated
from . import worksheets
from .identity import digest
from .email_provider import Rejected

def install(engine):
 with engine.begin() as c:
  require_isolated(c)
  c.execute(text('CREATE SCHEMA IF NOT EXISTS purchasing_v4_email_simulator'))
  c.execute(text('CREATE TABLE IF NOT EXISTS purchasing_v4_email_simulator.sent(id text PRIMARY KEY,delivery_id uuid UNIQUE NOT NULL,payload jsonb NOT NULL,payload_digest char(64) NOT NULL,created_at timestamptz NOT NULL DEFAULT now())'))
  c.execute(text('CREATE TABLE IF NOT EXISTS purchasing_v4_email_simulator.message(id text PRIMARY KEY,payload jsonb NOT NULL,original bytea NOT NULL)'))
  c.execute(text('CREATE TABLE IF NOT EXISTS purchasing_v4_email_simulator.attachment(id text PRIMARY KEY,message_id text NOT NULL,filename text NOT NULL,content bytea NOT NULL)'))
  c.execute(text("CREATE TABLE IF NOT EXISTS purchasing_v4_email_simulator.fault(delivery_id uuid PRIMARY KEY,mode text CHECK(mode IN ('REJECT','LOST','BEFORE','OK')))"))

class FixtureResend:
 isolated_simulator=True
 def __init__(self,engine):
  self.engine=engine;self.calls=0
  with engine.connect() as c:require_isolated(c);assert c.execute(text("SELECT to_regclass('purchasing_v4_email_simulator.sent')")).scalar_one()
 def send(self,did,payload):
  self.calls+=1
  with self.engine.begin() as c:
   require_isolated(c);mode=c.execute(text('SELECT mode FROM purchasing_v4_email_simulator.fault WHERE delivery_id=:id'),{'id':did}).scalar_one_or_none()
   if mode=='REJECT':raise Rejected('Simulated explicit rejection')
   if mode=='BEFORE':raise ConnectionError('Simulated transport failure; externally unknown')
   pid=str(uuid5(UUID(did),'resend-fixture'))
   c.execute(text('INSERT INTO purchasing_v4_email_simulator.sent(id,delivery_id,payload,payload_digest) VALUES (:id,:did,CAST(:p AS jsonb),:hash) ON CONFLICT(delivery_id) DO NOTHING'),dict(id=pid,did=did,p=worksheets.js(payload),hash=digest(payload)))
   assert c.execute(text('SELECT payload_digest FROM purchasing_v4_email_simulator.sent WHERE delivery_id=:id'),{'id':did}).scalar_one()==digest(payload)
  if mode=='LOST':raise TimeoutError('Simulated provider acceptance then lost response')
  return pid
 def sent(self,pid):
  with self.engine.connect() as c:return dict(c.execute(text('SELECT payload FROM purchasing_v4_email_simulator.sent WHERE id=:id'),{'id':pid}).scalar_one(),id=pid,last_event='sent')
 def received(self,pid):
  with self.engine.connect() as c:return c.execute(text('SELECT payload FROM purchasing_v4_email_simulator.message WHERE id=:id'),{'id':pid}).scalar_one()
 def attachment(self,pid,aid):
  with self.engine.connect() as c:r=c.execute(text('SELECT filename,octet_length(content) AS size FROM purchasing_v4_email_simulator.attachment WHERE message_id=:pid AND id=:id'),dict(pid=pid,id=aid)).mappings().one()
  return dict(r,id=aid,download_url='fixture:attachment:'+aid)
 def download(self,url,limit):
  with self.engine.connect() as c:
   if url.startswith('fixture:message:'):b=bytes(c.execute(text('SELECT original FROM purchasing_v4_email_simulator.message WHERE id=:id'),{'id':url.split(':')[-1]}).scalar_one())
   elif url.startswith('fixture:attachment:'):b=bytes(c.execute(text('SELECT content FROM purchasing_v4_email_simulator.attachment WHERE id=:id'),{'id':url.split(':')[-1]}).scalar_one())
   else:raise PermissionError('Simulator has no network')
  if len(b)>limit:raise ValueError('Bound exceeded')
  return b
