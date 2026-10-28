"""Optional SELECT-only receiving stages over the one accepted evidence contract."""
from collections import defaultdict
from datetime import datetime,timezone
from sqlalchemy import text
from .receiving_square import instant
from .contracts import QuantityEvidence,EvidenceState


def incoming_sql(c,legacy_sql):
    if not c.execute(text("SELECT to_regclass('purchasing_v4.receiving_supply')")).scalar_one():return legacy_sql
    # Original V3 semantics remain unchanged; adopted V4 facts have one projection.
    base=legacy_sql.rsplit(' ORDER BY',1)[0]
    base=base.replace("WHERE p.status", "WHERE NOT EXISTS(SELECT 1 FROM purchasing_v4.receiving_order ro WHERE ro.purchase_order_id=p.id) AND p.status")
    return base+''' UNION ALL SELECT id,purchase_order_id,variation_id,sku,ordered_qty,received_qty_total,
      store_id,allocated_qty,store_received_qty,closed FROM purchasing_v4.receiving_supply ORDER BY id,store_id'''


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
