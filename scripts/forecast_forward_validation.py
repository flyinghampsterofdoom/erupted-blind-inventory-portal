"""Explicit local forward-validation operations; never collects data or schedules work."""
import argparse,json
from app.forecast_validation.forward_validation import register,evaluate
p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['register','evaluate']);p.add_argument('--registry',required=True);p.add_argument('--input',required=True);p.add_argument('--registration');a=p.parse_args()
with open(a.input) as f:value=json.load(f)
print(register(a.registry,value) if a.operation=='register' else evaluate(a.registry,a.registration,value))
