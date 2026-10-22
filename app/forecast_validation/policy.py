"""One independently verified inventory policy for every shadow demand model."""
from fractions import Fraction as F
from math import ceil

VERSION='minmax-4-8-exact-v1'
def store_need(rate,row):
    if row.get('lifecycle','ACTIVE')!='ACTIVE' or not row.get('pack_known',True):
        return {'quantity':None,'reason':'LIFECYCLE_OR_PACK_UNRESOLVED','position':None}
    if rate is None or row.get('on_hand') is None or row.get('incoming') is None:
        return {'quantity':None,'reason':'DEMAND_OR_POSITION_UNKNOWN','position':None}
    position=F(row['on_hand'])+F(row['incoming']);v=F(rate)
    lead=row.get('lead_time_days') or 0
    level=ceil(v*max(28,lead));target=ceil(v*max(56,lead))
    need=ceil(max(F(0),target-position)) if position<=level else 0
    return {'quantity':need,'position':str(position),'reorder_level':level,'target':target,'triggered':position<=level,'version':VERSION}

def aggregate(store_results,pack,cost):
    if any(x['quantity'] is None for x in store_results):return {'quantity':None,'cost':None,'reason':'INCOMPLETE_STORE_REQUIREMENT'}
    qty=sum(x['quantity'] for x in store_results);rounded=((qty+pack-1)//pack)*pack
    return {'quantity':rounded,'unrounded':qty,'pack_surplus':rounded-qty,'cost':str(F(cost)*rounded) if cost is not None else None,'version':VERSION}
