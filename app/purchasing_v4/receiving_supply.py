"""Optional SELECT-only receiving stages over the one accepted evidence contract."""
from collections import defaultdict
from datetime import datetime,timezone
from sqlalchemy import text
from .receiving_square import instant
from .contracts import QuantityEvidence,EvidenceState


def native_incoming_sql():
    return """SELECT s.*, EXISTS (
      SELECT 1 FROM purchasing_v4.receiving_event e
      CROSS JOIN LATERAL jsonb_array_elements(e.payload->'lines') entry
      WHERE e.purchase_order_id=s.purchase_order_id AND e.payload->>'direct'='true'
        AND (entry->>'line_id')::bigint=s.id
    ) AS worksheet_direct FROM purchasing_v4.receiving_supply s"""


def incoming_sql(c,legacy_sql):
    legacy=legacy_sql.replace(" AS closed", """ AS closed, EXISTS (
      SELECT 1 FROM public.purchase_order_receipts receipt JOIN public.purchase_order_receipt_lines rl ON rl.receipt_id=receipt.id
      WHERE rl.purchase_order_line_id=l.id AND receipt.request_payload->>'worksheet_version' IS NOT NULL
    ) AS worksheet_direct""",1)
    if not c.execute(text("SELECT to_regclass('purchasing_v4.receiving_supply')")).scalar_one():return legacy
    base=legacy.rsplit(' ORDER BY',1)[0]
    base=base.replace("WHERE p.status", "WHERE NOT EXISTS(SELECT 1 FROM purchasing_v4.receiving_order ro WHERE ro.purchase_order_id=p.id) AND p.status")
    return base+''' UNION ALL SELECT id,purchase_order_id,variation_id,sku,ordered_qty,received_qty_total,
      store_id,allocated_qty,store_received_qty,closed,worksheet_direct FROM ('''+native_incoming_sql()+') received ORDER BY id,store_id'


def incoming_remainder(rows):
    """Project actual outstanding units for both legacy and worksheet receipts.

    A store can receive more than its original split. Reuse the established
    weighting for the remaining units without rewriting PO or receipt facts.
    The caller validates line/store totals before using this projection.
    """
    remaining={row['store_id']:max(0,row['allocated_qty']-row['store_received_qty']-row['closed']) for row in rows}
    total=max(0,rows[0]['ordered_qty']-rows[0]['received_qty_total']-sum(row['closed'] for row in rows))
    if sum(remaining.values())>total:
        from app.services.purchase_order_math_service import allocate_purchase
        return allocate_purchase(total,remaining)
    return remaining


def posting_membership(c,variations=None):
    output=defaultdict(list)
    if not c.execute(text("SELECT to_regclass('purchasing_v4.receiving_posting_membership')")).scalar_one():return output
    sql='SELECT * FROM purchasing_v4.receiving_posting_membership'
    if variations is not None:sql+=' WHERE variation_id=ANY(:variations)'
    for row in c.execute(text(sql),dict(variations=variations)).mappings():
        value=dict(row);proof=value.pop('remote_proof',None) or {};components=proof.get('changes',[proof]);times=[]
        for change in components:
            adjustment=change.get('adjustment',{}) if isinstance(change,dict) else {}
            try:
                remote_time=instant(adjustment.get('calculated_at') or adjustment['created_at'])
                times.append(remote_time)
            except (ValueError,TypeError,KeyError,AttributeError):times.append(None)
        value['remote_received_at']=max(times) if times and all(t is not None for t in times) else None
        output[(row['variation_id'],row['store_id'])].append(value)
    return output


def qualify_inventory(observation,memberships,count_row,*,as_of):
    if not memberships:return observation
    reasons=[]
    for op in memberships:
        if (op['state']!='VERIFIED' or op['updated_at']>as_of or not count_row or
            op.get('remote_received_at') is None or op['remote_received_at']>as_of or
            count_row.get('square_location_id')!=op['square_location_id'] or
            count_row.get('source_calculated_at') is None or count_row.get('started_at') is None or
            count_row['started_at']<op['updated_at'] or count_row['source_calculated_at']<op['remote_received_at']):reasons.append('UNRECONCILED_V4_INVENTORY_OPERATION:'+op['command_id'])
    if not reasons:return observation
    return QuantityEvidence(EvidenceState.UNKNOWN,None,observation.source,';'.join(reasons),observation.observed_at,observation.references+tuple('v4-inventory:'+x['command_id'] for x in memberships))
