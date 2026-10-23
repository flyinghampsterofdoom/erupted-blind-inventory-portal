"""Offline-derived, disposable Owner product views. Never opens a source connection."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from fractions import Fraction as F
from collections import Counter, defaultdict
from .contracts import MODELS, validate, stamp
from .policy import aggregate
from .product_diagnostics import abstention_audit, attention

HORIZONS = (7, 14, 28, 56)
LABELS = dict(zip(MODELS, ('Raw 84-day sales', 'Existing purchasing baseline', 'Conservative adjustment', 'Verified availability')))

def numeric(value):
    return None if value is None else float(F(str(value)))

def backtest_readiness(snapshot):
    """Rolling-window eligibility, without inventing pre-snapshot inventory states."""
    rows = snapshot['rows']
    return {'status': 'INSUFFICIENT_HISTORY', 'winner': None,
            'training_days_required': 84, 'retained_days': min(len(r['sales']) for r in rows),
            'horizons': {str(h): {'eligible_folds': 0, 'minimum_total_days': 84+h,
                'missing_followup_days': h, 'mae': None, 'wape': None} for h in HORIZONS},
            'dimensions': ['model', 'store', 'category', 'training-volume band', 'horizon'],
            'stockout_utility': None, 'purchase_utility': None,
            'reasons': ['No retained 84-day training window followed by an observed test horizon.',
                        'Snapshot inventory, incoming commitments and catalog knowledge cannot be backdated.',
                        'Unknown or stockout exposure censors demand; observed sales are not uncensored demand.'],
            'verified_exposure_days': dict(Counter(v for r in rows for v in r['verified_availability']))}

def build(source, destination, labels=None):
    """Explicit offline preparation; atomic publication, no mutation of input evidence."""
    source, destination = Path(source), Path(destination)
    raw = source.read_bytes(); payload = json.loads(raw); snap = payload['snapshot']
    if payload['schema'] != 'forecast-result-v1':
        raise ValueError('Expected frozen forecast-result-v1')
    validate(snap, now=stamp(snap['origin']))
    forecasts = {(str(f['store_id']), f['variation_id']): f for f in payload['forecasts']}
    labels = labels or {}; origin = datetime.fromisoformat(snap['origin'])
    metadata = {'source_sha256': hashlib.sha256(raw).hexdigest(), 'origin': snap['origin'],
                'kind': snap['kind'], 'stores': snap['stores'], 'model_version': payload['model_version'],
                'policy_version': payload['policy_version'], 'backtests': backtest_readiness(snap),
                'notice': 'Frozen historical review. Inventory is observed at the retained timestamp, not live.',
                'projection_schema': 2, 'abstentions': abstention_audit(payload),
                'rows': len(snap['rows']), 'labels_provenance': labels.get('provenance', 'Evidence identifiers')}
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=destination.name+'.preparing-',dir=destination.parent)
    os.close(fd);temp=Path(name)
    db = sqlite3.connect(temp)
    try:
        db.executescript('''PRAGMA application_id=1163282000; PRAGMA user_version=2;
        CREATE TABLE meta (body TEXT NOT NULL);
        CREATE TABLE products (store TEXT, variation TEXT, name TEXT, variant TEXT, sku TEXT, category TEXT,
        vendor TEXT, model TEXT, rate REAL, demand7 REAL, demand14 REAL, demand28 REAL, demand56 REAL,
        on_hand REAL, incoming REAL, days REAL, need INTEGER, cost REAL, excess INTEGER, slow INTEGER, pack INTEGER, unit_cost TEXT, stockout TEXT, priority TEXT, rank INTEGER, near_term INTEGER, insufficient INTEGER, missing TEXT,
        PRIMARY KEY(store,variation,model));
        CREATE TABLE details (store TEXT, variation TEXT, body TEXT, PRIMARY KEY(store,variation));
        CREATE INDEX product_filters ON products(model,store,vendor,category);
        CREATE INDEX product_attention ON products(model,need,days);''')
        db.execute('INSERT INTO meta VALUES (?)', (json.dumps(metadata),))
        for row in snap['rows']:
            store, vid = str(row['store_id']), row['variation_id']; fc = forecasts[(store, vid)]
            cat = row.get('category') or 'Unassigned'
            vendor = labels.get('vendors', {}).get(str(row.get('vendor_id')), 'Unknown supplier' if row.get('vendor_id') is None else f"Supplier #{row['vendor_id']}")
            for model in MODELS:
                result, policy = fc['models'][model], fc['policy'][model]
                rate = numeric(result['rate']); stock = numeric(row['on_hand']); incoming = numeric(row['incoming'])
                days = max(0, stock) / rate if stock is not None and rate is not None and rate > 0 else None
                observed = datetime.fromisoformat(row['inventory_observed_at']) if row.get('inventory_observed_at') else origin
                stockout = (observed + timedelta(days=days)).date().isoformat() if days is not None and days < 36500 else None
                # Excess is a scenario above the existing target, not proof of unsaleable stock.
                excess = stock is not None and policy.get('target') is not None and stock > policy['target']
                slow = (stock is not None and stock > 0 and rate == 0 and
                        all(v == 'IN_STOCK' for v in row['verified_availability'][-28:]))
                facts = {k: v for k, v in row.items() if k != 'source_records'}
                detail = {'facts': facts, 'models': fc['models'], 'policies': fc['policy'],
                          'stockout_date': None,
                          'stockout_assumption': 'Constant estimated sales rate; excludes incoming stock without a confirmed arrival date.',
                          'verified_days': row['verified_availability'].count('IN_STOCK'),
                          'unknown_days': sum(v in ('UNKNOWN', 'PARTIAL') for v in row['verified_availability']),
                          'source_record_count': len(row['source_records'])}
                if model == MODELS[0]:
                    db.execute('INSERT INTO details VALUES (?,?,?)',(store,vid,json.dumps(detail)))
                priority=attention(row,result,policy,days,excess,slow)
                need = policy.get('quantity'); unit_cost = numeric(row.get('unit_cost'))
                db.execute('INSERT INTO products VALUES ('+','.join('?' for _ in range(28))+')',
                    (store, vid, row['name'], row.get('variant', ''), row.get('sku',''), cat, vendor, model,
                     rate, *(None if rate is None else rate*h for h in HORIZONS), stock, incoming, days, need,
                     None if need is None or unit_cost is None else need*unit_cost, int(excess), int(slow), row['pack'], row.get('unit_cost'), stockout, priority['primary'], priority['rank'],int(priority['near_term']),int(priority['insufficient']),json.dumps(priority['missing'])))
        db.commit()
    except BaseException:
        db.close();temp.unlink(missing_ok=True);raise
    finally: db.close()
    with temp.open('rb') as stream:os.fsync(stream.fileno())
    os.chmod(temp, 0o400); os.replace(temp, destination)
    return metadata

class ProductView:
    """Indexed read-only projection access. No ORM, forecast calculation or external I/O."""
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        if self.db.execute('PRAGMA application_id').fetchone()[0]!=1163282000 or self.db.execute('PRAGMA user_version').fetchone()[0]!=2:
            self.db.close();raise ValueError('Unsupported product projection schema')
        self.deadline=time.monotonic()+.5
        self.db.set_progress_handler(lambda:int(time.monotonic()>self.deadline),1000)
    def close(self): self.db.close()
    def meta(self): return json.loads(self.db.execute('SELECT body FROM meta').fetchone()[0])
    def facets(self):
        values = {k: [r[0] for r in self.db.execute(f'SELECT DISTINCT {k} FROM products ORDER BY {k}')] for k in ('store','category','vendor')}
        values['category']=sorted({part for group in values['category'] for part in group.split(',')})
        return values
    def listing(self, model='B_CURRENT', store='', category='', vendor='', q='', attention='', page=0, variation=''):
        clauses, args = ['model=?'], [model]
        for key, value in (('store',store), ('category',category), ('vendor',vendor)):
            if value:
                clauses.append("instr(','||category||',',','||?||',')>0" if key=='category' else key+'=?'); args.append(value)
        if variation: clauses.append('variation=?'); args.append(variation)
        if q:
            clauses.append("(instr(lower(name||' '||variant||' '||sku), lower(?))>0)"); args.append(q)
        if attention == 'risk': clauses.append('days<=7')
        elif attention == 'reorder': clauses.append('need>0')
        elif attention == 'excess': clauses.append('excess=1')
        elif attention == 'slow': clauses.append('slow=1')
        elif attention == 'abstain': clauses.append('rate IS NULL')
        elif attention == 'evidence': clauses.append('insufficient=1')
        elif attention == 'near_term': clauses.append('near_term=1')
        where = ' AND '.join(clauses)
        summary = dict(self.db.execute('SELECT count(*) total,sum(days<=7) risk,sum(need>0) reorder,sum(excess) excess,sum(slow) slow,sum(rate IS NULL) abstain,sum(near_term) near_term,sum(insufficient) insufficient,sum(demand7) demand7,sum(demand14) demand14,sum(demand28) demand28,sum(demand56) demand56,sum(cost) known_store_cost FROM products WHERE '+where,args).fetchone())
        rows = [dict(r) for r in self.db.execute('SELECT store,variation,name,variant,sku,category,vendor,rate,demand7,demand14,demand28,demand56,on_hand,incoming,days,need,cost,excess,slow,priority,missing FROM products WHERE '+where+' ORDER BY rank,days IS NULL,days,name,variation,store LIMIT 50 OFFSET ?',args+[page*50])]
        return summary, rows
    def detail(self, store, variation, model):
        row = self.db.execute('SELECT products.*,details.body FROM products JOIN details USING(store,variation) WHERE store=? AND variation=? AND model=?',(store,variation,model)).fetchone()
        if row is None: return None
        value = dict(row); value['detail'] = json.loads(value.pop('body')); value['detail']['stockout_date']=value.pop('stockout'); return value
    def purchases(self, model='B_CURRENT', store='', category='', vendor='', q='', variation='', **ignored):
        clauses, args = ['model=?'], [model]
        for k,v in (('store',store),('category',category),('vendor',vendor)):
            if v:
                clauses.append("instr(','||category||',',','||?||',')>0" if k=='category' else k+'=?');args.append(v)
        if variation: clauses.append('variation=?');args.append(variation)
        if q: clauses.append("instr(lower(name||' '||variant||' '||sku),lower(?))>0");args.append(q)
        groups = defaultdict(list)
        for row in self.db.execute('SELECT variation,name,variant,sku,vendor,need,pack,unit_cost FROM products WHERE '+' AND '.join(clauses),args):groups[row['variation']].append(dict(row))
        results=[]
        for vid, rows in groups.items():
            first=rows[0]
            packs={r['pack'] for r in rows}
            costs={r['unit_cost'] for r in rows}
            if len(packs)!=1 or len(costs)!=1: raise ValueError('Inconsistent frozen pack/cost')
            result=aggregate([{'quantity':r['need']} for r in rows],first['pack'],first['unit_cost'])
            results.append({**result,'variation':vid,'name':first['name'],'variant':first['variant'],'sku':first['sku'],'vendor':first['vendor'],'pack':first['pack'],'stores':len(rows)})
        return sorted(results,key=lambda r: (r['quantity'] is None,-(r['quantity'] or 0),r['name']))
