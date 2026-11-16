"""Opt-in owner ADMIN purchasing on the canonical production PO/receiving path.

No provider, inventory adapter, startup migration, role grant or implicit owner
assignment. The isolated Phase 5 PO ownership/receiving stack stays isolated.
"""
from dataclasses import dataclass
from uuid import uuid4,uuid5,UUID
import hashlib
from sqlalchemy import text
from sqlalchemy.orm import Session
from .production_target import OwnerInspectionTarget
from . import submission,controls,worksheets,outbox


@dataclass(frozen=True)
class ProductionSubmissionTarget(OwnerInspectionTarget):
    def verify(self,connection):
        super().verify(connection)
        if connection.scalar(text("SELECT to_regclass('purchasing_v4.production_submission_contract')")) is None:
            raise RuntimeError('Production submission requires its separately approved ledger migration')
        row=connection.execute(text('SELECT version,owner_principal_id FROM purchasing_v4.production_submission_contract')).one_or_none()
        if row is None or tuple(row)!=(1,self.owner_principal_id):
            raise PermissionError('Production submission contract/owner mismatch')
        if connection.scalar(text("SELECT to_regclass('purchasing_v4.po_ownership')")):
            raise PermissionError('Production submission cannot silently take over the isolated PO writer registry')
        if connection.scalar(text('SELECT principal_id FROM purchasing_v4.commitment_owner'))!=self.owner_principal_id:
            raise PermissionError('Production submission owner does not match the configured ADMIN')
        role=connection.execute(text("SELECT NOT (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls),pg_has_role(session_user,oid,'MEMBER') FROM pg_roles WHERE rolname='erupted_v4_po_writer'")).one_or_none()
        if role is None or tuple(role)!=(True,True):
            raise PermissionError('Qualified production PO writer role and membership are required')
        if connection.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='v4_production_supply_lock' AND NOT tgisinternal"))!=7:
            raise RuntimeError('Production supply serialization guards are incomplete')


def authorize(connection,**values):
    # The existing authorizer retains review digests, explicit acknowledgment,
    # advisory locks, immutable receipts, reservations and worksheet sealing.
    result=submission.authorize(connection,queue_delivery=False,**values)
    if not result.get('replayed'):
        from app.services.v2_order_payments_service import initialize_new_order_if_configured
        from app.models import PurchaseOrder
        # Join the caller's transaction: a finance failure rolls back the entire
        # multi-vendor decision, including ledger, orders and worksheet seals.
        with Session(bind=connection,join_transaction_mode='create_savepoint') as db:
            for child in result['children']:
                if child['purchase_order_id'] is not None:
                    initialize_new_order_if_configured(db,order=db.get(PurchaseOrder,child['purchase_order_id']),actor_id=values['actor_id'])
            db.commit()
    return result


def documents(engine,*,actor_id,child_id):
    """Owner-requested immutable rendering only; no mail worker or delivery."""
    with engine.begin() as c:
        submission.owner(c,actor_id)
        row=c.execute(text("""SELECT x.*,s.state FROM purchasing_v4.integration_command x
            JOIN purchasing_v4.integration_state s ON s.command_id=x.id
            JOIN purchasing_v4.authorized_vendor v ON v.id=x.vendor_authorization_id
            JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id
            WHERE v.id=:id AND a.owner_id=:owner AND x.kind='GENERATE_DOCUMENTS'
            FOR UPDATE OF s"""),dict(id=controls.uuid(child_id),owner=actor_id)).mappings().one_or_none()
        if row is None:raise controls.InvalidPolicy('Unknown owner-authorized vendor order/request')
        command=str(row['id'])
        if row['state']=='ACKNOWLEDGED':
            originals=[dict(r) for r in c.execute(text('SELECT id::text,format,sha256,content FROM purchasing_v4.vendor_artifact WHERE command_id=:id ORDER BY format'),{'id':command}).mappings()]
            if {r['format'] for r in originals}!={'PDF','XLSX'} or any(hashlib.sha256(bytes(r['content'])).hexdigest()!=r['sha256'] for r in originals):
                raise controls.Conflict('Original vendor files are missing or corrupt; restore and reconcile the originals')
            return [{k:v for k,v in r.items() if k!='content'} for r in originals]
        if row['state']!='PENDING':raise controls.Conflict('Document rendering outcome requires reconciliation')
        if submission.digest(row['payload'])!=row['payload_digest']:raise controls.Conflict('Original vendor document payload failed integrity verification')
        from .vendor_documents import render,validate_projection
        validate_projection(row['payload']['document'])
        files=render(row['payload']['document'])
        lease=str(uuid4())
        c.execute(text('SET LOCAL ROLE erupted_v4_po_writer'))
        try:
            c.execute(text("UPDATE purchasing_v4.integration_state SET state='LEASED',lease_id=:lease,lease_until=now()+interval '5 minutes',version=version+1,updated_at=now() WHERE command_id=:id"),dict(id=command,lease=lease))
            for format,content in files.items():
                c.execute(text("""INSERT INTO purchasing_v4.vendor_artifact(id,vendor_authorization_id,command_id,format,sha256,content,public_projection)
                    VALUES (:id,:child,:command,:format,:sha,:content,CAST(:projection AS jsonb))"""),dict(id=str(uuid5(UUID(command),format)),child=child_id,command=command,format=format,sha=hashlib.sha256(content).hexdigest(),content=content,projection=worksheets.js(row['payload']['document'])))
            outbox.finish(c,command,lease,'ACKNOWLEDGED',dict(manual_document_render=True,no_delivery=True,lease_id=lease))
        finally:
            # A failed SQL statement aborts the transaction; the connection pool
            # rolls it back and restores SET LOCAL ROLE automatically.
            import sys
            if sys.exc_info()[0] is None:c.execute(text('RESET ROLE'))
        return [dict(r) for r in c.execute(text('SELECT id::text,format,sha256 FROM purchasing_v4.vendor_artifact WHERE command_id=:id ORDER BY format'),{'id':command}).mappings()]
