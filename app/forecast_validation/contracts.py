"""Versioned evidence contract; raw records are retained, never replaced by a hash."""
from datetime import datetime,date,timedelta,timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import hashlib,json,re

TZ=ZoneInfo('America/Los_Angeles')
SCHEMA='forecast-evidence-v1'
MODELS=('A_RAW84','B_CURRENT','C_CONSERVATIVE','D_VERIFIED')
VERSION='audit-20261022-v1'
class EvidenceError(ValueError):pass
class Conflict(EvidenceError):pass

def stamp(value):
    if isinstance(value,str):
        value=re.sub(r'\.(\d+)',lambda m:'.'+(m[1]+'000000')[:6],value)
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None:raise EvidenceError('A timezone-aware timestamp is required')
    return value.astimezone(timezone.utc)

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def number(value,*,nullable=False,nonnegative=False):
    if value is None and nullable:return None
    if value is None or isinstance(value,bool):raise EvidenceError('Missing or invalid numeric evidence')
    try:v=Decimal(str(value))
    except Exception as exc:raise EvidenceError('Invalid numeric evidence') from exc
    if not v.is_finite() or nonnegative and v<0:raise EvidenceError('Nonfinite/negative evidence')
    return v

def validate(snapshot,*,now=None,max_rows=6000):
    if snapshot.get('schema')!=SCHEMA:raise EvidenceError('Unsupported evidence schema')
    if not snapshot.get('scope'):raise EvidenceError('Explicit scope required')
    origin,cutoff=stamp(snapshot['origin']),stamp(snapshot['cutoff'])
    if cutoff>origin:raise EvidenceError('Source cutoff is after forecast origin')
    now=stamp(now or datetime.now(timezone.utc))
    if origin>now:raise EvidenceError('Forecast origin is in the future')
    kind=snapshot.get('kind','PROSPECTIVE')
    if kind not in ('PROSPECTIVE','RETROSPECTIVE','SYNTHETIC'):raise EvidenceError('Invalid run kind')
    if kind=='PROSPECTIVE' and (now-origin).total_seconds()>900:raise EvidenceError('Backdated forecasts cannot be prospective')
    start=date.fromisoformat(snapshot['evaluation_start'])
    if start!=origin.astimezone(TZ).date()+timedelta(days=1):raise EvidenceError('Evaluation must start at the next Pacific midnight')
    stores=snapshot.get('stores',{})
    if len(stores)!=4 or len({s['location_id'] for s in stores.values()})!=4:raise EvidenceError('Four distinct store identities are required')
    rows=snapshot.get('rows',[])
    if not rows or len(rows)>max_rows:raise EvidenceError('Invalid or unbounded scope')
    keys=set();per_sku={};previous=origin.astimezone(TZ).date()-timedelta(days=84)
    for row in rows:
        key=(row['store_id'],row['variation_id'])
        if row['store_id'] not in stores or not row['variation_id'] or key in keys:raise EvidenceError('Duplicate/missing scope identity')
        keys.add(key)
        per_sku.setdefault(row['variation_id'],set()).add(row['store_id'])
        if row['history_start']!=previous.isoformat() or len(row['sales'])!=84:raise EvidenceError('Require the same 84 completed calendar days')
        for q in row['sales']:number(q,nonnegative=True)
        for field in ('availability','verified_availability'):
            if len(row[field])!=84 or any(v not in (('IN_STOCK','STOCKOUT','UNKNOWN') if field=='availability' else ('IN_STOCK','STOCKOUT','UNKNOWN','PARTIAL')) for v in row[field]):raise EvidenceError('Explicit daily availability required')
        if not isinstance(row['sales_complete'],bool):raise EvidenceError('Sales completeness must be explicit')
        number(row.get('on_hand'),nullable=True);number(row.get('incoming'),nullable=True,nonnegative=True)
        number(row.get('unit_cost'),nullable=True,nonnegative=True)
        lead=row.get('lead_time_days')
        if lead is not None and (type(lead) is not int or not 0<=lead<=365):raise EvidenceError('Invalid lead time')
        if type(row['pack']) is not int or not 1<=row['pack']<=10000:raise EvidenceError('Invalid pack')
        for field in ('inventory_observed_at','inventory_calculated_at'):
            if row.get(field) and stamp(row[field])>cutoff:raise EvidenceError('Inventory evidence is from after cutoff')
        for record in row.get('source_records',[]):
            if not record.get('id') or not record.get('source') or 'data' not in record or 'version' not in record:raise EvidenceError('Source identity missing')
            if stamp(record['known_at'])>cutoff or stamp(record['occurred_at'])>cutoff:raise EvidenceError('Future source record')
        if not row.get('source_records') and row['sales_complete'] and any(number(q)>0 for q in row['sales']):raise EvidenceError('Positive sales require retained source records')
        if row.get('product_created_at') and stamp(row['product_created_at'])>cutoff:raise EvidenceError('Product is newer than cutoff')
    if any(s!=set(stores) for s in per_sku.values()):raise EvidenceError('Each variation needs all four stores, including unknown evidence')
    # Preserve source payload and version, not just references into mutable business tables.
    canonical(snapshot)
    return snapshot
