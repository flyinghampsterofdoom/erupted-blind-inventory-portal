"""Prepare disposable read-only Owner views from existing frozen evidence. No source reads."""
import argparse
import json
from app.forecast_validation.product import build

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--labels',help='Optional display-only vendor mapping JSON')
    args=parser.parse_args()
    result=build(args.source,args.destination,json.load(open(args.labels)) if args.labels else None)
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
