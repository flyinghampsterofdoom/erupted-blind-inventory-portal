"""Deterministic rolling-origin evaluation. Fixtures are not production accuracy.

Optional JSON input: [{name, sales:[daily units], availability:[bool/null],
unit_cost:"3.00"}]. Use only owner-authorized local historical exports.
"""
from __future__ import annotations
import argparse,json
from decimal import Decimal
from pathlib import Path
from app.services.purchase_order_math_service import DemandEvidence,forecast_demand,ENGINE_VERSION


def evaluate(series, *, minimum_history=28, horizon=14, stride=14):
    results=[]
    for item in series:
        sales=[Decimal(str(v)) for v in item['sales']];availability=item.get('availability',[True]*len(sales));cost=Decimal(str(item.get('unit_cost',0)))
        rows={name:[] for name in ('unified','average_28','average_56')}
        skipped=0
        for cutoff in range(minimum_history,len(sales)-horizon+1,stride):
            if any(value is False for value in availability[cutoff:cutoff+horizon]):
                skipped+=1;continue  # Censored future sales cannot be a demand truth.
            actual=sum(sales[cutoff:cutoff+horizon],Decimal(0))
            prediction=forecast_demand(DemandEvidence(tuple(sales[:cutoff]),Decimal(0),
                availability=tuple(availability[:cutoff]),product_age_days=cutoff))
            forecasts={'unified':prediction.daily_velocity,
                'average_28':sum(sales[max(0,cutoff-28):cutoff])/min(cutoff,28),
                'average_56':sum(sales[max(0,cutoff-56):cutoff])/min(cutoff,56)}
            for method,velocity in forecasts.items():
                if velocity is None:continue
                predicted=velocity*horizon;rows[method].append((predicted,actual))
        metrics={}
        for method,points in rows.items():
            errors=[predicted-actual for predicted,actual in points];volume=sum((actual for _,actual in points),Decimal(0))
            metrics[method]={'origins':len(points),'mae_units':float(sum(abs(e) for e in errors)/len(errors)) if errors else None,
                'bias_units':float(sum(errors)/len(errors)) if errors else None,
                'wape':float(sum(abs(e) for e in errors)/volume) if volume else None,
                'shortfall_units_proxy':float(sum(max(actual-predicted,0) for predicted,actual in points)),
                'excess_units_proxy':float(sum(max(predicted-actual,0) for predicted,actual in points)),
                'capital_required_proxy':float(sum(predicted*cost for predicted,_ in points))}
        results.append({'series':item['name'],'skipped_censored_holdouts':skipped,'metrics':metrics})
    return {'engine':ENGINE_VERSION,'method':'rolling origin; 14-day observed-sales holdout; past-only fitting',
        'risk_note':'Shortfall/excess/capital are matched-horizon proxies, not measured store stockouts or actual spend.',
        'results':results}


def fixtures():
    return [{'name':'stable','sales':[2]*180,'unit_cost':'3'},
        {'name':'sustained_growth','sales':[1]*60+[2]*60+[4]*60,'unit_cost':'3'},
        {'name':'sustained_decline','sales':[4]*60+[2]*60+[1]*60,'unit_cost':'3'},
        {'name':'isolated_spike','sales':[2]*85+[100]+[2]*94,'unit_cost':'3'},
        {'name':'slowdown','sales':[2]*90+[0]*90,'unit_cost':'3'},
        {'name':'intermittent','sales':[2 if day%7<5 else 0 for day in range(180)],'unit_cost':'3'},
        {'name':'stockout_history','sales':[2]*60+[0]*14+[2]*106,'availability':[True]*60+[False]*14+[True]*106,'unit_cost':'3'}]


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input');parser.add_argument('--output',required=True);args=parser.parse_args()
    data=json.loads(Path(args.input).read_text()) if args.input else fixtures()
    result=evaluate(data);result['data_kind']='owner-supplied historical export' if args.input else 'synthetic historical fixtures; production accuracy unverified'
    Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
