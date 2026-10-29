"""Durable isolated Square contract simulator. No HTTP and no real credentials."""
from copy import deepcopy
from datetime import datetime,timezone
from sqlalchemy import text
from .isolation import require_isolated
from .identity import digest
from . import worksheets
from .receiving_square import API_VERSION,WRITE_PATH,HISTORY_PATH
from app.services.square_request_policy import enforce_square_request_policy


def install(engine):
    with engine.begin() as c:
        require_isolated(c)
        c.execute(text('CREATE SCHEMA IF NOT EXISTS purchasing_v4_simulator'))
        c.execute(text('''CREATE TABLE IF NOT EXISTS purchasing_v4_simulator.adjustment(
          command_id uuid PRIMARY KEY,payload_digest char(64) NOT NULL,payload jsonb NOT NULL,change jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now())'''))
        c.execute(text("ALTER TABLE purchasing_v4_simulator.adjustment ADD COLUMN IF NOT EXISTS additional_changes jsonb NOT NULL DEFAULT '[]'::jsonb"))
        c.execute(text('''CREATE TABLE IF NOT EXISTS purchasing_v4_simulator.fault(
          command_id uuid PRIMARY KEY,mode text NOT NULL CHECK(mode IN ('LOST','BEFORE','HIDDEN','OK')))'''))


class FixtureInventoryTransport:
    headers={'Square-Version':API_VERSION}
    isolated_simulator=True
    def __init__(self,engine):
        self.engine=engine
        with engine.connect() as c:
            require_isolated(c)
            if not c.execute(text("SELECT to_regclass('purchasing_v4_simulator.adjustment')")).scalar_one():raise PermissionError('Explicit fixture simulator installation required; live transport is unavailable')
    def post(self,path,payload,*,inventory_quantity_write=False):
        enforce_square_request_policy('POST',path,payload,inventory_quantity_write=inventory_quantity_write)
        if path==HISTORY_PATH:
            with self.engine.connect() as c:
                require_isolated(c)
                rows=c.execute(text('''SELECT a.change,a.additional_changes FROM purchasing_v4_simulator.adjustment a
                  LEFT JOIN purchasing_v4_simulator.fault f ON f.command_id=a.command_id
                  WHERE coalesce(f.mode,'OK')<>'HIDDEN' AND a.change->'adjustment'->>'catalog_object_id'=ANY(:variations)
                  AND (a.change->'adjustment'->>'from_location_id'=ANY(:locations) OR a.change->'adjustment'->>'to_location_id'=ANY(:locations)) ORDER BY a.created_at'''),dict(variations=payload['catalog_object_ids'],locations=payload['location_ids'])).all()
                changes=[change for row in rows for change in [row[0],*row[1]]];start=int(payload.get('cursor','0'));page=changes[start:start+1000]
                return dict(changes=page,**({'cursor':str(start+1000)} if start+1000<len(changes) else {}))
        if path!=WRITE_PATH:raise PermissionError('Simulator supports only the qualified inventory endpoints')
        key=payload['idempotency_key'];mode='OK'
        with self.engine.begin() as c:
            require_isolated(c);mode=c.execute(text('SELECT mode FROM purchasing_v4_simulator.fault WHERE command_id=:id'),{'id':key}).scalar_one_or_none() or 'OK'
            if mode=='BEFORE':raise ConnectionError('Simulated transport failure before remote application; local outcome remains unknown')
            prior=c.execute(text('SELECT payload_digest FROM purchasing_v4_simulator.adjustment WHERE command_id=:id'),{'id':key}).scalar_one_or_none()
            if prior and prior!=digest(payload):raise ValueError('Simulated Square idempotency key payload mismatch')
            if not prior:
                changes=deepcopy(payload['changes'])
                for i,change in enumerate(changes):
                    change['adjustment']['id']='SIMULATED-'+key+('-'+str(i) if i else '')
                    change['adjustment']['created_at']=datetime.now(timezone.utc).isoformat()
                c.execute(text('INSERT INTO purchasing_v4_simulator.adjustment(command_id,payload_digest,payload,change,additional_changes) VALUES (:id,:digest,CAST(:payload AS jsonb),CAST(:change AS jsonb),CAST(:additional AS jsonb))'),dict(id=key,digest=digest(payload),payload=worksheets.js(payload),change=worksheets.js(changes[0]),additional=worksheets.js(changes[1:])))
        if mode=='LOST':raise ConnectionError('Simulated Square applied the adjustment; response was lost')
        return dict(counts=[],isolated_simulator=True)
