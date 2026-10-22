"""Offline verification against the October 22 audit's retained evidence; no network."""
import argparse,importlib.util,json,sys
from pathlib import Path
from datetime import date,timedelta
from fractions import Fraction as F
from app.forecast_validation.demand import calculate,current_without_stockouts
from app.forecast_validation.policy import store_need,aggregate

def main():
    parser=argparse.ArgumentParser();parser.add_argument('audit_directory',type=Path);args=parser.parse_args();root=args.audit_directory
    sys.path.insert(0,str(root))
    import methods,ledger
    source=json.loads((root/'po139-source.private.json').read_text());events=ledger.normalize(json.loads((root/'raw-inventory-history.private.json').read_text())['changes'])
    origin=ledger.stamp(source['revision'][0]['evidence']['observed_at']);products={r['variation_id']:r['accepted'] for r in source['session_lines']};totals={m:F(0) for m in ('A_RAW84','B_CURRENT','C_CONSERVATIVE')};control=F(0);pairs=0;abstentions=0
    for line in source['lines']:
        p=products[line['variation_id']];needs={m:[] for m in totals};controls=[]
        for sid,e in p['stores'].items():
            days=[date(2026,7,29)+timedelta(days=i) for i in range(84)];u=[e['sales'].get(str(d),'0') for d in days];ev=events[(e['location_id'],line['variation_id'])];daily,_,_=ledger.reconstruct(ev,days[0],date(2026,10,22),knowledge_at=origin)
            balance=None
            for event in ev:
                if event['time']<origin and event['known']<origin:balance=event['qty'] if event['kind']=='PHYSICAL_COUNT' else balance+event['delta'] if balance is not None else None
            strict=[daily[d]['strict'] if balance is not None and balance==F(e['current_inventory']) else None for d in days]
            row={'sales':u,'sales_complete':e['sales_complete'],'availability':['IN_STOCK' if v is True else 'STOCKOUT' if v is False else 'UNKNOWN' for v in e['availability']],'verified_availability':['IN_STOCK' if v is True else 'STOCKOUT' if v is False else 'UNKNOWN' for v in strict],'on_hand':e['current_inventory'],'incoming':str(e['incoming']),'lead_time_days':p['supplier_lead_time_days'],'lifecycle':p['lifecycle']}
            rates={'A_RAW84':methods.raw_rate(u),'B_CURRENT':methods.current_rate(u,e['availability'],hand=F(e['current_inventory'])+F(e['incoming']))[0],'C_CONSERVATIVE':methods.conservative_rate(u,strict)[0],'D_VERIFIED':methods.verified_rate(u,strict,min_days=14)}
            for m,expected in rates.items():
                actual=calculate(m,row);assert (F(actual['rate']) if actual['rate'] is not None else None)==expected,(sid,line['sku'],m)
                if m in needs:needs[m].append(store_need(actual['rate'],row))
                if m=='D_VERIFIED':abstentions+=actual['rate'] is None
            assert store_need(calculate('B_CURRENT',row)['rate'],row)['quantity']==p['forecasts'][sid]['quantity']
            controls.append(store_need(current_without_stockouts(row)['rate'],row));pairs+=1
        for m in totals:totals[m]+=F(aggregate(needs[m],p['multiple'],line['unit_cost'])['cost'])
        control+=F(aggregate(controls,p['multiple'],line['unit_cost'])['cost'])
    assert totals=={'A_RAW84':F(10674),'B_CURRENT':F(15411),'C_CONSERVATIVE':F('11626.5')}
    assert totals['B_CURRENT']-control==F('6043.5') and abstentions==22
    print(json.dumps({'source':'Offline frozen audit; no order changes','store_sku_pairs':pairs,'all_four_model_rates_match_independent_audit':True,'current_policy_matches_saved_store_forecasts':True,'exposure':{m:float(v) for m,v in totals.items()},'current_without_stockouts':float(control),'stockout_adjustment':float(totals['B_CURRENT']-control),'D_verified_abstentions':abstentions},indent=2))
if __name__=='__main__':main()
