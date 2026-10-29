"""Explicit isolated finite collector. Does not create/activate a schedule."""
import argparse,time
from sqlalchemy import create_engine
from app.purchasing_v4.isolation import require_isolated_url


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--isolated-url',required=True)
    p.add_argument('--reason',choices=('DAILY','MANUAL','RECOVERY'),required=True)
    args=p.parse_args();require_isolated_url(args.isolated_url)
    from uuid import uuid4
    command_key=None if args.reason=='DAILY' else args.reason.lower()+':'+str(uuid4())
    from app.services.daily_square_collector import run_once
    from app.services.purchasing_history_service import history_post
    # Established policy-enforced transport; no alternate credentials or writers.
    engine=create_engine(args.isolated_url)
    try:
        for attempt in range(3):
            try:
                result=run_once(engine,history_post,reason=args.reason,command_key=command_key)
                if result['status']=='PARTIAL' and attempt<2:
                    time.sleep(2**attempt);continue
                print(result)
                if result['status']=='PARTIAL':raise SystemExit(2)
                return
            except (RuntimeError,TimeoutError,ConnectionError,ValueError):
                if attempt==2:raise
                time.sleep(2**attempt)
    finally:engine.dispose()

if __name__=='__main__':main()
